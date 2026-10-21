from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from research.models import Citation, ResearchClaim, ResearchInstrument, ResearchReport
from research.reviews import MAX_IMPORT_BYTES, MAX_SAMPLES, ReviewConflictError, ReviewStore, execute_review
from research.store import ResearchStore

NOW = datetime(2026, 9, 29, 14, tzinfo=UTC)


def report(database, identity="r1", ticker="AAPL", *, action="buy_candidate", age=0):
    created = NOW + timedelta(days=age)
    with ResearchStore(database) as store:
        evidence = store.upsert_evidence({"source_url": f"https://example.com/{identity}",
            "title": "Company filing", "published_at": (created - timedelta(hours=1)).isoformat(),
            "content_text": "The company reported higher revenue and stable operating cash generation.",
            "content_kind": "article", "completeness": "full"}, now=created - timedelta(minutes=2))
        original = ResearchReport(id=identity, evidence_id=evidence.id, evidence_hash=evidence.content_hash,
            instrument=ResearchInstrument(market="US", ticker=ticker), horizon="swing", action=action,
            status="ready", extraction_status="analyzed", summary="Original report summary", analyzer_version="test",
            claims=[ResearchClaim(kind="fact", text="Higher revenue", ticker=ticker, direction="positive", relationship="revenue",
                                 citation=Citation(evidence_id=evidence.id, source_url=evidence.source_url, quote="reported higher revenue"))],
            created_at=created, valid_until=created + timedelta(hours=1), reference_price=100.0,
            conditions=["Verify next filing"], invalidation=["Revenue declines"],
            market_context={"ticker": ticker, "market": "US", "currency": "USD", "price": 100.0,
                            "provider": "baseline-fixture", "source_as_of": (created - timedelta(minutes=1)).isoformat(),
                            "observed_at": (created - timedelta(seconds=20)).isoformat()})
        return store.save_report(original)


def sample(original, **changes):
    return {"instrument": original.instrument.model_dump(mode="json"), "price": 110.0, "currency": "USD",
            "source_as_of": original.valid_until.isoformat(),
            "observed_at": (original.valid_until + timedelta(minutes=1)).isoformat(),
            "provider": "user-provided-fixture", "granularity": "daily", **changes}


def save(database, identity="r1", **changes):
    return execute_review(database, "review_save", {"report_id": identity, "status": "verified", "notes": "核对原文",
                          "next_review_at": "2026-10-20T09:00:00+08:00", "revision": 0, **changes})


def test_review_persists_independently_and_never_changes_original(tmp_path):
    database = tmp_path / "state.db"
    original = report(database)
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT payload_json FROM research_reports WHERE id='r1'").fetchone()[0]
    pending = execute_review(database, "review_list", {})["items"][0]["review"]
    assert pending["status"] == "pending" and pending["revision"] == 0
    assert save(database)["revision"] == 1
    reopened = execute_review(database, "review_list", {})["items"][0]
    assert reopened["review"]["notes"] == "核对原文"
    assert reopened["review"]["next_review_at"] == "2026-10-20T01:00:00+00:00"
    assert reopened["original_action"] == "buy_candidate"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT payload_json FROM research_reports WHERE id='r1'").fetchone()[0] == before
    detail = execute_review(database, "review_detail", {"report_id": original.id})
    assert detail["original"] is True
    assert json.loads(detail["content"])["summary"] == original.summary
    assert list(tmp_path.glob("*.pre-workflow-*.bak"))


def test_stale_review_write_is_rejected_across_connections(tmp_path):
    database = tmp_path / "state.db"
    report(database)
    first = save(database)
    with pytest.raises(ReviewConflictError):
        save(database, notes="stale edit")
    second = save(database, revision=first["revision"], status="deferred", notes="需要补充材料")
    assert second["revision"] == 2
    with pytest.raises(ReviewConflictError):
        save(database, revision=1)
    assert execute_review(database, "review_list", {})["items"][0]["review"]["notes"] == "需要补充材料"


