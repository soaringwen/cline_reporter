"""底层数据源读取：taskHistory.json / task_metadata.json / 老版本 state.vscdb。"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

# skill 路径特征：以 .cline/skills/<name>/ 开头或位于路径中段
SKILL_PATH_RE = re.compile(r"(?:^|/)\.cline/skills/([^/\\]+)")

LEGACY_GLOBAL_STATE_KEY = "saoudrizwan.claude-dev"
LEGACY_FALLBACK_KEYS = (
    "saoudrizwan.claude-dev",
    "claude-dev",
    "cline",
)


def _read_json(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# 新版：state/taskHistory.json
# --------------------------------------------------------------------------- #
def read_task_history(path: Path) -> List[Dict[str, Any]]:
    """读取 taskHistory.json，返回任务记录列表。"""
    data = _read_json(path)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    # 少数版本包了一层 {"taskHistory": [...]}
    if isinstance(data, dict):
        inner = data.get("taskHistory")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
    return []


# --------------------------------------------------------------------------- #
# 老版本：globalStorage/state.vscdb 的 ItemTable 中的全局状态 JSON
# --------------------------------------------------------------------------- #
def read_legacy_task_history(vscdb: Path) -> List[Dict[str, Any]]:
    """从 state.vscdb 读取老版本 Cline 的 taskHistory。

    老版本把 globalState 整体序列化进 ItemTable 的 `saoudrizwan.claude-dev` 键，
    taskHistory 是其中的一个数组字段。若不存在则返回空列表（不抛错）。
    """
    if not vscdb.is_file():
        return []

    keys = list(LEGACY_FALLBACK_KEYS)
    records: List[Dict[str, Any]] = []
    try:
        # 只读方式打开，避免影响运行中的 VS Code
        uri = f"file:{vscdb}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only = ON")
            for key in keys:
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)
                ).fetchone()
                if not row or not row[0]:
                    continue
                payload = _decode_item_value(row[0])
                found = _extract_task_history(payload)
                if found:
                    records = found
                    break
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return records


def _decode_item_value(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 极端情况
            return None
    if isinstance(value, str):
        return _read_json_str(value)
    return value


def _read_json_str(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_task_history(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("taskHistory", "task_history", "tasks"):
        inner = payload.get(key)
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
    return []


# --------------------------------------------------------------------------- #
# 任务详情：tasks/<taskId>/task_metadata.json
# --------------------------------------------------------------------------- #
def read_task_metadata(tasks_dir: Path, task_id: str) -> Optional[Dict[str, Any]]:
    meta = _read_json(tasks_dir / task_id / "task_metadata.json")
    return meta if isinstance(meta, dict) else None


def extract_skill_paths(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 files_in_context 提取命中的 skill 记录（同名 skill 合并，保留最早读取时间）。"""
    files = meta.get("files_in_context")
    if not isinstance(files, list):
        return []

    merged: Dict[str, Dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        match = SKILL_PATH_RE.search(raw_path)
        if not match:
            continue
        name = match.group(1).strip()
        if not name:
            continue

        read_date = _as_int(item.get("cline_read_date")) or _as_int(item.get("user_edit_date"))
        entry = merged.get(name)
        if entry is None:
            merged[name] = {
                "skill": name,
                "first_read_ts": read_date,
                "files": [raw_path],
                "record_sources": sorted(
                    {str(item.get("record_source"))} if item.get("record_source") else set()
                ),
            }
        else:
            if raw_path not in entry["files"]:
                entry["files"].append(raw_path)
            if read_date and (entry["first_read_ts"] is None or read_date < entry["first_read_ts"]):
                entry["first_read_ts"] = read_date
            source = item.get("record_source")
            if source and str(source) not in entry["record_sources"]:
                entry["record_sources"].append(str(source))
    return sorted(merged.values(), key=lambda x: x["skill"])


def extract_models(meta: Dict[str, Any]) -> List[str]:
    """model_usage 中出现的模型列表（按出现顺序去重）。"""
    usage = meta.get("model_usage")
    if not isinstance(usage, list):
        return []
    models: List[str] = []
    for item in usage:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model_id")
        if isinstance(model_id, str) and model_id and model_id not in models:
            models.append(model_id)
    return models


def extract_model_provider(meta: Dict[str, Any]) -> Optional[str]:
    usage = meta.get("model_usage")
    if isinstance(usage, list):
        for item in reversed(usage):
            if isinstance(item, dict) and item.get("model_provider_id"):
                return str(item["model_provider_id"])
    return None


def extract_cline_version(meta: Dict[str, Any]) -> Optional[str]:
    history = meta.get("environment_history")
    if isinstance(history, list):
        for item in reversed(history):
            if isinstance(item, dict) and item.get("cline_version"):
                return str(item["cline_version"])
    return None


def extract_environment(meta: Dict[str, Any]) -> Dict[str, Any]:
    history = meta.get("environment_history")
    if isinstance(history, list):
        for item in reversed(history):
            if isinstance(item, dict):
                return {
                    "os_name": item.get("os_name"),
                    "os_version": item.get("os_version"),
                    "os_arch": item.get("os_arch"),
                    "host_name": item.get("host_name"),
                    "host_version": item.get("host_version"),
                }
    return {}


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None
