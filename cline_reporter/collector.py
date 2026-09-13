"""采集与归一化：把多来源原始数据合并为统一的上报记录。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import SCHEMA_VERSION
from .config import APP_NAME
from .locate import ClineStorage, discover_storages
from .sources import (
    extract_cline_version,
    extract_environment,
    extract_model_provider,
    extract_models,
    extract_skill_paths,
    read_legacy_task_history,
    read_task_history,
    read_task_metadata,
)

UNKNOWN = "unknown"


def detect_terminal() -> Tuple[str, str]:
    """返回 (terminal_id, terminal_name)。

    terminal_name 取人类可读的机器名；terminal_id 取稳定的短标识。
    """
    env_id = os.environ.get("CLINE_REPORTER_TERMINAL_ID", "").strip()
    env_name = os.environ.get("CLINE_REPORTER_TERMINAL_NAME", "").strip()

    name = env_name
    if not name and sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            name = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
    if not name:
        name = platform.node() or UNKNOWN

    terminal_id = env_id
    if not terminal_id:
        local = ""
        if sys.platform == "darwin":
            try:
                out = subprocess.run(
                    ["scutil", "--get", "LocalHostName"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                local = out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                local = ""
        if not local:
            local = platform.node().split(".")[0]
        terminal_id = local or UNKNOWN

    return terminal_id, name


@dataclass
class UsageRecord:
    """一条任务级 token 用量记录。"""

    terminal_id: str
    hostname: str
    ide_type: str
    ide_app: str
    source: str  # taskHistory.json | state.vscdb
    task_id: str
    task_ulid: Optional[str]
    task_text: Optional[str]
    task_text_hash: Optional[str]
    ts: Optional[int]  # 毫秒
    model: Optional[str]
    model_provider: Optional[str]
    models_used: List[str]
    cwd: Optional[str]
    project: Optional[str]
    tokens_in: int = 0
    tokens_out: int = 0
    cache_writes: int = 0
    cache_reads: int = 0
    total_cost: float = 0.0
    size: int = 0
    is_favorited: bool = False
    cline_version: Optional[str] = None
    environment: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    record_type: str = "usage"

    @property
    def key(self) -> str:
        return f"usage|{self.ide_type}|{self.task_id}"

    def fingerprint(self) -> str:
        """仅对会变化的语义字段取指纹，用于增量判断。"""
        payload = json.dumps(
            [
                self.ts,
                self.tokens_in,
                self.tokens_out,
                self.cache_writes,
                self.cache_reads,
                self.total_cost,
                self.size,
                self.model,
                self.cwd,
                self.is_favorited,
                self.task_text,
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def to_payload(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("record_type", None)
        data["report_type"] = "cline_usage"
        return data


@dataclass
class SkillRecord:
    """一条 skill 使用记录。"""

    terminal_id: str
    hostname: str
    ide_type: str
    task_id: str
    skill: str
    used_at: Optional[int]  # 毫秒
    file_count: int
    files: List[str]
    record_sources: List[str]
    schema_version: int = SCHEMA_VERSION

    @property
    def key(self) -> str:
        return f"skill|{self.ide_type}|{self.task_id}|{self.skill}"

    def fingerprint(self) -> str:
        payload = json.dumps(
            [self.used_at, self.file_count, sorted(self.files)],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "report_type": "cline_skill_usage",
            "schema_version": self.schema_version,
            "terminal_id": self.terminal_id,
            "hostname": self.hostname,
            "ide_type": self.ide_type,
            "task_id": self.task_id,
            "skill": self.skill,
            "used_at": self.used_at,
            "file_count": self.file_count,
            "files": self.files,
            "record_sources": self.record_sources,
        }


@dataclass
class CollectResult:
    usage: List[UsageRecord] = field(default_factory=list)
    skills: List[SkillRecord] = field(default_factory=list)
    storages: List[ClineStorage] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    terminal_id: str = ""
    hostname: str = ""

    def summary(self) -> Dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "hostname": self.hostname,
            "storages": [s.describe() for s in self.storages],
            "usage_records": len(self.usage),
            "skill_records": len(self.skills),
            "warnings": self.warnings,
        }


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _as_opt_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_project(cwd: Optional[str], mapping: Dict[str, str]) -> Optional[str]:
    """按配置映射解析项目名，未命中则用 cwd 的 basename。"""
    if not cwd:
        return None
    best_key = ""
    for pattern, name in (mapping or {}).items():
        if pattern and pattern in cwd and len(pattern) > len(best_key):
            best_key = pattern
            best_name = name
    if best_key:
        return str(best_name)
    base = os.path.basename(cwd.rstrip("/\\"))
    return base or cwd


def _is_excluded(cwd: Optional[str], prefixes: List[str]) -> bool:
    if not cwd:
        return False
    for prefix in prefixes or []:
        if prefix and cwd.startswith(prefix):
            return True
    return False


def _is_ignored_model(model: Optional[str], patterns: List[str]) -> bool:
    if not model:
        return False
    lowered = model.lower()
    return any(p and p.lower() in lowered for p in patterns or [])


def _truncate_text(text: Optional[str], enabled: bool, max_len: int) -> Tuple[Optional[str], Optional[str]]:
    """返回 (上报文本, 文本 hash)。文本始终算 hash，便于后台做去重/关联。"""
    if not text:
        return None, None
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if not enabled:
        return None, digest
    limit = max_len if isinstance(max_len, int) and max_len > 0 else 0
    if limit and len(text) > limit:
        return text[:limit], digest
    return text, digest


def collect(config: Dict[str, Any]) -> CollectResult:
    """执行一次完整采集。"""
    cfg_collect = config.get("collect", {})
    terminal_id, hostname = detect_terminal()
    if config.get("terminal", {}).get("id"):
        terminal_id = str(config["terminal"]["id"])
    if config.get("terminal", {}).get("name"):
        hostname = str(config["terminal"]["name"])

    result = CollectResult(terminal_id=terminal_id, hostname=hostname)

    storages = discover_storages(cfg_collect.get("ide_roots"))
    result.storages = storages
    if not storages:
        result.warnings.append(
            "未发现任何 Cline 存储目录，请用 collect.ide_roots 手动指定 globalStorage 路径"
        )
        return result

    project_mapping = cfg_collect.get("project_mapping") or {}
    exclude_prefixes = cfg_collect.get("exclude_cwd_prefixes") or []
    ignored_skills = {s.lower() for s in (cfg_collect.get("ignored_skill_names") or [])}
    ignored_models = cfg_collect.get("ignored_models") or []
    include_task_text = bool(cfg_collect.get("include_task_text", True))
    text_max_len = int(cfg_collect.get("task_text_max_len") or 0)
    include_skills = bool(cfg_collect.get("include_skill_records", True))

    for storage in storages:
        raw_records: List[Dict[str, Any]] = []
        source_name = ""

        history = read_task_history(storage.task_history_file)
        if history:
            raw_records = history
            source_name = "taskHistory.json"
        elif cfg_collect.get("include_legacy_vscdb", True):
            history = read_legacy_task_history(storage.vscdb_file)
            if history:
                raw_records = history
                source_name = "state.vscdb"

        if not raw_records:
            result.warnings.append(f"{storage.ide_app}: 未找到任务历史数据，已跳过")
            continue

        for raw in raw_records:
            task_id = _as_str(raw.get("id")) or _as_str(raw.get("taskId"))
            if not task_id:
                continue

            cwd = _as_str(raw.get("cwdOnTaskInitialization")) or _as_str(raw.get("cwd"))
            if _is_excluded(cwd, exclude_prefixes):
                continue

            meta = read_task_metadata(storage.tasks_dir, task_id) or {}
            models_used = extract_models(meta)

            model = _as_str(raw.get("modelId")) or (models_used[-1] if models_used else None)
            if _is_ignored_model(model, ignored_models):
                continue

            task_text_raw = _as_str(raw.get("task"))
            task_text, task_text_hash = _truncate_text(
                task_text_raw, include_task_text, text_max_len
            )

            record = UsageRecord(
                terminal_id=terminal_id,
                hostname=hostname,
                ide_type=storage.ide_type,
                ide_app=storage.ide_app,
                source=source_name,
                task_id=task_id,
                task_ulid=_as_str(raw.get("ulid")),
                task_text=task_text,
                task_text_hash=task_text_hash,
                ts=_as_opt_int(raw.get("ts")),
                model=model,
                model_provider=extract_model_provider(meta),
                models_used=models_used,
                cwd=cwd,
                project=resolve_project(cwd, project_mapping),
                tokens_in=_as_int(raw.get("tokensIn")),
                tokens_out=_as_int(raw.get("tokensOut")),
                cache_writes=_as_int(raw.get("cacheWrites")),
                cache_reads=_as_int(raw.get("cacheReads")),
                total_cost=_as_float(raw.get("totalCost")),
                size=_as_int(raw.get("size")),
                is_favorited=bool(raw.get("isFavorited", False)),
                cline_version=extract_cline_version(meta),
                environment=extract_environment(meta),
            )
            result.usage.append(record)

            if include_skills:
                for skill_info in extract_skill_paths(meta):
                    if skill_info["skill"].lower() in ignored_skills:
                        continue
                    result.skills.append(
                        SkillRecord(
                            terminal_id=terminal_id,
                            hostname=hostname,
                            ide_type=storage.ide_type,
                            task_id=task_id,
                            skill=skill_info["skill"],
                            used_at=skill_info["first_read_ts"] or record.ts,
                            file_count=len(skill_info["files"]),
                            files=skill_info["files"],
                            record_sources=skill_info["record_sources"],
                        )
                    )

    result.usage.sort(key=lambda r: (r.ts or 0, r.task_id))
    result.skills.sort(key=lambda r: (r.used_at or 0, r.skill))
    return result


def default_export_path(name: str) -> Path:
    return Path.home() / f".{APP_NAME}" / name
