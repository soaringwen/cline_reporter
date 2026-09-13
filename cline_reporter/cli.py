"""命令行入口：doctor / collect / stats / report / export / schedule。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .collector import CollectResult, SkillRecord, UsageRecord, collect, detect_terminal
from .config import APP_NAME, endpoint_url, load_config, state_dir
from .locate import discover_storages
from .reporter import Reporter
from .scheduler import LAUNCH_AGENT_DIR, install as schedule_install
from .scheduler import status as schedule_status
from .scheduler import uninstall as schedule_uninstall
from .state import ReportState, select_new
from . import stats as stats_mod
from .stats import aggregate_skills, aggregate_usage, export as export_data, render_table

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

USAGE_DIM_LABELS = {
    "model": "模型",
    "project": "项目",
    "terminal": "终端",
    "ide": "IDE",
    "source": "数据源",
    "day": "日期",
    "task": "任务",
}


def _echo(message: str = "") -> None:
    print(message)


def _usage_entry(record: UsageRecord) -> Dict[str, Any]:
    return {
        "kind": "usage",
        "key": record.key,
        "fingerprint": record.fingerprint(),
        "payload": record.to_payload(),
    }


def _skill_entry(record: SkillRecord) -> Dict[str, Any]:
    return {
        "kind": "skill",
        "key": record.key,
        "fingerprint": record.fingerprint(),
        "payload": record.to_payload(),
    }


def _load(args: argparse.Namespace) -> Dict[str, Any]:
    return load_config(getattr(args, "config", None))


def _collect_with_banner(config: Dict[str, Any]) -> CollectResult:
    result = collect(config)
    for warning in result.warnings:
        print(f"[warn] {warning}", file=sys.stderr)
    return result


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    terminal_id, hostname = detect_terminal()

    _echo(f"{APP_NAME} v{__version__} 环境自检")
    _echo("=" * 64)
    _echo(f"Python            : {sys.version.split()[0]} ({sys.executable})")
    _echo(f"配置文件          : {config.get('_config_path') or '（未找到，使用内置默认值）'}")
    _echo(f"状态目录          : {state_dir(config)}")
    _echo(f"终端标识          : terminal_id={terminal_id}  hostname={hostname}")
    _echo(f"usage 上报地址    : {endpoint_url(config, 'usage_path') or '（未配置）'}")
    _echo(f"skill 上报地址    : {endpoint_url(config, 'skill_path') or '（未配置）'}")
    _echo(f"上报模式          : {config['endpoint']['mode']} / batch_size={config['endpoint']['batch_size']}")
    _echo(f"包含任务文本      : {config['collect']['include_task_text']}")
    _echo("")

    storages = discover_storages(config["collect"].get("ide_roots"))
    _echo(f"发现 Cline 存储 {len(storages)} 处：")
    if not storages:
        _echo("  （无）请检查是否已安装 Cline 扩展，或用 collect.ide_roots 指定路径")
        return EXIT_FAIL

    total_usage = 0
    for storage in storages:
        history_ok = storage.task_history_file.is_file()
        tasks_count = len(list(storage.tasks_dir.iterdir())) if storage.tasks_dir.is_dir() else 0
        meta_count = sum(
            1 for d in storage.tasks_dir.iterdir() if (d / "task_metadata.json").is_file()
        ) if storage.tasks_dir.is_dir() else 0
        _echo(f"  - {storage.describe()}")
        _echo(
            f"      taskHistory.json: {'✓' if history_ok else '✗'}"
            f" | tasks/ 目录: {tasks_count} 个"
            f" | task_metadata.json: {meta_count} 个"
            f" | state.vscdb: {'✓' if storage.vscdb_file.is_file() else '✗'}"
        )
        if history_ok:
            from .sources import read_task_history

            total_usage += len(read_task_history(storage.task_history_file))
    _echo("")
    _echo(f"taskHistory 记录总数: {total_usage}")

    _echo("")
    _echo("执行一次试采集…")
    result = _collect_with_banner(config)
    _echo(f"  归一化 usage 记录 : {len(result.usage)}")
    _echo(f"  归一化 skill 记录 : {len(result.skills)}")
    skills = sorted({r.skill for r in result.skills})
    if skills:
        _echo(f"  命中的 skill      : {', '.join(skills)}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# collect
# --------------------------------------------------------------------------- #
def cmd_collect(args: argparse.Namespace) -> int:
    config = _load(args)
    result = _collect_with_banner(config)

    if args.out:
        fmt = "csv" if str(args.out).lower().endswith(".csv") else "json"
        content = export_data(result, fmt, Path(args.out).expanduser())
        _echo(f"已导出 {fmt.upper()} -> {Path(args.out).expanduser()} ({len(content)} 字节)")

    if args.json:
        _echo(
            json.dumps(
                {
                    **result.summary(),
                    "usage": stats_mod.usage_rows(result.usage),
                    "skills": stats_mod.skill_rows(result.skills),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _echo("采集结果：")
        for key, value in result.summary().items():
            if isinstance(value, list):
                _echo(f"  {key}:")
                for item in value or ["（无）"]:
                    _echo(f"    - {item}")
            else:
                _echo(f"  {key}: {value}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def _print_usage_table(rows: List[Dict[str, Any]], dimension: str) -> None:
    headers = [USAGE_DIM_LABELS.get(dimension, dimension)] + [
        label for _key, label in stats_mod.USAGE_METRICS
    ]
    keys = ["key"] + [key for key, _label in stats_mod.USAGE_METRICS]
    _echo(f"\n按{USAGE_DIM_LABELS.get(dimension, dimension)}统计（token 用量）")
    _echo(render_table(rows, headers, keys))


def _print_skill_table(rows: List[Dict[str, Any]]) -> None:
    headers = ["Skill"] + [label for _key, label in stats_mod.SKILL_METRICS]
    keys = ["key"] + [key for key, _label in stats_mod.SKILL_METRICS]
    _echo("\n按 Skill 统计（使用情况）")
    _echo(render_table(rows, headers, keys))


def cmd_stats(args: argparse.Namespace) -> int:
    config = _load(args)
    result = _collect_with_banner(config)

    dimensions = args.by or ["model", "project", "terminal", "ide", "day"]

    if args.json:
        payload: Dict[str, Any] = {"summary": result.summary()}
        for dimension in dimensions:
            payload[dimension] = aggregate_usage(result.usage, dimension)
        if args.skill:
            payload["skill"] = aggregate_skills(result.skills)
        _echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    total_in = sum(r.tokens_in for r in result.usage)
    total_out = sum(r.tokens_out for r in result.usage)
    total_cache_read = sum(r.cache_reads for r in result.usage)
    total_cache_write = sum(r.cache_writes for r in result.usage)
    total_cost = sum(r.total_cost for r in result.usage)

    _echo(f"终端: {result.hostname} ({result.terminal_id})")
    _echo(f"任务数: {len(result.usage)}   总 token: {total_in + total_out:,}")
    _echo(
        f"  输入 {total_in:,} | 输出 {total_out:,}"
        f" | 缓存读 {total_cache_read:,} | 缓存写 {total_cache_write:,}"
        f" | 费用 {total_cost:.6f}"
    )

    for dimension in dimensions:
        rows = aggregate_usage(result.usage, dimension)
        _print_usage_table(rows, dimension)

    if args.skill:
        _print_skill_table(aggregate_skills(result.skills))

    return EXIT_OK


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def cmd_export(args: argparse.Namespace) -> int:
    config = _load(args)
    result = _collect_with_banner(config)
    out = Path(args.out).expanduser() if args.out else None
    content = export_data(result, args.format, out, include_skills=not args.no_skill)
    if out:
        _echo(f"已导出 {args.format.upper()} -> {out} ({len(content)} 字节)")
    else:
        _echo(content)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def cmd_report(args: argparse.Namespace) -> int:
    config = _load(args)
    if not args.dry_run and not config["endpoint"].get("url") and not str(
        config["endpoint"].get("usage_path", "")
    ).startswith("http"):
        print(
            "[error] 未配置上报地址。请在配置文件中设置 endpoint.url，"
            "或先用 --dry-run 查看将要上报的内容。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    directory = state_dir(config)
    state = ReportState.load(directory)
    reporter = Reporter(config)
    result = _collect_with_banner(config)

    _echo(f"终端: {result.hostname} ({result.terminal_id})")
    _echo(f"采集到 usage {len(result.usage)} 条 / skill {len(result.skills)} 条")

    if args.force:
        usage_new, skill_new = list(result.usage), list(result.skills)
    else:
        usage_new = select_new(result.usage, state)
        skill_new = select_new(result.skills, state)

    entries: List[Dict[str, Any]] = [_usage_entry(r) for r in usage_new]
    entries += [_skill_entry(r) for r in skill_new]
    if args.limit and args.limit > 0:
        entries = entries[: args.limit]

    new_keys = {entry["key"] for entry in entries}
    pending: List[Dict[str, Any]] = []
    if not args.no_pending:
        pending = [
            item
            for item in state.read_pending()
            if isinstance(item, dict) and item.get("key") not in new_keys
        ]

    all_entries = pending + entries
    if not all_entries:
        _echo("没有需要上报的新数据（增量比对后为空）。")
        return EXIT_OK

    _echo(
        f"待上报：新增 {len(entries)} 条"
        f"{f' + 历史失败重试 {len(pending)} 条' if pending else ''}"
    )

    groups: Dict[str, List[Dict[str, Any]]] = {"usage": [], "skill": []}
    for entry in all_entries:
        groups.setdefault(entry.get("kind", "usage"), []).append(entry)

    succeeded = 0
    failed_entries: List[Dict[str, Any]] = []

    for kind in ("usage", "skill"):
        batch_entries = groups.get(kind) or []
        if not batch_entries:
            continue
        payloads = [entry["payload"] for entry in batch_entries]
        chunks = reporter.send_payloads(
            kind,
            payloads,
            dry_run=args.dry_run,
            hostname=result.hostname,
            terminal_id=result.terminal_id,
        )
        offset = 0
        for chunk, send_result in chunks:
            size = len(chunk)
            current = batch_entries[offset : offset + size]
            offset += size
            if send_result.ok:
                succeeded += size
                if not args.dry_run:
                    for entry in current:
                        state.mark_reported(entry["key"], entry["fingerprint"], kind)
            else:
                failed_entries.extend(current)
                print(
                    f"[error] {kind} 上报失败({size} 条): {send_result.error}"
                    + (f" | 响应: {send_result.body}" if send_result.body else ""),
                    file=sys.stderr,
                )

    if args.dry_run:
        url = reporter.url_for("usage")
        _echo(f"[dry-run] 未真正发送。目标地址示例: {url or '（未配置）'}")
        sample = (all_entries[0]["payload"] if all_entries else {})
        _echo("[dry-run] 首条 payload 示例:")
        _echo(json.dumps(sample, ensure_ascii=False, indent=2)[:2000])
    else:
        state.clear_pending()
        if failed_entries:
            state.rewrite_pending(failed_entries)
        state.save()

    _echo("")
    _echo(f"上报完成：成功 {succeeded} 条，失败 {len(failed_entries)} 条")
    if failed_entries and not args.dry_run:
        _echo(f"失败数据已存入本地队列，下次运行自动重试：{directory / 'pending.jsonl'}")
        return EXIT_FAIL
    return EXIT_OK


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #
def cmd_schedule(args: argparse.Namespace) -> int:
    config = _load(args)
    directory = state_dir(config)
    log_dir = directory / "logs"

    if args.action == "install":
        info = schedule_install(
            interval_minutes=args.interval,
            config_path=config.get("_config_path"),
            extra_args=[],
            log_dir=log_dir,
            dry_run=args.dry_run,
        )
    elif args.action == "uninstall":
        info = schedule_uninstall()
    else:
        info = schedule_status()

    _echo(json.dumps(info, ensure_ascii=False, indent=2))
    if args.action == "install" and not args.dry_run:
        _echo(f"\n日志目录: {log_dir}")
        if LAUNCH_AGENT_DIR.exists() and sys.platform == "darwin":
            _echo(f"launchd plist: {LAUNCH_AGENT_DIR / 'com.cline-usage-reporter.plist'}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cline-reporter",
        description="Cline Token 用量与 Skill 使用 本地旁路采集上报工具（上报方案三）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  cline-reporter doctor                       # 环境自检\n"
            "  cline-reporter stats --skill                # 本地统计\n"
            "  cline-reporter report --dry-run             # 试跑（不发送）\n"
            "  cline-reporter report                       # 增量上报\n"
            "  cline-reporter export --format csv --out ~/cline.csv\n"
            "  cline-reporter schedule install --interval 30\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", help="配置文件路径（JSON）")

    doctor = sub.add_parser("doctor", help="环境自检与数据源探测")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    collect_cmd = sub.add_parser("collect", help="采集本地数据并预览/导出")
    add_common(collect_cmd)
    collect_cmd.add_argument("--json", action="store_true", help="以 JSON 输出全部明细")
    collect_cmd.add_argument("--out", help="导出到文件（.json / .csv）")
    collect_cmd.set_defaults(func=cmd_collect)

    stats_cmd = sub.add_parser("stats", help="本地统计（按模型/项目/终端/Skill 等）")
    add_common(stats_cmd)
    stats_cmd.add_argument(
        "--by",
        action="append",
        choices=sorted(USAGE_DIM_LABELS.keys()),
        help="统计维度，可重复指定；默认 model,project,terminal,ide,day",
    )
    stats_cmd.add_argument("--skill", action="store_true", default=True, help="包含 Skill 统计（默认开启）")
    stats_cmd.add_argument("--no-skill", action="store_false", dest="skill", help="不统计 Skill")
    stats_cmd.add_argument("--json", action="store_true", help="以 JSON 输出")
    stats_cmd.set_defaults(func=cmd_stats)

    report_cmd = sub.add_parser("report", help="增量上报到后台")
    add_common(report_cmd)
    report_cmd.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不真正请求")
    report_cmd.add_argument("--force", action="store_true", help="忽略增量状态，全量重报")
    report_cmd.add_argument("--limit", type=int, default=0, help="本次最多上报多少条（0 表示不限）")
    report_cmd.add_argument("--no-pending", action="store_true", help="不重试本地失败队列")
    report_cmd.set_defaults(func=cmd_report)

    export_cmd = sub.add_parser("export", help="导出明细到 CSV / JSON")
    add_common(export_cmd)
    export_cmd.add_argument("--format", choices=["csv", "json"], default="csv")
    export_cmd.add_argument("--out", help="输出文件路径；不指定则打印到标准输出")
    export_cmd.add_argument("--no-skill", action="store_true", help="不导出 skill 段")
    export_cmd.set_defaults(func=cmd_export)

    sched = sub.add_parser("schedule", help="安装/卸载/查看定时采集任务")
    add_common(sched)
    sched.add_argument("action", choices=["install", "uninstall", "status"], help="操作类型")
    sched.add_argument("--interval", type=int, default=30, help="采集间隔（分钟），默认 30")
    sched.add_argument("--dry-run", action="store_true", help="只打印将要执行的配置，不落盘")
    sched.set_defaults(func=cmd_schedule)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
