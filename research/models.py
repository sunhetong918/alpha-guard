"""Strict evidence and research contracts, separate from executable rule signals."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Self, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Horizon = Literal["short", "swing", "long"]
ResearchMarket = Literal["US", "HK", "CN"]
ContentKind = Literal["metadata", "summary", "article", "transcript"]
Completeness = Literal["full", "partial", "metadata"]
ResearchAction = Literal["buy_candidate", "watch", "hold_review", "reduce_review", "insufficient_evidence"]
ResearchStatus = Literal["ready", "waiting_content", "needs_configuration", "invalid_output", "failed", "insufficient_evidence", "expired"]
Platform = Literal["youtube", "bilibili", "douyin", "xiaohongshu", "rss"]


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              str_strip_whitespace=True, allow_inf_nan=False)


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research timestamps require a timezone")
    return value.astimezone(UTC)


def parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return aware(value if isinstance(value, datetime)
                     else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def canonical_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("a credential-free HTTP(S) source URL is required")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query)
                             if not k.lower().startswith("utm_")
                             and k.lower() not in {"fbclid", "gclid"}))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                       parsed.path or "/", query, ""))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


class ResearchInstrument(ResearchModel):
    ticker: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str = Field(default="", max_length=200)
    market: ResearchMarket
    exchange: str = Field(default="", max_length=32)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()

    @property
    def key(self) -> str:
        return f"{self.market}:{self.ticker}"


class Subscription(ResearchModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    platform: Platform
    url: str = Field(min_length=1, max_length=2048)
    label: str = Field(default="", max_length=200)
    enabled: bool = True
    feed_url: str | None = Field(default=None, max_length=2048)

    @field_validator("url", "feed_url")
    @classmethod
    def check_url(cls, value: str | None) -> str | None:
        if value is not None:
            canonical_url(value)
        return value


class TranscriptSegment(ResearchModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    text: str = Field(min_length=1, max_length=20_000)
    speaker: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def check_span(self) -> Self:
        if self.end_seconds is not None and self.end_seconds < self.start_seconds:
            raise ValueError("transcript end precedes start")
        return self


class EvidenceRecord(ResearchModel):
    id: str
    source_identity: str
    external_id: str = Field(default="", max_length=256)
    source_url: str = Field(default="", max_length=2048)
    title: str = Field(min_length=1, max_length=1000)
    author: str = Field(default="", max_length=300)
    platform: str = Field(default="news", max_length=80)
    published_at: datetime | None = None
    discovered_at: datetime
    retrieved_at: datetime
    origin_published_at: datetime | None = None
    cluster_first_seen_at: datetime | None = None
    summary: str = Field(default="", max_length=20_000)
    content_text: str = Field(default="", max_length=500_000)
    content_kind: ContentKind = "metadata"
    completeness: Completeness = "metadata"
    segments: list[TranscriptSegment] = Field(default_factory=list, max_length=10_000)
    quality_issues: list[str] = Field(default_factory=list, max_length=100)
    content_hash: str
    source_cluster: str

    @field_validator("published_at", "discovered_at", "retrieved_at", "origin_published_at", "cluster_first_seen_at")
    @classmethod
    def check_time(cls, value: datetime | None) -> datetime | None:
        return aware(value) if value is not None else None

    @field_validator("source_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        canonical_url(value)
        return value

    @property
    def event_at(self) -> datetime | None:
        if "original_publication_time_unknown" in self.quality_issues:
            return None
        return self.origin_published_at or self.published_at

    @property
    def research_ready(self) -> bool:
        return (bool(self.source_url) and self.completeness == "full"
                and self.content_kind in {"article", "transcript"}
                and len(self.content_text.strip()) >= 40
                and (self.content_kind != "transcript" or bool(self.segments)))


class ExtractedClaim(ResearchModel):
    kind: Literal["fact", "opinion", "inference"]
    text: str = Field(min_length=1, max_length=1600)
    quote: str = Field(min_length=1, max_length=1200)
    segment_index: int | None = Field(default=None, ge=0)
    ticker: str = Field(min_length=1, max_length=80)
    direction: Literal["positive", "negative", "neutral"]
    relationship: str = Field(min_length=1, max_length=1200)


class ExtractedThesis(ResearchModel):
    ticker: str = Field(min_length=1, max_length=80)
    horizon: Horizon
    direction: Literal["positive", "negative", "neutral"]
    claim_indices: list[int] = Field(min_length=1, max_length=1000)
    thesis: str = Field(min_length=1, max_length=40_000)
    catalysts: list[str] = Field(default_factory=list, max_length=200)
    counter_evidence: list[str] = Field(min_length=1, max_length=200)
    conditions: list[str] = Field(min_length=1, max_length=200)
    invalidation: list[str] = Field(min_length=1, max_length=200)
    verification_steps: list[str] = Field(min_length=1, max_length=200)

    @field_validator("catalysts", "counter_evidence", "conditions", "invalidation", "verification_steps")
    @classmethod
    def bound_text(cls, values: list[str]) -> list[str]:
        if any(not v.strip() or len(v) > 1200 for v in values):
            raise ValueError("thesis text must be nonempty and bounded")
        return values


class ExtractionResult(ResearchModel):
    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=1000)
    theses: list[ExtractedThesis] = Field(default_factory=list, max_length=300)


class Citation(ResearchModel):
    evidence_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1, max_length=2048)
    quote: str = Field(min_length=1, max_length=1200)
    start_seconds: float | None = None
    end_seconds: float | None = None
    speaker: str = ""

    @field_validator("source_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        canonical_url(value)
        return value


class ResearchClaim(ResearchModel):
    kind: Literal["fact", "opinion", "inference"]
    text: str
    ticker: str
    direction: Literal["positive", "negative", "neutral"]
    relationship: str
    verification: Literal["source_attributed"] = "source_attributed"
    citation: Citation


class ResearchReport(ResearchModel):
    id: str
    evidence_id: str
    evidence_hash: str
    instrument: ResearchInstrument
    horizon: Horizon
    action: ResearchAction
    status: ResearchStatus
    extraction_status: str
    summary: str
    thesis: str = ""
    claims: list[ResearchClaim] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    invalidation: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    quality_issues: list[str] = Field(default_factory=list)
    created_at: datetime
    valid_until: datetime
    reference_price: float | None = Field(default=None, gt=0)
    market_context: dict[str, Any] = Field(default_factory=dict)
    analyzer_version: str
    independent_source_count: int = Field(default=1, ge=1)

    @field_validator("created_at", "valid_until")
    @classmethod
    def check_time(cls, value: datetime) -> datetime:
        return aware(value)

    @model_validator(mode="after")
    def check_validity(self) -> Self:
        if self.valid_until <= self.created_at:
            raise ValueError("report validity must follow its creation")
        if any(claim.citation.evidence_id != self.evidence_id for claim in self.claims):
            raise ValueError("report citation must reference the report evidence version")
        if self.action in {"buy_candidate", "reduce_review"}:
            if (not self.claims or self.reference_price is None or not self.conditions
                    or not self.invalidation or self.status != "ready"):
                raise ValueError("directional candidates require evidence and price")
        return self

    def at_time(self, now: datetime) -> ResearchReport:
        if aware(now) >= self.valid_until:
            return self.model_copy(update={
                "status": "expired", "action": "watch",
                "summary": "研究建议已过期；需要新的证据与行情验证后再考虑行动。",
                "quality_issues": list(dict.fromkeys([*self.quality_issues, "recommendation_expired"])),
            })
        return self


def evidence_from_item(item: Mapping[str, Any], *, now: datetime | None = None) -> EvidenceRecord:
    timestamp = aware(now or datetime.now(UTC))
    url = canonical_url(str(item.get("source_url") or item.get("url") or ""))
    title = str(item.get("title") or "未命名材料")[:1000]
    summary = str(item.get("summary") or "")[:20_000]
    content = str(item.get("content_text") or "")[:500_000]
    segments = [s if isinstance(s, TranscriptSegment) else TranscriptSegment.model_validate(s)
                for s in list(item.get("segments") or [])[:10_000]]
    if segments and not content:
        content = "\n".join(s.text for s in segments)[:500_000]
    kind = item.get("content_kind") or ("summary" if summary else "metadata")
    completeness = item.get("completeness") or "metadata"
    issues = [str(x)[:240] for x in list(item.get("quality_issues") or [])[:95]]
    published = parse_time(item.get("published_at") or item.get("datetime"))
    if published is None:
        issues.append("publication_time_unknown")
    elif published > timestamp:
        issues.append("publication_time_in_future")
    if len(str(item.get("content_text") or "")) > 500_000 or len(item.get("segments") or []) > 10_000:
        completeness = "partial"
        issues.append("content_truncated")
    external_id = str(item.get("external_id") or "")[:256]
    platform = str(item.get("platform") or item.get("source") or "news")[:80]
    identity = digest([platform, external_id, url]) if external_id else digest(url or [platform, title])
    body_hash = digest([content or summary or title,
                        [s.model_dump() for s in segments], kind, completeness])
    author = str(item.get("author") or "")[:300]
    # A source's semantic header can change without its body changing. Keep
    # body hashes/clusters reusable while preserving immutable header versions.
    header = [title.strip(), author.strip(), published.isoformat() if published else None]
    return EvidenceRecord(id=digest([identity, body_hash, header]), source_identity=identity,
                          external_id=external_id, source_url=url, title=title,
                          author=author, platform=platform,
                          published_at=published, discovered_at=timestamp, retrieved_at=timestamp,
                          summary=summary, content_text=content, content_kind=cast(ContentKind, kind),
                          completeness=cast(Completeness, completeness), segments=segments,
                          quality_issues=list(dict.fromkeys(issues)), content_hash=body_hash,
                          source_cluster=digest(re.sub(r"\s+", " ", content or summary or title).strip()))
