"""配置加载：默认值 + 配置文件深合并 + 路径解析。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List

APP_NAME = "cline-usage-reporter"
DEFAULT_STATE_DIR = Path.home() / f".{APP_NAME}"

DEFAULTS: Dict[str, Any] = {
    "endpoint": {
        # 后台地址，例如 https://your-backend.com
        "url": "",
        # usage / skill 两类记录的上报路径，可分别配置
        "usage_path": "/api/usage/report",
        "skill_path": "/api/skill/report",
        # 鉴权：Bearer token（可留空）
        "token": "",
        # 额外请求头
        "headers": {},
        "timeout_seconds": 15,
        "retries": 3,
        # batch: 一次 POST 提交一批；single: 每条记录一次 POST
        "mode": "batch",
        # 单次批量上报的最大记录数
        "batch_size": 200,
    },
    "collect": {
        # 额外扫描的 globalStorage 根目录（绝对路径），用于自动发现之外的场景
        "ide_roots": [],
        # 是否上报任务描述文本；关闭则只上报 hash / 长度
        "include_task_text": True,
        "task_text_max_len": 200,
        # 是否上报 skill 使用记录
        "include_skill_records": True,
        # 是否兼容老版本 state.vscdb
        "include_legacy_vscdb": True,
        # cwd 前缀 -> 项目名的映射（子串匹配，命中最长的优先）
        "project_mapping": {},
        # 忽略这些 cwd 前缀下的任务
        "exclude_cwd_prefixes": [],
        # 忽略这些 skill 名（大小写不敏感）
        "ignored_skill_names": [],
        # 忽略这些模型（大小写不敏感，子串匹配）
        "ignored_models": [],
    },
    "terminal": {
        # 终端标识，留空则自动探测（scutil --get ComputerName / hostname）
        "id": "",
        "name": "",
    },
    "state": {
        # 增量状态与待上报队列存放目录
        "dir": str(DEFAULT_STATE_DIR),
    },
}


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并，override 优先。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def search_paths() -> List[Path]:
    """配置文件搜索顺序。"""
    paths: List[Path] = []
    env = os.environ.get("CLINE_REPORTER_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / f"{APP_NAME}.config.json")
    paths.append(Path.cwd() / "config.json")
    paths.append(DEFAULT_STATE_DIR / "config.json")
    return paths


def load_config(explicit: str | None = None) -> Dict[str, Any]:
    """加载配置。显式指定路径时若不存在则直接报错，避免静默跑错端点。"""
    cfg: Dict[str, Any] = {}
    used: Path | None = None

    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        used = path
    else:
        for path in search_paths():
            if path.is_file():
                cfg = json.loads(path.read_text(encoding="utf-8"))
                used = path
                break

    merged = deep_merge(DEFAULTS, _expand(cfg))
    merged["_config_path"] = str(used) if used else None
    return merged


def state_dir(config: Dict[str, Any]) -> Path:
    path = Path(str(config["state"]["dir"])).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def endpoint_url(config: Dict[str, Any], path_key: str) -> str:
    """拼接上报地址；若既没有 base url 也没有绝对路径，返回空串表示未配置。"""
    ep = config["endpoint"]
    base = str(ep.get("url") or "").rstrip("/")
    path = str(ep.get(path_key) or "")
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not base:
        return ""
    return f"{base}{path if path.startswith('/') else '/' + path}"