def test_concurrent_first_reviews_have_exactly_one_winner(tmp_path):
    database = tmp_path / "state.db"
    report(database)
    execute_review(database, "review_list", {})  # complete the additive migration first
    barrier = threading.Barrier(2)

    def writer(note):
        barrier.wait(timeout=3)
        try:
            return save(database, notes=note)["revision"]
        except ReviewConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(writer, ["window one", "window two"]))
    assert results.count(1) == 1 and results.count("conflict") == 1
    assert execute_review(database, "review_list", {})["items"][0]["review"]["revision"] == 1


@pytest.mark.parametrize("changes", [{"report_id": "missing"}, {"revision": True}, {"revision": -1},
                                     {"status": "approve_trade"}, {"notes": "x" * 4001},
                                     {"next_review_at": "2026-10-20T09:00:00"}])
def test_invalid_review_is_rejected_without_partial_write(tmp_path, changes):
    database = tmp_path / "state.db"
    report(database)
    with pytest.raises(ValueError):
        save(database, **changes)
    assert execute_review(database, "review_list", {})["items"][0]["review"]["revision"] == 0


def test_history_filters_and_pagination_use_original_records(tmp_path):
    database = tmp_path / "state.db"
    report(database, "old", age=-1)
    report(database, "new")
    report(database, "other", ticker="MSFT")
    save(database, "old")
    first = execute_review(database, "review_list", {"instrument": "US:AAPL", "limit": 1})
    assert first["total"] == 2 and first["has_more"]
    assert first["items"][0]["report_id"] == "new"
    second = execute_review(database, "review_list", {"instrument": "US:AAPL", "limit": 1, "offset": 1})
    assert second["items"][0]["report_id"] == "old" and not second["has_more"]
    pending = execute_review(database, "review_list", {"status": "pending", "created_from": NOW.isoformat()})
    assert {row["report_id"] for row in pending["items"]} == {"new", "other"}
    verified = execute_review(database, "review_list", {"status": "verified", "created_to": NOW.isoformat()})
    assert [row["report_id"] for row in verified["items"]] == ["old"]


@pytest.mark.parametrize("fields", [{"limit": 51}, {"offset": -1}, {"offset": False},
                                    {"created_from": "2026-01-01"}, {"instrument": "BAD:AAPL"},
                                    {"status": []}, {"created_from": NOW.isoformat(), "created_to": NOW.isoformat()}])
def test_history_validation(tmp_path, fields):
    with pytest.raises(ValueError):
        execute_review(tmp_path / "state.db", "review_list", fields)


def test_sample_import_and_followup_remain_readable_after_reopen(tmp_path):
    database = tmp_path / "state.db"
    original = report(database)
    imported = execute_review(database, "replay_import", {"source_label": "my-prices.json", "json_text": json.dumps([sample(original)])})
    assert imported["sample_count"] == 1
    assert execute_review(database, "replay_list", {})["items"][0]["id"] == imported["id"]
    result = execute_review(database, "replay_run", {"report_id": original.id, "sample_set_id": imported["id"],
                                                    "as_of": (NOW + timedelta(days=1)).isoformat()})
    assert result["result"]["status"] == "observed"
    assert result["result"]["price_change_pct"] == pytest.approx(10)
    assert result["result"]["baseline_source_as_of"] == original.market_context["source_as_of"]
    assert result["result"]["end_provider"] == "user-provided-fixture"
    assert "not_a_trading_backtest" in result["limitations"]
    last = execute_review(database, "review_detail", {"report_id": original.id})["last_followup"]
    assert last == result
    with ReviewStore(database) as store:
        assert store.original_report(original.id) == original


