"""增量状态：已上报指纹记录 + 失败待重试队列。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

STATE_FILENAME = "state.json"
PENDING_FILENAME = "pending.jsonl"

# 已上报键的保留上限，避免状态文件无限增长（超出后按时间淘汰最旧记录）
MAX_TRACKED_KEYS = 20000


@dataclass
class ReportState:
    """记录每条记录的上报指纹，实现「有变化才重报」。"""

    directory: Path
    reported: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _dirty: bool = False

    # ---------------- 持久化 ---------------- #
    @property
    def path(self) -> Path:
        return self.directory / STATE_FILENAME

    @property
    def pending_path(self) -> Path:
        return self.directory / PENDING_FILENAME

    @classmethod
    def load(cls, directory: Path) -> "ReportState":
        directory.mkdir(parents=True, exist_ok=True)
        instance = cls(directory=directory)
        if instance.path.is_file():
            try:
                data = json.loads(instance.path.read_text(encoding="utf-8"))
                reported = data.get("reported")
                if isinstance(reported, dict):
                    instance.reported = {
                        str(k): v for k, v in reported.items() if isinstance(v, dict)
                    }
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                instance.reported = {}
        return instance

    def save(self) -> None:
        if not self._dirty:
            return
        self._trim()
        payload = {
            "version": 1,
            "updated_at": int(time.time()),
            "reported": self.reported,
        }
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        self._dirty = False

    def _trim(self) -> None:
        if len(self.reported) <= MAX_TRACKED_KEYS:
            return
        ordered = sorted(
            self.reported.items(), key=lambda kv: kv[1].get("at", 0), reverse=True
        )
        self.reported = dict(ordered[:MAX_TRACKED_KEYS])

    # ---------------- 增量判断 ---------------- #
    def is_reported(self, key: str, fingerprint: str) -> bool:
        entry = self.reported.get(key)
        return bool(entry) and entry.get("fp") == fingerprint

    def mark_reported(self, key: str, fingerprint: str, target: str = "") -> None:
        self.reported[key] = {"fp": fingerprint, "at": int(time.time()), "target": target}
        self._dirty = True

    def forget(self, key: str) -> None:
        if key in self.reported:
            del self.reported[key]
            self._dirty = True

    # ---------------- 待上报队列 ---------------- #
    def append_pending(self, items: Iterable[Dict[str, Any]]) -> int:
        """把发送失败的数据落到本地队列，下次运行优先重试。"""
        items = [item for item in items if isinstance(item, dict)]
        if not items:
            return 0
        with self.pending_path.open("a", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return len(items)

    def read_pending(self) -> List[Dict[str, Any]]:
        if not self.pending_path.is_file():
            return []
        out: List[Dict[str, Any]] = []
        try:
            with self.pending_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        out.append(parsed)
        except (OSError, UnicodeDecodeError):
            return []
        return out

    def clear_pending(self) -> None:
        try:
            self.pending_path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self._dirty = True

    def rewrite_pending(self, items: List[Dict[str, Any]]) -> None:
        self.clear_pending()
        if items:
            self.append_pending(items)


def _atomic_write(path: Path, content: str) -> None:
    """先写临时文件再替换，避免中断导致状态文件损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def select_new(
    records: List[Any],
    state: ReportState,
    limit: Optional[int] = None,
) -> List[Any]:
    """筛出「未上报」或「内容已变化」的记录（记录需实现 key / fingerprint）。"""
    fresh = [r for r in records if not state.is_reported(r.key, r.fingerprint())]
    if limit and limit > 0:
        return fresh[:limit]
    return fresh
