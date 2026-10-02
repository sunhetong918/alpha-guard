"""User-started research orchestration; construction never starts network work."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .locking import ResearchLease
from .models import ResearchInstrument
from .store import ResearchStore


class ResearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    markets: list[Literal["US", "HK", "CN"]] = Field(default=["US", "HK"])
    horizons: list[Literal["short", "swing", "long"]] = Field(
        default=["swing", "long"]
    )
    instruments: list[ResearchInstrument] = Field(default_factory=list, max_length=50)
    interval_minutes: int = Field(default=15, ge=1, le=1440)
    ai_enabled: bool = False
    news_enabled: bool = True
    news_sources: list[Literal["finnhub", "newsapi", "akshare"]] = Field(default_factory=list)
    max_items: int = Field(default=20, ge=1, le=100)
    max_ai_calls: int = Field(default=10, ge=0, le=50)
    media_enabled: bool = False
    max_media_items: int = Field(default=2, ge=1, le=5)
    max_media_seconds: int = Field(default=600, ge=60, le=600)

    @model_validator(mode="after")
    def validate_selections(self) -> ResearchOptions:
        if not self.markets or not self.horizons:
            raise ValueError("select at least one market and horizon")
        for values in (self.markets, self.horizons, self.news_sources):
            if len(values) != len(set(values)):
                raise ValueError("duplicate selection")
        identities = [(item.market, item.ticker) for item in self.instruments]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate instrument")
        return self


def _public(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _run_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value[key] for key in ("model_calls", "fetched", "reports", "restored_from_history", "media_attempts", "media_cache_hits", "transcription_calls")
              if type(value.get(key)) in {int, bool}}
    for key in ("status", "detail", "finished_at"):
        if isinstance(value.get(key), str):
            result[key] = value[key][:240]
    statuses = value.get("analysis_statuses")
    if isinstance(statuses, dict):
        result["analysis_statuses"] = {str(key)[:40]: count for key, count in list(statuses.items())[:20]
                                       if type(count) is int and 0 <= count <= 100_000}
    return result


def _source_summary(value: Any, *, nested: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("subscription_id", "label", "provider", "operation", "status", "detail", "error_code"):
        if isinstance(value.get(key), str):
            result[key] = value[key][:200 if key == "label" else 80]
    for key in ("fetched", "attempted", "succeeded", "failed", "cancelled", "article_count"):
        if type(value.get(key)) in {int, bool}:
            result[key] = value[key]
    if not nested and isinstance(value.get("sources"), list):
        result["sources"] = [_source_summary(item, nested=True) for item in value["sources"][:100]]
    return result


class ResearchService:
    """One cancellable worker per Guardian, with explicit ephemeral activation.

    Each DB operation uses its own connection on its owning thread. The UI only
    observes local snapshots; every external call is inside an explicit run.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        collect_news: Callable[..., Any] | None = None,
        discoverer: Callable[..., dict[str, Any]] | None = None,
        market_loader: Callable[..., dict[str, Any]] | None = None,
        analyzer: Callable[..., list[Any]] | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self._collect_news = collect_news
        self._discoverer = discoverer
        self._market_loader = market_loader
        self._analyzer = analyzer
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lease: ResearchLease | None = None
        self._state = "STOPPED"
        self._mode: str | None = None
        self._stage = "idle"
        self._next_run_at: str | None = None
        self._last_run: dict[str, Any] | None = None

    def _options(self) -> ResearchOptions:
        with ResearchStore(self.path) as store:
            return ResearchOptions.model_validate(store.load_settings() or {})

    def open_platform_login(self, platform: str) -> dict[str, Any]:
        """Open a user-controlled login page without changing research state."""
        from config import get_settings
        from .browser_source import open_platform_login

        environment = get_settings()
        return open_platform_login(platform, enabled=environment.research_browser_enabled,
                                   mode=environment.research_browser_mode)

    def snapshot(self, *, page: int = 0) -> dict[str, Any]:
        if type(page) is not int or page < 0:
            raise ValueError("page must be a nonnegative integer")
        page_size, window_limit = 20, 100
        offset = page * page_size
        from config import get_settings
        from .model_provider import model_capabilities

        environment = get_settings()
        capabilities = {
            **model_capabilities(environment),
            "transcription_configured": bool(environment.transcription_command),
            "browser_enabled": environment.research_browser_enabled,
            "browser_mode": environment.research_browser_mode,
            "cn_data_installed": importlib.util.find_spec("akshare") is not None,
            "news_sources": {"finnhub": bool(environment.finnhub_api_key),
                             "newsapi": bool(environment.newsapi_api_key),
                             "akshare": True},
        }
        with self._lock:
            runtime = {
                "state": self._state,
                "mode": self._mode,
                "stage": self._stage,
                "next_run_at": self._next_run_at,
                "last_run": self._last_run,
                "restart_policy": "stopped",
            }
        with ResearchStore(self.path) as store:
            options = ResearchOptions.model_validate(store.load_settings() or {})
            # Fetch one bounded SQL page plus a sentinel; never load the full
            # article archive to construct a compact desktop list.
            limit = min(page_size + 1, max(0, window_limit - offset))
            evidence = [_public(item) for item in store.list_evidence(limit=limit, offset=offset)] if limit else []
            conflicts = store.conflicting_profiles() if limit else set()
            reports = [self._current_report(_public(item), conflicts)
                       for item in store.list_reports(limit=limit, offset=offset)] if limit else []
            subscriptions = [_public(item) for item in store.list_subscriptions(limit=limit, offset=offset)] if limit else []
            runs = store.list_runs(limit=3)
        if runtime["last_run"] is None and runs:
            latest = runs[0]
            runtime["last_run"] = {**latest["stats"], "status": latest["status"],
                                   "detail": latest["detail"], "finished_at": latest["finished_at"],
                                   "restored_from_history": True}
        last_run = runtime["last_run"]
        source_values = last_run.get("sources", [])[:window_limit] if isinstance(last_run, dict) else []
        more = offset + page_size < window_limit and (
            any(len(values) > page_size for values in (evidence, reports, subscriptions))
            or len(source_values) > offset + page_size
        )
        if isinstance(last_run, dict):
            last_summary = _run_summary(last_run)
            last_summary["sources"] = [
                _source_summary(value) for value in source_values[offset:offset + page_size]
            ]
            runtime["last_run"] = last_summary
        # Full material is fetched separately. Keep the combined worst-case
        # Unicode page below the 1 MiB IPC frame limit while retaining URLs.
        evidence = evidence[:page_size]
        reports = reports[:page_size]
        for item in evidence:
            for key, cap in (("content_text", 240), ("summary", 300), ("title", 300), ("author", 160)):
                item[key] = str(item.get(key, ""))[:cap]
            item["segments"] = []
            item["quality_issues"] = [str(value)[:80] for value in item.get("quality_issues", [])[:4]]
        for report in reports:
            for key in ("claims", "market_context", "catalysts", "counter_evidence", "conditions", "invalidation", "verification_steps"):
                report.pop(key, None)
            for key, value in report.items():
                if isinstance(value, str):
                    report[key] = value[:300 if key in {"summary", "thesis"} else 256]
            report["quality_issues"] = [str(value)[:80] for value in report.get("quality_issues", [])[:4]]
        return {
            "page": page, "next_page": page + 1 if more else None, "window_limit": window_limit,
            "runtime": runtime,
            "capabilities": capabilities,
            "settings": options.model_dump(mode="json"),
            "subscriptions": subscriptions[:page_size],
            "evidence": evidence,
            "reports": reports,
            "runs": [{**{key: value for key, value in run.items() if key != "stats"},
                      "stats": _run_summary(run["stats"])} for run in runs] if page == 0 else [],
        }

    def detail(self, kind: str, identity: str, *, offset: int = 0, segment_offset: int = 0, full: bool = False) -> dict[str, Any]:
        if kind not in {"evidence", "report"} or not identity or len(identity) > 128:
            raise ValueError("invalid research detail")
        if type(offset) is not int or type(segment_offset) is not int or min(offset, segment_offset) < 0:
            raise ValueError("invalid detail offset")
        with ResearchStore(self.path) as store:
            item = store.get_evidence(identity) if kind == "evidence" else store.get_report(identity)
            conflicts = store.conflicting_profiles() if kind == "report" else set()
        if item is None:
            raise ValueError("research item not found")
        result = _public(item)
        if kind == "report":
            result = self._current_report(result, conflicts)
        if full:
            return result
        if kind == "evidence":
            content = result["content_text"]
            segments = result["segments"]
            result["content_text"] = content[offset:offset + 40_000]
            page: list[dict[str, Any]] = []
            size = 0
            for segment in segments[segment_offset:segment_offset + 100]:
                length = len(segment.get("text", ""))
                if page and size + length > 40_000:
                    break
                page.append(segment)
                size += length
            result.update(content_offset=offset,
                          content_next_offset=offset + 40_000 if offset + 40_000 < len(content) else None,
                          segment_offset=segment_offset,
                          segment_next_offset=segment_offset + len(page) if segment_offset + len(page) < len(segments) else None,
                          segments=page)
        else:
            # A merged long interview can have many counterarguments and
            # verification steps even when it has very few claims. Page all
            # explanation lists together without dropping their full text.
            lists = ("claims", "catalysts", "counter_evidence", "conditions", "invalidation", "verification_steps")
            size = max(len(result[key]) for key in lists)
            for key in lists:
                result[key] = result[key][offset:offset + 10]
            result["claim_offset"] = offset
            result["claim_next_offset"] = offset + 10 if offset + 10 < size else None
        return result

    @staticmethod
    def _current_report(report: dict[str, Any], conflicts: set[tuple[str, str, str]]) -> dict[str, Any]:
        instrument = report["instrument"]
        if (instrument["market"], instrument["ticker"], report["horizon"]) in conflicts and report["status"] != "expired":
            report["original_action"] = report["action"]
            report["action"] = "watch"
            report["status"] = "insufficient_evidence"
            report["quality_issues"] = list(dict.fromkeys([*report["quality_issues"], "conflicting_sources_requires_review"]))
            report["summary"] = "当前有效材料给出了相反方向；先比较原文、时间和反证，暂不形成方向性建议。"
        return report

    def configure(self, values: Mapping[str, Any]) -> dict[str, Any]:
        options = ResearchOptions.model_validate(dict(values))
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("stop monitoring before changing research settings")
            with ResearchLease(self.path), ResearchStore(self.path) as store:
                store.save_settings(options.model_dump(mode="json"))
        return options.model_dump(mode="json")

    def add_subscription(self, values: Mapping[str, Any]) -> dict[str, Any]:
        from .connectors import normalize_subscription

        allowed = {"platform", "url", "label", "feed_url", "enabled"}
        if set(values) - allowed:
            raise ValueError("unknown subscription fields")
        # URL normalization is local. Channel resolution is deferred to an explicit scan.
        item = normalize_subscription(dict(values))
        item = dict(_public(item))
        item["id"] = item.get("id") or uuid.uuid4().hex
        with ResearchStore(self.path) as store:
            existing = store.list_subscriptions()
            for subscription in existing:
                if subscription.platform == item["platform"] and subscription.url == item["url"]:
                    item["id"] = subscription.id
                    break
            if len(existing) >= 100 and item["id"] not in {sub.id for sub in existing}:
                raise ValueError("at most 100 subscriptions are supported")
            saved = store.upsert_subscription(item)
        return _public(saved)

    def edit_subscription(self, identity: str, *, enabled: bool | None = None, delete: bool = False) -> None:
        with ResearchStore(self.path) as store:
            if delete:
                store.delete_subscription(identity)
                return
            if type(enabled) is not bool:
                raise ValueError("enabled must be a boolean")
            for subscription in store.list_subscriptions():
                if subscription.id == identity:
                    item = _public(subscription)
                    item["enabled"] = enabled
                    store.upsert_subscription(item)
                    return
        raise ValueError("subscription not found")

    def import_text(self, values: Mapping[str, Any]) -> dict[str, Any]:
        from .ingest import ingest_text

        allowed = {"text", "kind", "source_url", "title", "author", "platform", "published_at"}
        if set(values) - allowed or not isinstance(values.get("text"), str):
            raise ValueError("invalid material import")
        if len(values["text"]) > 250_000:
            raise ValueError("material is too large")
        item = ingest_text(**dict(values))
        with ResearchStore(self.path) as store:
            return _public(store.upsert_evidence(item))

    def launch(self, *, continuous: bool = False, media: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("research is already running")
            self._options()  # validate before changing runtime state
            self._lease = ResearchLease(self.path)
            self._stop.clear()
            self._mode = "monitor" if continuous else ("transcribe" if media else "once")
            self._state = "RUNNING"
            self._stage = "queued"
            self._next_run_at = None
            self._thread = threading.Thread(
                target=self._worker, args=(continuous, media),
                name="alpha-guard-research", daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                self._lease.close()
                self._lease = None
                self._state = "STOPPED"
                raise
        return {"accepted": True, "state": "RUNNING", "mode": self._mode}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            running = self._thread is not None and self._thread.is_alive()
            self._state = "STOPPING" if running else "STOPPED"
            self._next_run_at = None
            return {"accepted": True, "state": self._state}

    def close(self, timeout: float = 2.0) -> None:
        self.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def _worker(self, continuous: bool, media: Mapping[str, Any] | None) -> None:
        try:
            if media is not None:
                self._transcribe(media)
                return
            while not self._stop.is_set():
                self._scan()
                if not continuous or self._stop.is_set():
                    break
                interval = self._options().interval_minutes * 60
                with self._lock:
                    self._stage = "waiting"
                    self._next_run_at = (datetime.now(UTC) + timedelta(seconds=interval)).isoformat()
                if self._stop.wait(interval):
                    break
        except Exception:
            # Do not serialize provider exceptions, URLs or credentials to IPC.
            with self._lock:
                self._last_run = {"status": "failed", "detail": "research_failed"}
            try:
                with ResearchStore(self.path) as store:
                    store.record_run("failed", "research_failed")
            except Exception:
                pass  # A broken store must not retain the running state or lease.
        finally:
            with self._lock:
                if self._lease is not None:
                    self._lease.close()
                    self._lease = None
                self._state = "STOPPED"
                self._stage = "idle"
                self._next_run_at = None

    def _transcribe(self, media: Mapping[str, Any]) -> None:
        from .ingest import transcribe_media
        from config import get_settings

        self._set_stage("transcribing")
        command_raw = get_settings().transcription_command
        command = json.loads(command_raw) if command_raw else None
        if command is not None and (
            not isinstance(command, list) or not all(isinstance(item, str) for item in command)
        ):
            raise ValueError("invalid transcription command")
        result = transcribe_media(
            command=command, cancelled=self._stop.is_set, **dict(media)
        )
        if self._stop.is_set():
            result = {"status": "cancelled", "detail": "cancelled"}
        with ResearchStore(self.path) as store:
            if result.get("item"):
                store.upsert_evidence(result["item"])
            store.record_run(result["status"], result.get("detail", ""))
        with self._lock:
            self._last_run = {key: result.get(key) for key in ("status", "detail")}

    def _scan(self) -> None:
        from config import get_settings
        from .analysis import AnalysisBudget, analyze_evidence
        from .connectors import discover, fetch_article
        from .market import fetch_market_context

        started = datetime.now(UTC)
        options = self._options()
        environment = get_settings()
        browser_enabled = environment.research_browser_enabled
        media_handler = None
        if options.media_enabled and browser_enabled:
            from .video_research import VideoResearch

            try:
                command = json.loads(environment.transcription_command or "null")
            except (TypeError, ValueError):
                command = None
            media_handler = VideoResearch(
                self.path, command=command, max_items=options.max_media_items,
                max_seconds=options.max_media_seconds, cancelled=self._stop.is_set,
                stage=self._set_stage,
            )
        instruments = [item for item in options.instruments if item.market in options.markets]
        source_results: list[dict[str, Any]] = []
        buckets: list[list[dict[str, Any]]] = []
        items: list[dict[str, Any]] = []
        self._set_stage("collecting")
        if options.news_enabled and not self._stop.is_set():
            report = self._news(instruments, options)
            source_results.append(report.to_summary())
            for raw in report.articles[:options.max_items]:
                if self._stop.is_set():
                    break
                published = raw.get("datetime")
                items.append({
                    "external_id": hashlib.sha256(str(raw.get("url") or raw.get("title", "")).encode()).hexdigest(),
                    "source_url": raw.get("url", ""),
                    "title": raw.get("title", ""), "author": raw.get("source", ""),
                    "published_at": published.isoformat() if isinstance(published, datetime) else published,
                    "summary": raw.get("summary", ""), "content_text": "",
                    "content_kind": "summary", "completeness": "partial",
                    "segments": [], "platform": "news", "quality_issues": [],
                })
        if items:
            buckets.append(items)
        with ResearchStore(self.path) as store:
            subscriptions = store.list_subscriptions(enabled_only=True)
        for subscription in subscriptions:
            if self._stop.is_set():
                break
            try:
                if self._discoverer is not None:
                    result = self._discoverer(_public(subscription), cancelled=self._stop.is_set)
                else:
                    result = discover(_public(subscription), cancelled=self._stop.is_set,
                                      browser_enabled=browser_enabled, media_handler=media_handler)
                buckets.append(result.get("items", [])[:options.max_items])
                source_results.append({
                    "subscription_id": subscription.id,
                    "label": subscription.label,
                    "status": result.get("status", "failed"),
                    "detail": result.get("detail", ""),
                    "fetched": len(result.get("items", [])),
                })
            except Exception:
                source_results.append({"subscription_id": subscription.id, "status": "failed", "detail": "source_failed"})
        items = [bucket[index] for index in range(options.max_items)
                 for bucket in buckets if index < len(bucket)][:options.max_items]
        # A small batch first establishes durable evidence. Failed enrichment
        # leaves the original summary intact and explicitly incomplete.
        self._set_stage("reading")
        with ResearchStore(self.path) as store:
            for index, item in enumerate(items[:options.max_items]):
                if self._stop.is_set():
                    break
                if item.get("platform") == "news" and item.get("source_url") and index < 10:
                    try:
                        result = fetch_article(
                            item["source_url"], title=item.get("title", ""),
                            author=item.get("author", ""), platform="news",
                            published_at=item.get("published_at"), cancelled=self._stop.is_set,
                        )
                        enriched = result.get("item") or {}
                        if enriched.get("content_text"):
                            item = {**item, **enriched, "external_id": item["external_id"],
                                    "summary": item["summary"] or enriched.get("summary", "")}
                        else:
                            item["quality_issues"] = list(dict.fromkeys([
                                *item.get("quality_issues", []),
                                *enriched.get("quality_issues", []), "full_content_unavailable",
                            ]))
                    except Exception:
                        item["quality_issues"] = ["full_content_unavailable"]
                if not self._stop.is_set():
                    try:
                        store.upsert_evidence(item)
                    except (ValueError, TypeError):
                        source_results.append({"status": "partial", "detail": "invalid_material"})
            evidence = store.list_current_evidence(limit=options.max_items)
        contexts: dict[str, dict[str, Any]] = {}
        if options.ai_enabled and evidence and instruments:
            self._set_stage("market_data")
            for instrument in instruments:
                if self._stop.is_set():
                    break
                try:
                    contexts[f"{instrument.market}:{instrument.ticker}"] = (
                        self._market_loader or fetch_market_context
                    )(_public(instrument), cancelled=self._stop.is_set)
                except Exception:
                    contexts[f"{instrument.market}:{instrument.ticker}"] = {"quality_issues": ["market_unavailable"]}
        count = 0
        budget = AnalysisBudget(options.max_ai_calls)
        analysis_statuses: dict[str, int] = {}
        self._set_stage("analyzing")
        with ResearchStore(self.path) as store:
            for material in evidence:
                if self._stop.is_set():
                    break
                reports = (self._analyzer or analyze_evidence)(
                    material, instruments, horizons=options.horizons,
                    market_contexts=contexts, store=store,
                    ai_enabled=options.ai_enabled, budget=budget,
                    cancelled=self._stop.is_set,
                )
                if self._stop.is_set():
                    break
                for report in reports:
                    saved = store.save_report(report)
                    analysis_statuses[saved.status] = analysis_statuses.get(saved.status, 0) + 1
                    count += 1
            failures = [item for item in source_results if item.get("status") in {"failed", "permission_required", "unsupported", "needs_configuration", "partial"}]
            successes = [item for item in source_results if item.get("status") in {"success", "partial"}]
            status = "cancelled" if self._stop.is_set() else (
                "partial" if failures and successes else "failed" if failures else
                "success" if successes or evidence else "disabled"
            )
            if options.ai_enabled and any(analysis_statuses.get(key, 0) for key in ("failed", "invalid_output", "needs_configuration")):
                status = "partial" if status == "success" else status
            result = {"model_calls": budget.used_calls, "analysis_statuses": analysis_statuses, "status": status, "fetched": len(items), "reports": count, "sources": source_results, "finished_at": datetime.now(UTC).isoformat()}
            if media_handler is not None:
                result.update(media_attempts=media_handler.attempts,
                              media_cache_hits=media_handler.cache_hits,
                              transcription_calls=media_handler.budget.used_calls)
            store.record_run(status, started_at=started, stats=result)
        with self._lock:
            self._last_run = result

    def _news(self, instruments: list[ResearchInstrument], options: ResearchOptions) -> Any:
        from config import load_news_config
        from news.sources import fetch_all_news_report

        config = load_news_config()
        sources = config.sources
        if options.news_sources:
            sources = sources.model_copy(update={
                name: getattr(sources, name).model_copy(update={"enabled": name in options.news_sources})
                for name in ("finnhub", "newsapi", "akshare")
            })
        queries = [keyword for topic in config.macro_topics for keyword in topic.keywords[:2]]
        return (self._collect_news or fetch_all_news_report)(
            [item.ticker for item in instruments if item.market != "CN"],
            queries, sources, should_continue=lambda: not self._stop.is_set(),
        )