@pytest.mark.parametrize("change,status,reason", [
    ({"observed_at": (NOW + timedelta(days=2)).isoformat()}, "pending", "end_price_unavailable"),
    ({"corporate_action": True}, "excluded", "corporate_action_requires_adjustment"),
])
def test_followup_pending_and_exclusion_retain_reasons(tmp_path, change, status, reason):
    database = tmp_path / "state.db"
    original = report(database)
    imported = execute_review(database, "replay_import", {"source_label": "manual", "json_text": json.dumps([sample(original, **change)])})
    result = execute_review(database, "replay_run", {"report_id": original.id, "sample_set_id": imported["id"],
                                                    "as_of": (NOW + timedelta(days=1)).isoformat()})
    assert result["result"]["status"] == status
    assert result["result"]["reason"] == reason
    assert result["result"]["price_change_pct"] is None


@pytest.mark.parametrize("change", [{"provider": ""}, {"source_as_of": None}, {"observed_at": None},
                                    {"source_as_of": "2026-09-29T15:00:00"}, {"price": float("nan")},
                                    {"price": -1}, {"currency": "CNY"}, {"unexpected": "value"}])
def test_invalid_samples_have_no_partial_import(tmp_path, change):
    database = tmp_path / "state.db"
    original = report(database)
    with pytest.raises(ValueError):
        execute_review(database, "replay_import", {"source_label": "bad", "json_text": json.dumps([sample(original), sample(original, **change)])})
    assert execute_review(database, "replay_list", {})["items"] == []


@pytest.mark.parametrize("raw", ["{}", "[]", "not-json", "[null]", " " * (MAX_IMPORT_BYTES + 1),
                                 json.dumps([{}] * (MAX_SAMPLES + 1)), json.dumps([{"provider": "中" * 100_000}], ensure_ascii=False)])
def test_import_json_type_byte_and_count_limits(tmp_path, raw):
    with pytest.raises(ValueError):
        execute_review(tmp_path / "state.db", "replay_import", {"source_label": "bad", "json_text": raw})


def test_original_detail_is_bounded_and_can_be_reassembled(tmp_path):
    database = tmp_path / "state.db"
    original = report(database)
    with sqlite3.connect(database) as connection:
        body = original.model_dump(mode="json")
        body["summary"] = "原始材料" * 20_000
        connection.execute("UPDATE research_reports SET payload_json=? WHERE id=?", (json.dumps(body), original.id))
    parts, offset = [], 0
    while True:
        result = execute_review(database, "review_detail", {"report_id": original.id, "offset": offset})
        assert len(result["content"]) <= 20_000
        parts.append(result["content"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert json.loads("".join(parts))["summary"] == body["summary"]


def test_followup_uses_requested_report_even_outside_first_history_page(tmp_path):
    database = tmp_path / "state.db"
    report(database, "first", age=-1)
    target = report(database, "target")
    samples = execute_review(database, "replay_import", {"source_label": "manual", "json_text": json.dumps([sample(target)])})
    result = execute_review(database, "replay_run", {"report_id": target.id, "sample_set_id": samples["id"], "as_of": (NOW + timedelta(days=1)).isoformat()})
    assert result["result"]["report_id"] == "target"


@pytest.mark.parametrize("change", [{"as_of": "2100-01-01T00:00:00Z"}, {"as_of": "2020-01-01T00:00:00Z"},
                                    {"sample_set_id": "missing"}, {"report_id": "missing"}])
def test_invalid_followup_does_not_save_a_result(tmp_path, change):
    database = tmp_path / "state.db"
    original = report(database)
    samples = execute_review(database, "replay_import", {"source_label": "manual", "json_text": json.dumps([sample(original)])})
    with pytest.raises(ValueError):
        execute_review(database, "replay_run", {"report_id": original.id, "sample_set_id": samples["id"],
                                               "as_of": (NOW + timedelta(days=1)).isoformat(), **change})
    assert execute_review(database, "review_detail", {"report_id": original.id})["last_followup"] is None


def test_unknown_fields_do_not_create_a_database(tmp_path):
    database = tmp_path / "state.db"
    with pytest.raises(ValueError):
        execute_review(database, "review_list", {"api_key": "invalid"})
    assert not database.exists()
