"""跨平台定位 Cline 本地存储：自动发现装了 Cline 扩展的各 IDE。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

EXTENSION_DIR_NAME = "saoudrizwan.claude-dev"

# IDE 应用目录名 -> 规范化类型标识
IDE_TYPE_MAP = {
    "code": "vscode",
    "code - insiders": "vscode-insiders",
    "vscodium": "vscodium",
    "cursor": "cursor",
    "trae": "trae",
    "trae cn": "trae",
    "trae solo": "trae",
    "kiro": "kiro",
    "windsurf": "windsurf",
    "codebuddy": "codebuddy",
    "codebuddy cn": "codebuddy",
    "workbuddy": "workbuddy",
    "qoder": "qoder",
}


def ide_type_of(app_dir_name: str) -> str:
    key = app_dir_name.strip().lower()
    if key in IDE_TYPE_MAP:
        return IDE_TYPE_MAP[key]
    # 兜底：Code - Exploration 之类
    for prefix, value in IDE_TYPE_MAP.items():
        if key.startswith(prefix):
            return value
    return key.replace(" ", "-") or "unknown"


@dataclass
class ClineStorage:
    """一个 IDE 下 Cline 扩展的存储位置。"""

    ide_app: str  # 应用目录名，如 "Code" / "Trae CN"
    ide_type: str  # 规范化类型，如 "vscode" / "trae"
    global_storage: Path  # .../User/globalStorage

    @property
    def extension_dir(self) -> Path:
        return self.global_storage / EXTENSION_DIR_NAME

    @property
    def task_history_file(self) -> Path:
        return self.extension_dir / "state" / "taskHistory.json"

    @property
    def tasks_dir(self) -> Path:
        return self.extension_dir / "tasks"

    @property
    def vscdb_file(self) -> Path:
        return self.global_storage / "state.vscdb"

    def is_readable(self) -> bool:
        return self.extension_dir.is_dir()

    def describe(self) -> str:
        return f"{self.ide_app} ({self.ide_type}) -> {self.extension_dir}"


def _glob_roots() -> List[Path]:
    """按平台返回可能的 */*/User/globalStorage 候选根目录。"""
    home = Path.home()
    roots: List[Path] = []
    if sys.platform == "darwin":
        candidates = [home / "Library" / "Application Support"]
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        candidates = [Path(appdata)] if appdata else []
        candidates.append(home / "AppData" / "Roaming")
    else:
        candidates = [home / ".config"]
        candidates.append(home / ".var" / "app")
    for candidate in candidates:
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def discover_storages(extra_roots: Iterable[str] | None = None) -> List[ClineStorage]:
    """自动发现所有安装了 Cline 扩展的 IDE 存储。

    extra_roots 可直接指向 globalStorage 或其上级目录。
    """
    found: List[ClineStorage] = []
    seen: set[str] = set()

    def add(global_storage: Path) -> None:
        if not global_storage.is_dir():
            return
        if not (global_storage / EXTENSION_DIR_NAME).is_dir():
            return
        key = str(global_storage)
        if key in seen:
            return
        seen.add(key)
        # .../<App>/User/globalStorage
        try:
            app = global_storage.parent.parent.name
        except IndexError:
            app = global_storage.parent.name
        found.append(
            ClineStorage(ide_app=app, ide_type=ide_type_of(app), global_storage=global_storage)
        )

    for root in _glob_roots():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            add(entry / "User" / "globalStorage")

    for raw in extra_roots or []:
        path = Path(os.path.expanduser(os.path.expandvars(str(raw))))
        # 允许传入 globalStorage / User / 应用目录 三种层级
        for candidate in (
            path,
            path / "User" / "globalStorage",
            path / "globalStorage",
        ):
            add(candidate)
            if (candidate / EXTENSION_DIR_NAME).is_dir():
                break

    return found
