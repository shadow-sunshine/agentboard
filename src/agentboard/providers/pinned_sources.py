"""固定来源计划 Provider（P6 真实研究接线）。

设计边界（任务书第 2 条的落地）：
- **检索摘要 / 仓库元数据不是技术证据**：本 Provider 不消费搜索结果条目，
  而是按**显式来源计划**（每候选 ≤N 个文件，repo + path + ref/commit）通过
  GitHub contents API 取**原文**（base64 解码后的完整文件字节），
  并把解析出的 commit 写进规范 source_url
  （`https://github.com/{repo}/blob/{40位commit}/{path}`）——
  任何人可用同一 URL 重新取回并重算 sha256 完成重验。
- **有界**：来源计划是显式 JSON（候选 → 文件清单），不做全网检索、
  不跟随链接、不扩大范围；HTTP 调用次数有计数（进运行审计）。
- **fail-closed**：ref 解析失败 / 文件缺失 / 非 base64 编码（>1MB 的文件
  contents API 不返回内容）/ 声明 commit 与解析结果不符——全部抛 providers
  异常族（runner 的可重试闭集接管），**绝不静默降级或返回占位内容**。
- **主机限制**：只允许 api.github.com（经 HttpTransport 主机白名单强制）；
  不访问 localhost / 私网 / 任意主机。

与 P2.1 的关系：GitHub 仓库检索 Provider（发现元数据）继续可用；
本模块解决的是"证据必须有可重验原文"这一 P6 要求。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..research.snapshot import expected_excerpt_for
from .errors import ContentTooLargeError, SourceHTTPError, SourceResponseError, TransportError
from .interface import (
    RetrievedDocument,
    SearchQuery,
    Transport,
    TransportRequest,
)

API_HOST = "api.github.com"
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
#: 规范 source_url：固定 commit 的 blob 页（无 query / fragment）。
MAX_FILE_BYTES = 1 << 20

PROVIDER_ID = "github.pinned_sources.v1"


@dataclass(frozen=True, slots=True)
class SourceFileSpec:
    """一个候选的一件来源文件（repo + path + ref 或显式 commit）。"""

    repo: str
    path: str
    ref: str = "HEAD"
    commit: str = ""  # 显式固定（40 位十六进制）；为空则运行时解析 ref

    def __post_init__(self) -> None:
        for label, value in (("repo", self.repo), ("path", self.path), ("ref", self.ref)):
            if not isinstance(value, str) or not value.strip():
                raise TransportError(f"SourceFileSpec.{label} 必须是非空字符串")
        if self.commit and not COMMIT_SHA_PATTERN.fullmatch(self.commit):
            raise TransportError(
                f"SourceFileSpec.commit 必须是 40 位十六进制（留空则运行时解析），"
                f"实际 {self.commit!r}"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise TransportError("repo 必须是 owner/repo")
        if (self.path.startswith("/") or "\\" in self.path or
                any(part in ("", ".", "..") for part in self.path.split("/")) or
                not re.fullmatch(r"[A-Za-z0-9_.\-/]+", self.path)):
            raise TransportError("path 必须是安全的仓库相对路径")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", self.ref):
            raise TransportError("ref 含非法字符")

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.repo, self.path, self.commit or self.ref)

    def to_dict(self) -> dict[str, str]:
        return {
            "commit": self.commit,
            "path": self.path,
            "ref": self.ref,
            "repo": self.repo,
        }


def parse_source_plan(raw: Mapping[str, Any]) -> dict[str, tuple[SourceFileSpec, ...]]:
    """解析来源计划 JSON（候选 → 文件清单；闭集字段，fail-closed）。

    形态::

        {"candidates": {"pgvector": [{"repo": "...", "path": "...", "ref": "...",
                                      "commit": "(可选)"}], ...}}

    每候选至少 1 件、至多 4 件（有界）；未知字段拒绝。
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), dict):
        raise TransportError("来源计划必须含 candidates 对象")
    if set(raw) != {"candidates"}:
        raise TransportError("来源计划含未知字段")
    plan: dict[str, tuple[SourceFileSpec, ...]] = {}
    for candidate_id, entries in raw["candidates"].items():
        if not isinstance(candidate_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", candidate_id):
            raise TransportError("来源计划的候选标识必须是非空字符串")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 4:
            raise TransportError(
                f"候选 {candidate_id!r} 的来源文件必须是 1..4 件（有界），"
                f"实际 {len(entries) if isinstance(entries, list) else type(entries).__name__}"
            )
        specs: list[SourceFileSpec] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise TransportError(f"候选 {candidate_id!r} 的来源条目必须是对象")
            unknown = sorted(set(entry) - {"repo", "path", "ref", "commit"})
            if unknown:
                raise TransportError(
                    f"候选 {candidate_id!r} 的来源条目含未知字段 {unknown}"
                )
            if any(not isinstance(value, str) for value in entry.values()):
                raise TransportError("来源条目的字段必须是字符串")
            specs.append(
                SourceFileSpec(
                    repo=str(entry.get("repo", "")),
                    path=str(entry.get("path", "")),
                    ref=str(entry.get("ref") or "HEAD"),
                    commit=str(entry.get("commit") or ""),
                )
            )
        plan[candidate_id] = tuple(specs)
    if not plan:
        raise TransportError("来源计划不能为空")
    return plan


@dataclass(frozen=True, slots=True)
class SourceAuditEntry:
    """一件已取回原文的审计记录（进运行审计与 verifier 复核）。"""

    candidate_id: str
    repo: str
    path: str
    commit: str
    source_url: str
    content_sha256: str
    byte_size: int
    blob_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "byte_size": self.byte_size,
            "blob_sha": self.blob_sha,
            "commit": self.commit,
            "content_sha256": self.content_sha256,
            "candidate_id": self.candidate_id,
            "path": self.path,
            "repo": self.repo,
            "source_url": self.source_url,
        }


