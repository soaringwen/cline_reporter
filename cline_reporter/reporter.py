"""上报器：HTTP 发送、批量/单条模式、重试与错误归类。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import endpoint_url


@dataclass
class SendResult:
    ok: bool
    status: Optional[int] = None
    body: str = ""
    error: str = ""
    sent: int = 0


@dataclass
class ReportOutcome:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    results: List[SendResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
        }


class Reporter:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.ep = config.get("endpoint", {})
        self.timeout = float(self.ep.get("timeout_seconds") or 15)
        self.retries = max(0, int(self.ep.get("retries") or 0))
        self.mode = str(self.ep.get("mode") or "batch").lower()
        self.batch_size = max(1, int(self.ep.get("batch_size") or 200))

    # ---------------- 请求 ---------------- #
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "cline-usage-reporter/1.0",
        }
        token = str(self.ep.get("token") or "").strip()
        if token:
            headers["Authorization"] = (
                token if token.lower().startswith("bearer ") else f"Bearer {token}"
            )
        extra = self.ep.get("headers") or {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key:
                    headers[str(key)] = str(value)
        return headers

    def _post(self, url: str, payload: Dict[str, Any]) -> SendResult:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = ""
        last_status: Optional[int] = None
        last_body = ""

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                url, data=data, headers=self._headers(), method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    status = getattr(response, "status", 200)
                    if 200 <= status < 300:
                        return SendResult(ok=True, status=status, body=body[:500])
                    last_error = f"HTTP {status}"
                    last_status, last_body = status, body[:500]
            except urllib.error.HTTPError as exc:
                # 4xx 属客户端问题，重试无意义
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    body = ""
                last_status, last_body = exc.code, body
                last_error = f"HTTP {exc.code}"
                if 400 <= exc.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))

        return SendResult(
            ok=False, status=last_status, body=last_body, error=last_error or "unknown error"
        )

    # ---------------- 上报编排 ---------------- #
    def url_for(self, kind: str) -> str:
        key = "skill_path" if kind == "skill" else "usage_path"
        url = endpoint_url(self.config, key)
        if not url and kind == "skill":
            url = endpoint_url(self.config, "usage_path")
        return url

    @staticmethod
    def _chunks(items: List[Dict[str, Any]], size: int):
        for index in range(0, len(items), size):
            yield items[index : index + size]

    def send_payloads(
        self,
        kind: str,
        payloads: List[Dict[str, Any]],
        dry_run: bool = False,
        hostname: str = "",
        terminal_id: str = "",
    ) -> List[Tuple[List[Dict[str, Any]], SendResult]]:
        """按 kind 发送一批 payload。

        返回 [(本批 payload 切片, 结果)]，调用方可据此精确标记成功/失败。
        """
        if not payloads:
            return []

        url = self.url_for(kind)

        # dry-run 先于地址校验：即使未配置后台也要能预览将要发送的内容
        if dry_run:
            return [
                (
                    payloads,
                    SendResult(
                        ok=True,
                        sent=len(payloads),
                        body=(
                            f"[dry-run] would POST {len(payloads)} {kind} record(s) to "
                            f"{url or '（未配置上报地址）'}"
                        ),
                    ),
                )
            ]

        if not url:
            message = f"未配置 {kind} 上报地址（endpoint.url 为空）"
            return [(payloads, SendResult(ok=False, error=message, sent=len(payloads)))]

        if self.mode == "single":
            results: List[Tuple[List[Dict[str, Any]], SendResult]] = []
            for payload in payloads:
                results.append(([payload], self._post(url, payload)))
            return results

        results = []
        for chunk in self._chunks(payloads, self.batch_size):
            envelope = {
                "report_type": f"cline_{kind}_batch",
                "schema_version": 1,
                "hostname": hostname,
                "terminal_id": terminal_id,
                "sent_at": int(time.time() * 1000),
                "records": chunk,
            }
            result = self._post(url, envelope)
            result.sent = len(chunk)
            results.append((chunk, result))
        return results

    def report(
        self,
        usage_payloads: List[Dict[str, Any]],
        skill_payloads: List[Dict[str, Any]],
        dry_run: bool = False,
        hostname: str = "",
        terminal_id: str = "",
    ) -> ReportOutcome:
        """便捷入口：一次性上报 usage + skill，仅返回汇总结果。"""
        outcome = ReportOutcome(total=len(usage_payloads) + len(skill_payloads))
        for kind, payloads in (("usage", usage_payloads), ("skill", skill_payloads)):
            for _chunk, result in self.send_payloads(
                kind, payloads, dry_run=dry_run, hostname=hostname, terminal_id=terminal_id
            ):
                outcome.results.append(result)
                if result.ok:
                    outcome.succeeded += result.sent
                else:
                    outcome.failed += result.sent
                    outcome.errors.append(f"{kind} {result.error}")
        return outcome

    def resolve_urls(self) -> Dict[str, str]:
        return {
            "usage": endpoint_url(self.config, "usage_path"),
            "skill": endpoint_url(self.config, "skill_path"),
        }

    @staticmethod
    def normalize_url(url: str) -> str:
        return urllib.parse.urlsplit(url).geturl() if url else url
