"""本地统计聚合与导出（CSV / JSON / 终端表格）。"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .collector import CollectResult, SkillRecord, UsageRecord

DIMENSIONS = ("model", "project", "terminal", "ide", "source", "day", "skill", "task")

# 维度 -> (表头, 取值函数)
_DIM_FIELDS = {
    "model": ("模型", lambda r: r.model or "unknown"),
    "project": ("项目", lambda r: r.project or "unknown"),
    "terminal": ("终端", lambda r: r.hostname or r.terminal_id or "unknown"),
    "ide": ("IDE", lambda r: r.ide_type or "unknown"),
    "source": ("数据源", lambda r: r.source or "unknown"),
    "day": ("日期", lambda r: _day_of(r.ts)),
    "task": ("任务", lambda r: _task_label(r)),
}

USAGE_METRICS = (
    ("tasks", "任务数"),
    ("tokens_in", "输入 token"),
    ("tokens_out", "输出 token"),
    ("cache_writes", "缓存写"),
    ("cache_reads", "缓存读"),
    ("total_tokens", "合计 token"),
    ("total_cost", "费用"),
    ("size", "数据量(B)"),
)

SKILL_METRICS = (
    ("tasks", "任务数"),
    ("files", "关联文件数"),
)


def _day_of(ts: Optional[int]) -> str:
    if not ts:
        return "unknown"
    try:
        return _dt.datetime.fromtimestamp(ts / 1000.0).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _task_label(record: UsageRecord) -> str:
    text = (record.task_text or "").strip().replace("\n", " ")
    if text:
        return f"{record.task_id} · {text[:60]}"
    return record.task_id


def aggregate_usage(records: Sequence[UsageRecord], dimension: str) -> List[Dict[str, Any]]:
    """按指定维度聚合 token 用量。"""
    if dimension not in _DIM_FIELDS:
        raise ValueError(f"不支持的统计维度: {dimension}（可选 {', '.join(_DIM_FIELDS)}）")
    _, key_fn = _DIM_FIELDS[dimension]

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "key": "",
            "tasks": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_writes": 0,
            "cache_reads": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "size": 0,
            "last_ts": 0,
        }
    )

    for record in records:
        bucket = buckets[str(key_fn(record))]
        bucket["key"] = str(key_fn(record))
        bucket["tasks"] += 1
        bucket["tokens_in"] += record.tokens_in
        bucket["tokens_out"] += record.tokens_out
        bucket["cache_writes"] += record.cache_writes
        bucket["cache_reads"] += record.cache_reads
        bucket["total_tokens"] += record.tokens_in + record.tokens_out
        bucket["total_cost"] += record.total_cost
        bucket["size"] += record.size
        bucket["last_ts"] = max(bucket["last_ts"], record.ts or 0)

    rows = list(buckets.values())
    rows.sort(key=lambda row: (row["total_tokens"], row["tasks"]), reverse=True)
    return rows


def aggregate_skills(records: Sequence[SkillRecord], dimension: str = "skill") -> List[Dict[str, Any]]:
    """按 skill / 终端 / 项目维度聚合 skill 使用。"""
    if dimension == "skill":
        key_fn = lambda r: r.skill  # noqa: E731
    elif dimension == "terminal":
        key_fn = lambda r: r.hostname or r.terminal_id  # noqa: E731
    else:
        key_fn = lambda r: r.skill  # noqa: E731

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"key": "", "tasks": 0, "files": 0, "task_ids": set(), "last_ts": 0}
    )
    for record in records:
        key = str(key_fn(record) or "unknown")
        bucket = buckets[key]
        bucket["key"] = key
        bucket["tasks"] += 1
        bucket["files"] += record.file_count
        bucket["task_ids"].add(record.task_id)
        bucket["last_ts"] = max(bucket["last_ts"], record.used_at or 0)

    rows = []
    for bucket in buckets.values():
        rows.append(
            {
                "key": bucket["key"],
                "tasks": len(bucket["task_ids"]),
                "files": bucket["files"],
                "last_ts": bucket["last_ts"],
            }
        )
    rows.sort(key=lambda row: (row["tasks"], row["files"]), reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_table(
    rows: Sequence[Dict[str, Any]],
    headers: Sequence[str],
    keys: Sequence[str],
    max_width: int = 48,
) -> str:
    """纯文本等宽表格（中日韩字符按 2 宽度计算）。"""
    def width(text: str) -> int:
        return sum(2 if _is_wide(ch) else 1 for ch in text)

    def pad(text: str, target: int) -> str:
        return text + " " * max(0, target - width(text))

    def cell(value: Any) -> str:
        if isinstance(value, float):
            text = f"{value:.6f}".rstrip("0").rstrip(".") or "0"
        else:
            text = str(value)
        if width(text) > max_width:
            clipped = ""
            for ch in text:
                if width(clipped + ch) > max_width - 1:
                    break
                clipped += ch
            text = clipped + "…"
        return text

    table = [[cell(row.get(key, "")) for key in keys] for row in rows]
    header_cells = [cell(h) for h in headers]
    widths = [width(h) for h in header_cells]
    for row in table:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], width(value))

    lines = ["  ".join(pad(h, widths[i]) for i, h in enumerate(header_cells)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    for row in table:
        lines.append("  ".join(pad(value, widths[i]) for i, value in enumerate(row)).rstrip())
    if not table:
        lines.append("（无数据）")
    return "\n".join(lines)


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    )


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
USAGE_COLUMNS = [
    "task_id",
    "task_ulid",
    "ts",
    "datetime",
    "task_text_hash",
    "task_text",
    "model",
    "model_provider",
    "models_used",
    "project",
    "cwd",
    "tokens_in",
    "tokens_out",
    "cache_writes",
    "cache_reads",
    "total_tokens",
    "total_cost",
    "size",
    "is_favorited",
    "ide_type",
    "ide_app",
    "source",
    "hostname",
    "terminal_id",
    "cline_version",
]


def _usage_row(record: UsageRecord) -> Dict[str, Any]:
    return {
        "task_id": record.task_id,
        "task_ulid": record.task_ulid,
        "ts": record.ts,
        "datetime": (
            _dt.datetime.fromtimestamp(record.ts / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            if record.ts
            else ""
        ),
        "task_text_hash": record.task_text_hash,
        "task_text": record.task_text or "",
        "model": record.model or "",
        "model_provider": record.model_provider or "",
        "models_used": ",".join(record.models_used),
        "project": record.project or "",
        "cwd": record.cwd or "",
        "tokens_in": record.tokens_in,
        "tokens_out": record.tokens_out,
        "cache_writes": record.cache_writes,
        "cache_reads": record.cache_reads,
        "total_tokens": record.tokens_in + record.tokens_out,
        "total_cost": record.total_cost,
        "size": record.size,
        "is_favorited": record.is_favorited,
        "ide_type": record.ide_type,
        "ide_app": record.ide_app,
        "source": record.source,
        "hostname": record.hostname,
        "terminal_id": record.terminal_id,
        "cline_version": record.cline_version or "",
    }


SKILL_COLUMNS = [
    "task_id",
    "skill",
    "used_at",
    "datetime",
    "file_count",
    "files",
    "record_sources",
    "ide_type",
    "hostname",
    "terminal_id",
]


def _skill_row(record: SkillRecord) -> Dict[str, Any]:
    return {
        "task_id": record.task_id,
        "skill": record.skill,
        "used_at": record.used_at,
        "datetime": (
            _dt.datetime.fromtimestamp(record.used_at / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            if record.used_at
            else ""
        ),
        "file_count": record.file_count,
        "files": ";".join(record.files),
        "record_sources": ",".join(record.record_sources),
        "ide_type": record.ide_type,
        "hostname": record.hostname,
        "terminal_id": record.terminal_id,
    }


def export(
    result: CollectResult,
    fmt: str,
    out: Optional[Path],
    include_skills: bool = True,
) -> str:
    """导出采集结果，返回写入内容（同时落盘）。"""
    fmt = fmt.lower()
    if fmt == "csv":
        content = _to_csv(result, include_skills)
    elif fmt == "json":
        content = _to_json(result, include_skills)
    else:
        raise ValueError(f"不支持的导出格式: {fmt}（可选 csv / json）")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
    return content


def _to_csv(result: CollectResult, include_skills: bool) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=USAGE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for record in result.usage:
        writer.writerow(_usage_row(record))
    # skill 段用带标记的分隔注释行，保证单文件可解析
    if include_skills and result.skills:
        buffer.write("\n")
        buffer.write("# --- skill_usage ---\n")
        skill_writer = csv.DictWriter(buffer, fieldnames=SKILL_COLUMNS, extrasaction="ignore")
        skill_writer.writeheader()
        for record in result.skills:
            skill_writer.writerow(_skill_row(record))
    return buffer.getvalue()


def _to_json(result: CollectResult, include_skills: bool) -> str:
    payload = {
        "terminal_id": result.terminal_id,
        "hostname": result.hostname,
        "storages": [s.describe() for s in result.storages],
        "usage": [_usage_row(r) for r in result.usage],
        "skills": [_skill_row(r) for r in (result.skills if include_skills else [])],
        "warnings": result.warnings,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def usage_rows(records: Iterable[UsageRecord]) -> List[Dict[str, Any]]:
    return [_usage_row(r) for r in records]


def skill_rows(records: Iterable[SkillRecord]) -> List[Dict[str, Any]]:
    return [_skill_row(r) for r in records]