class PinnedSourceProvider:
    """按显式来源计划取真实原文的 Provider（实现既有 Provider 协议）。

    查询词形如 `{candidate_id} {dimension_id}`（runtime 计划生成）；
    只有首词命中计划内候选时才返回该候选的原文文档，否则返回空
    （⇒ runner 走缺口补检 / 显式 unsupported，不编造）。
    同一 (repo, path, commit) 在一次运行内只取一次（缓存是记录，不改变内容）。
    """

    def __init__(
        self,
        *,
        source_plan: Mapping[str, Sequence[SourceFileSpec]],
        transport: Transport,
        provider_id: str = PROVIDER_ID,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._plan = {cid: tuple(specs) for cid, specs in source_plan.items()}
        if not self._plan:
            raise TransportError("PinnedSourceProvider 需要非空来源计划")
        self._transport = transport
        self.provider_id = provider_id
        if (isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0):
            raise TransportError("timeout_seconds 必须为有限正数")
        self._timeout = timeout_seconds
        self.http_calls = 0
        # (repo, path, commit_or_ref) → (commit, content_text)；缓存只影响重复请求次数
        self._content_cache: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        self.audit: list[SourceAuditEntry] = []
        self._materials: dict[tuple[str, str, str, str], tuple[SourceAuditEntry, str]] = {}

    # ---- Provider 协议 ----

    def search(
        self,
        query: SearchQuery,
        *,
        captured_at: str,
        timeout_seconds: float | None = None,
    ) -> Sequence[RetrievedDocument]:
        candidate_id = query.terms.split()[0] if query.terms.strip() else ""
        specs = self._plan.get(candidate_id)
        if not specs:
            return ()  # 计划外候选 ⇒ 零结果（不编造）
        # 运行时预算透传的超时（P3 纪律）：取 min(自身配置, 预算值)。
        if timeout_seconds is not None:
            self._timeout = min(self._timeout, float(timeout_seconds))
        return tuple(
            self._fetch_document(candidate_id, spec, query, captured_at) for spec in specs
        )

    # ---- 内部步骤（每步可测） ----

    def _get_json(self, url: str) -> dict[str, Any]:
        self.http_calls += 1
        response = self._transport.send(
            TransportRequest(
                url=url,
                headers=self._headers(),
                timeout_seconds=self._timeout,
                max_response_bytes=MAX_FILE_BYTES * 4,
            )
        )
        if response.status != 200:
            raise SourceHTTPError(response.status, url)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceResponseError(f"{url} 的响应不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise SourceResponseError(f"{url} 的响应顶层不是对象")
        if "message" in payload and "content" not in payload and "sha" not in payload:
            # GitHub 错误响应（rate limit / not found 等）显式失败
            raise SourceResponseError(f"{url} 返回错误：{payload.get('message')!r}")
        return payload

    def resolve_commit(self, spec: SourceFileSpec) -> str:
        """ref/commit → 40 位 commit。

        显式声明 commit 时也做一次解析核对：GitHub 对该 ref 的解析结果与
        声明不符 ⇒ 来源不匹配，显式失败（fail-closed）。
        """
        cached = self._content_cache.get((spec.repo, spec.path, spec.ref))
        if cached is not None and not spec.commit:
            return cached[0]
        ref = spec.commit or spec.ref
        url = f"https://api.github.com/repos/{spec.repo}/commits/{ref}"
        payload = self._get_json(url)
        sha = payload.get("sha")
        if not isinstance(sha, str) or not COMMIT_SHA_PATTERN.match(sha):
            raise SourceResponseError(f"{url} 未返回合法 commit sha：{sha!r}")
        if spec.commit and sha != spec.commit:
            raise SourceResponseError(
                f"{url} 解析到的 commit {sha} 与声明的 {spec.commit} 不符"
                "（来源不匹配，fail-closed）"
            )
        return sha

    def fetch_content(self, spec: SourceFileSpec) -> tuple[str, str, str]:
        """返回 (commit, content_text, blob_sha)；同键缓存（只影响请求次数）。"""
        key = (spec.repo, spec.path, spec.commit or spec.ref)
        cached = self._content_cache.get(key)
        if cached is not None:
            return cached
        commit = self.resolve_commit(spec)
        url = (
            f"https://api.github.com/repos/{spec.repo}/contents/{spec.path}"
            f"?ref={commit}"
        )
        self.http_calls += 1
        response = self._transport.send(
            TransportRequest(
                url=url,
                headers=self._headers(),
                timeout_seconds=self._timeout,
                max_response_bytes=MAX_FILE_BYTES * 4,
            )
        )
        if response.status != 200:
            raise SourceHTTPError(response.status, url)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceResponseError(f"{url} 的响应不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise SourceResponseError("contents 响应顶层必须是对象")
        encoding = payload.get("encoding")
        encoded = payload.get("content")
        if encoding != "base64" or not isinstance(encoded, str) or not encoded:
            # contents API 对 >1MB 文件返回 encoding=none（无内容）⇒ 显式失败
            raise ContentTooLargeError(
                f"{url} 未返回可解码内容（encoding={encoding!r}）；"
                "文件可能超过 contents API 的 1MB 限制——请换更小的来源文件"
            )
        try:
            raw = base64.b64decode("".join(encoded.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SourceResponseError(f"{url} 的 base64 内容解码失败：{exc}") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise ContentTooLargeError(
                f"{url} 的原文超过 {MAX_FILE_BYTES} 字节上限"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceResponseError(f"{url} 的原文不是合法 UTF-8：{exc}") from exc
        blob_sha = payload.get("sha")
        actual_blob = hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
        if not isinstance(blob_sha, str) or blob_sha != actual_blob:
            raise SourceResponseError("Git blob SHA 与原文字节不符")
        if "path" in payload and payload["path"] != spec.path:
            raise SourceResponseError("返回路径与来源计划不符")
        if "size" in payload and (type(payload["size"]) is not int or payload["size"] != len(raw)):
            raise SourceResponseError("返回文件大小与原文字节不符")
        # 声明 commit 与解析结果不符 ⇒ 显式失败（来源不匹配 fail-closed）
        if spec.commit and spec.commit != commit:
            raise SourceResponseError(
                f"{spec.repo}/{spec.path}：解析到的 commit {commit} 与声明的 "
                f"{spec.commit} 不符（来源不匹配，fail-closed）"
            )
        self._content_cache[key] = (commit, text, blob_sha)
        return commit, text, blob_sha

    def _fetch_document(
        self,
        candidate_id: str,
        spec: SourceFileSpec,
        query: SearchQuery,
        captured_at: str,
    ) -> RetrievedDocument:
        commit, text, blob_sha = self.fetch_content(spec)
        source_url = f"https://github.com/{spec.repo}/blob/{commit}/{spec.path}"
        entry = SourceAuditEntry(
                candidate_id=candidate_id,
                repo=spec.repo,
                path=spec.path,
                commit=commit,
                source_url=source_url,
                content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                byte_size=len(text.encode("utf-8")),
                blob_sha=blob_sha,
            )
        self.audit.append(entry)
        self._materials[(candidate_id, spec.repo, spec.path, commit)] = (entry, text)
        return RetrievedDocument.build(
            source_id=f"{self.provider_id}:{candidate_id}:{spec.repo}/{spec.path}@{commit[:12]}",
            query_terms=query.normalized_terms,
            source_url=source_url,
            title=f"{spec.repo}/{spec.path}@{commit[:12]}",
            captured_at=captured_at,
            content=text,
        )

    @staticmethod
    def _headers() -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def verify_report_sources(self, report: dict[str, Any], audit: Any) -> tuple[bool, list[str]]:
        """从本次实际取回的原文重算，不采信 researcher 的来源声明。"""
        reasons: list[str] = []
        if audit != self.source_audit():
            reasons.append("来源审计与本次实际取回的原文集合不一致")
        if set(report.get("candidate_ids", [])) != set(self._plan):
            reasons.append("报告候选与显式来源计划不一致")
        by_candidate: dict[str, list[tuple[SourceAuditEntry, str]]] = {}
        for entry, text in self._materials.values():
            raw = text.encode("utf-8")
            measured_sha = hashlib.sha256(raw).hexdigest()
            measured_blob = hashlib.sha1(
                f"blob {len(raw)}\0".encode("ascii") + raw
            ).hexdigest()
            if (entry.content_sha256 != measured_sha or entry.blob_sha != measured_blob
                    or entry.byte_size != len(raw)):
                reasons.append(
                    f"{entry.candidate_id}/{entry.path}: 来源审计摘要与本次原文字节不符"
                )
            by_candidate.setdefault(entry.candidate_id, []).append((entry, text))
        # 与 CorpusBuilder 相同：按内容摘要去重，再按摘要和 URL 排序。
        for finding in report.get("findings", []):
            candidate = finding.get("candidate_id")
            unique: dict[str, tuple[SourceAuditEntry, str]] = {}
            for entry, text in by_candidate.get(candidate, []):
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                unique.setdefault(digest, (entry, text))
            ordered = sorted(unique.values(), key=lambda item: (
                hashlib.sha256(item[1].encode("utf-8")).hexdigest(), item[0].source_url
            ))
            for ref in finding.get("evidence_refs", []):
                locator = ref.get("locator", "")
                match = re.fullmatch(r"/sections/(\d+)/text", locator)
                index = int(match[1]) if match else -1
                valid = (
                    ref.get("source_type") == "fixture_snapshot"
                    and ref.get("source_id") == f"fixture:{candidate}"
                    and 0 <= index < len(ordered)
                    and ref.get("excerpt") == expected_excerpt_for(ordered[index][1])
                )
                if not valid:
                    reasons.append(
                        f"{candidate}/{finding.get('dimension_id')}: 证据与实际原文不符"
                    )
        return not reasons, reasons

    def source_audit(self) -> list[dict[str, Any]]:
        """去重后的来源审计（同键只保留一条；排序确定）。"""
        seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for entry in self.audit:
            key = (entry.candidate_id, entry.repo, entry.path, entry.commit)
            seen[key] = entry.to_dict()
        return [seen[key] for key in sorted(seen)]


__all__ = [
    "API_HOST",
    "COMMIT_SHA_PATTERN",
    "PROVIDER_ID",
    "PinnedSourceProvider",
    "SourceAuditEntry",
    "SourceFileSpec",
    "parse_source_plan",
]
