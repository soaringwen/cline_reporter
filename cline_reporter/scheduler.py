"""定时任务安装：macOS launchd / Linux cron / Windows 计划任务。"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

LABEL = "com.cline-usage-reporter"
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"


def _python_executable() -> str:
    return sys.executable or "python3"


def _build_command(config_path: Optional[str], extra_args: List[str]) -> List[str]:
    cmd = [_python_executable(), "-m", "cline_reporter", "report"]
    if config_path:
        cmd += ["--config", config_path]
    cmd += list(extra_args)
    return cmd


def plist_path() -> Path:
    return LAUNCH_AGENT_DIR / f"{LABEL}.plist"


def install_macos(
    interval_minutes: int,
    config_path: Optional[str],
    extra_args: List[str],
    log_dir: Path,
) -> Dict[str, str]:
    interval = max(1, int(interval_minutes))
    log_dir.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)

    command = _build_command(config_path, extra_args)
    # 让 launchd 下也能找到 PATH 中的依赖
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": str(Path.home()),
    }

    payload = {
        "Label": LABEL,
        "ProgramArguments": command,
        "StartInterval": interval * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "reporter.out.log"),
        "StandardErrorPath": str(log_dir / "reporter.err.log"),
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(Path.home()),
    }

    target = plist_path()
    with target.open("wb") as fh:
        plistlib.dump(payload, fh)

    _launchctl(["bootout", f"gui/{os.getuid()}/{LABEL}"], ignore_error=True)
    result = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(target)])
    if result.returncode != 0:
        # 老版本 macOS 回退
        result = _launchctl(["load", "-w", str(target)])

    return {
        "platform": "launchd",
        "plist": str(target),
        "command": " ".join(command),
        "interval_minutes": str(interval),
        "loaded": "yes" if result.returncode == 0 else f"no ({result.stderr.strip()})",
    }


def uninstall_macos() -> Dict[str, str]:
    target = plist_path()
    _launchctl(["bootout", f"gui/{os.getuid()}/{LABEL}"], ignore_error=True)
    if target.exists():
        target.unlink()
    return {"platform": "launchd", "plist": str(target), "removed": "yes"}


def status_macos() -> Dict[str, str]:
    target = plist_path()
    result = _launchctl(["print", f"gui/{os.getuid()}/{LABEL}"], ignore_error=True)
    return {
        "platform": "launchd",
        "plist": str(target),
        "plist_exists": "yes" if target.exists() else "no",
        "loaded": "yes" if result.returncode == 0 else "no",
    }


def _launchctl(args: List[str], ignore_error: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if ignore_error:
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(exc))
        raise


def cron_line(interval_minutes: int, config_path: Optional[str], extra_args: List[str]) -> str:
    interval = max(1, int(interval_minutes))
    command = " ".join(_build_command(config_path, extra_args))
    if interval == 60 or (interval % 60 == 0 and interval // 60 <= 24):
        hours = max(1, interval // 60)
        return f"0 */{hours} * * * {command} >/dev/null 2>&1"
    return f"*/{interval} * * * * {command} >/dev/null 2>&1"


def install_linux_cron(
    interval_minutes: int, config_path: Optional[str], extra_args: List[str]
) -> Dict[str, str]:
    line = cron_line(interval_minutes, config_path, extra_args)
    existing = _read_crontab()
    kept = [entry for entry in existing.splitlines() if LABEL not in entry and "cline_reporter report" not in entry]
    kept = [entry for entry in kept if entry.strip()]
    kept.append(f"# {LABEL}")
    kept.append(line)
    new_tab = "\n".join(kept) + "\n"

    result = subprocess.run(
        ["crontab", "-"], input=new_tab, capture_output=True, text=True, check=False
    )
    return {
        "platform": "cron",
        "installed": "yes" if result.returncode == 0 else f"no ({result.stderr.strip()})",
        "cron_line": line,
    }


def uninstall_linux_cron() -> Dict[str, str]:
    existing = _read_crontab()
    kept = [
        entry
        for entry in existing.splitlines()
        if LABEL not in entry and "cline_reporter report" not in entry
    ]
    new_tab = "\n".join(entry for entry in kept if entry.strip()) + "\n"
    result = subprocess.run(
        ["crontab", "-"], input=new_tab, capture_output=True, text=True, check=False
    )
    return {
        "platform": "cron",
        "removed": "yes" if result.returncode == 0 else f"no ({result.stderr.strip()})",
    }


def _read_crontab() -> str:
    if shutil.which("crontab") is None:
        return ""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def windows_command(interval_minutes: int, config_path: Optional[str], extra_args: List[str]) -> str:
    command = " ".join(_build_command(config_path, extra_args))
    interval = max(1, int(interval_minutes))
    return (
        f'schtasks /Create /TN "{LABEL}" /SC MINUTE /MO {interval} '
        f'/TR "{command}" /F'
    )


def install(
    interval_minutes: int,
    config_path: Optional[str],
    extra_args: List[str],
    log_dir: Path,
    dry_run: bool = False,
) -> Dict[str, str]:
    if dry_run:
        if sys.platform == "darwin":
            return {
                "platform": "launchd",
                "plist": str(plist_path()),
                "command": " ".join(_build_command(config_path, extra_args)),
                "interval_minutes": str(interval_minutes),
                "dry_run": "yes",
            }
        if os.name == "nt":
            return {"platform": "schtasks", "command": windows_command(interval_minutes, config_path, extra_args), "dry_run": "yes"}
        return {"platform": "cron", "cron_line": cron_line(interval_minutes, config_path, extra_args), "dry_run": "yes"}

    if sys.platform == "darwin":
        return install_macos(interval_minutes, config_path, extra_args, log_dir)
    if os.name == "nt":
        return {
            "platform": "schtasks",
            "hint": "请在管理员 PowerShell 中执行以下命令：",
            "command": windows_command(interval_minutes, config_path, extra_args),
        }
    return install_linux_cron(interval_minutes, config_path, extra_args)


def uninstall() -> Dict[str, str]:
    if sys.platform == "darwin":
        return uninstall_macos()
    if os.name == "nt":
        return {
            "platform": "schtasks",
            "hint": "请执行：",
            "command": f'schtasks /Delete /TN "{LABEL}" /F',
        }
    return uninstall_linux_cron()


def status() -> Dict[str, str]:
    if sys.platform == "darwin":
        return status_macos()
    if os.name == "nt":
        return {"platform": "schtasks", "command": f'schtasks /Query /TN "{LABEL}"'}
    return {"platform": "cron", "cron_line": cron_line(30, None, [])}
