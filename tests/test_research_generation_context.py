"""Generation provenance is owned by code and survives cache/reopen unchanged."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from config import Settings
from research.analysis import (
    ANALYZER_VERSION, _ExtractionChunk, analyze_evidence, build_generation_context,
    extraction_cache_key, generation_config_signature,
)
from research.model_provider import generation_parameters
from research.models import ExtractionResult, ModelProfile, ResearchReport, evidence_from_item
from research.store import ResearchStore
from research.service import ResearchService
from research.reviews import execute_review
from test_research_core import BODY, NOW, Client, context, instrument, item, output


def runtime(**changes):
    return Settings(_env_file=None, anthropic_api_key=None, **changes)


def test_summary_omits_config_but_report_and_review_details_keep_original(tmp_path):
    database = tmp_path / "state.db"
    with ResearchStore(database) as store:
        original = analyze(store=store)[0]
    service = ResearchService(database)
    summary = next(value for value in service.snapshot()["reports"] if value["id"] == original.id)
    assert "generation_context" not in summary  # No repeated full configuration in compact IPC lists.
    expected = original.generation_context.model_dump(mode="json")
    assert service.detail("report", original.id)["generation_context"] == expected
    review = execute_review(database, "review_detail", {"report_id": original.id})
    assert review["generation_context"] == expected
    assert json.loads(review["content"])["generation_context"] == expected


def profile(**changes):
    return ModelProfile.model_validate({"id": "a" * 32, "name": "Fixture relay",
        "protocol": "openai_compatible", "base_url": "https://relay.example.test/private-path",
        "model": "fixture-model", "timeout_seconds": 30.0, **changes})


def analyze(store=None, *, settings=None, client=None, evidence=None, **changes):
    return analyze_evidence(evidence or evidence_from_item(item(), now=NOW), [instrument()],
        horizons=("swing",), market_contexts={"US:AAPL": context()}, settings=settings or runtime(),
        client=client or Client(output()), store=store, now=NOW, **changes)


def test_context_is_bounded_and_model_output_schema_cannot_author_it():
    frozen = build_generation_context(runtime(), [instrument()], ("swing",), client_supplied=True)
    assert frozen.provider == "injected_client"
    assert frozen.parameters.max_output_tokens == 6000
    assert frozen.parameters.timeout_seconds is None
    assert "generation_context" not in _ExtractionChunk.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        frozen.parameters.max_output_tokens = 1
    with pytest.raises(ValidationError):
        _ExtractionChunk.model_validate({**output(), "generation_context": frozen.model_dump()})
    report = analyze(client=Client({**output(), "generation_context": frozen.model_dump(mode="json")}))[0]
    assert report.status == "invalid_output" and report.claims == []
    # Failed attempts record only the configuration frozen by application code.
    assert report.generation_context == frozen


def test_signature_ignores_profile_name_and_credential_rotation_but_tracks_behavior():
    settings = runtime()
    first = profile()
    def signature(value):
        return generation_config_signature(settings, [instrument()], ("long", "swing"), profile=value)
    original = signature(first)
    renamed = first.model_copy(update={"name": "Renamed", "key_configured": True,
        "key_updated_at": "later", "updated_at": "later"})
    assert signature(renamed) == original
    for changes in ({"model": "different"}, {"protocol": "anthropic_compatible"},
                    {"base_url": "https://other.example.test"}, {"timeout_seconds": 45.0},
                    {"allow_private_network": True}):
        assert signature(first.model_copy(update=changes)) != original
    assert generation_config_signature(settings, [instrument()], ("swing",), profile=first) != original
    assert generation_config_signature(settings, [instrument(ticker="MSFT")], ("long", "swing"),
                                       profile=first) != original
    assert signature(first) == generation_config_signature(settings, [instrument()],
                                                           ("swing", "long"), profile=first)
    # The read-only helper also accepts legacy lightweight test settings.
    assert len(generation_config_signature(SimpleNamespace(), [instrument()], ("swing",))) == 64


@pytest.mark.parametrize("provider, expected", [
    ("codex_cli", {"max_output_tokens": None, "timeout_seconds": 37.0,
                   "reasoning_effort": "low", "max_turns": 1, "tools_enabled": False}),
    ("claude_cli", {"max_output_tokens": None, "timeout_seconds": 37.0,
                    "reasoning_effort": None, "max_turns": 1, "tools_enabled": False}),
    ("anthropic", {"max_output_tokens": 6000, "timeout_seconds": 60.0,
                   "reasoning_effort": None, "max_turns": None, "tools_enabled": False}),
])
def test_snapshot_contains_only_actual_provider_parameters(provider, expected):
    settings = runtime(research_model_provider=provider, research_model_timeout_seconds=37,
                       http_timeout_seconds=120)
    snapshot = build_generation_context(settings, [instrument()], ("swing",))
    assert snapshot.provider == provider
    assert snapshot.parameters.model_dump() == expected
    assert snapshot.parameters == generation_parameters(settings)
    assert snapshot.endpoint_fingerprint is None  # Managed client routing is not guessed.


def test_analysis_freezes_settings_and_market_before_first_model_call():
    settings = runtime(anthropic_model="before-model")
    market = context()
    class MutatingClient(Client):
        def create(self, **kwargs):
            settings.anthropic_model = "after-model"
            market["price"] = 999.0
            return super().create(**kwargs)
    client = MutatingClient(output())
    evidence = evidence_from_item(item(content_text=BODY * 500), now=NOW)
    reports = analyze_evidence(evidence, [instrument()], horizons=("swing",),
        settings=settings, client=client, now=NOW, market_contexts={"US:AAPL": market})
    assert len(client.calls) > 1
    assert {call["model"] for call in client.calls} == {"before-model"}
    report = reports[0]
    assert report.generation_context.model == "before-model"
    assert report.generation_context.parameters.max_output_tokens == client.calls[0]["max_tokens"]
    assert report.reference_price == 120.0
    assert report.generation_context.analyzer_version == ANALYZER_VERSION


def test_cache_and_reopened_report_keep_original_snapshot(tmp_path):
    database = tmp_path / "state.db"
    client = Client(output())
    with ResearchStore(database) as store:
        first = analyze(store, client=client)[0]
        second = analyze(store, client=client, max_calls=0)[0]
        assert len(client.calls) == 1
        assert second.id == first.id
        assert second.generation_context == first.generation_context
    with ResearchStore(database) as reopened:
        historical = reopened.get_report(first.id, now=NOW)
        assert historical.generation_context == first.generation_context
        other = analyze(reopened, settings=runtime(anthropic_model="another-model"), client=client)[0]
        assert len(client.calls) == 2 and other.id != first.id
        assert other.generation_context.model == "another-model"
        assert reopened.get_report(first.id, now=NOW).generation_context == first.generation_context


def test_profile_requests_and_history_use_frozen_safe_snapshot(tmp_path, monkeypatch):
    from research import model_http
    calls = []
    selected = profile()
    class FakeKeyStore:
        def load(self, _identifier):
            return "fixture-secret-key"
    monkeypatch.setattr("research.model_key_store.ModelKeyStore", FakeKeyStore)
    with ResearchStore(tmp_path / "state.db") as store:
        store.save_model_profile(selected)
        store.save_settings({**store.load_settings(), "model_profile_id": selected.id})
        def post(**kwargs):
            calls.append(kwargs)
            store.save_model_profile(selected.model_copy(update={"model": "replacement", "timeout_seconds": 44.0}))
            return {"choices": [{"message": {"content": json.dumps(output())}}]}
        monkeypatch.setattr(model_http, "model_post_json", post)
        report = analyze(store, evidence=evidence_from_item(item(content_text=BODY * 500), now=NOW))[0]
        assert len(calls) > 1
        assert {call["payload"]["model"] for call in calls} == {selected.model}
        assert {call["timeout_seconds"] for call in calls} == {selected.timeout_seconds}
        snapshot = report.generation_context
        assert snapshot.profile_id == selected.id and snapshot.provider == selected.protocol
        assert snapshot.model == selected.model and snapshot.parameters.timeout_seconds == 30.0
        assert snapshot.endpoint_fingerprint and snapshot.profile_version
        serialized = report.model_dump_json()
        for forbidden in (selected.base_url, "private-path", "fixture-secret-key", "Authorization", selected.name):
            assert forbidden not in serialized
        store.delete_model_profile(selected.id)
        assert store.get_report(report.id, now=NOW).generation_context == snapshot


def test_profile_rename_and_key_rotation_reuse_cache_without_restamping(tmp_path, monkeypatch):
    selected = profile()
    monkeypatch.setattr("research.model_key_store.ModelKeyStore", lambda: SimpleNamespace(load=lambda _: None))
    calls = []
    def post(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": json.dumps(output())}}]}
    monkeypatch.setattr("research.model_http.model_post_json", post)
    with ResearchStore(tmp_path / "state.db") as store:
        store.save_model_profile(selected)
        store.save_settings({**store.load_settings(), "model_profile_id": selected.id})
        first = analyze(store)[0]
        store.save_model_profile(selected.model_copy(update={"name": "new name", "key_updated_at": "later"}))
        cached = analyze(store, max_calls=0)[0]
        assert len(calls) == 1
        assert cached.generation_context == first.generation_context
        assert cached.id == first.id


def test_partial_chunks_resume_with_original_configuration(tmp_path):
    client = Client(output())
    evidence = evidence_from_item(item(content_text=BODY * 500), now=NOW)
    with ResearchStore(tmp_path / "state.db") as store:
        interrupted = analyze(store, evidence=evidence, client=client, max_calls=1)[0]
        assert interrupted.extraction_status == "budget_exhausted"
        assert len(client.calls) == 1
        complete = analyze(store, evidence=evidence, client=client)[0]
        assert complete.status == "ready" and len(client.calls) == 3
        assert complete.generation_context == interrupted.generation_context
        assert analyze(store, evidence=evidence, client=client, max_calls=0)[0].id == complete.id
        assert len(client.calls) == 3


def test_legacy_report_and_extraction_remain_unknown_and_are_not_restamped(tmp_path):
    fresh = analyze()[0]
    legacy_payload = fresh.model_dump(mode="json")
    legacy_payload.pop("generation_context")
    old_report = ResearchReport.model_validate_json(json.dumps(legacy_payload))
    assert old_report.generation_context is None
    assert ExtractionResult.model_validate_json(json.dumps(output())).generation_context is None
    with ResearchStore(tmp_path / "state.db") as store:
        store.upsert_evidence(evidence_from_item(item(), now=NOW))
        store.save_report(old_report)
        assert store.get_report(old_report.id, now=NOW).generation_context is None
        # Even a contextless extraction found under a current key is never attributed
        # to today's configuration. A real fresh extraction is required.
        evidence = evidence_from_item(item(), now=NOW)
        snapshot = build_generation_context(runtime(), [instrument()], ("swing",), client_supplied=True)
        key = extraction_cache_key(evidence, [instrument()], ("swing",),
            "injected_client:" + snapshot.model, config_version=snapshot.config_version)
        store.save_extraction(key, ExtractionResult.model_validate(output()))
        client = Client(output())
        current = analyze(store, client=client)[0]
        assert len(client.calls) == 1
        # Existing immutable historical identity wins; it is never silently filled.
        assert current.generation_context is None


def test_deferred_report_persistence_keeps_caches_but_writes_no_report(tmp_path):
    client = Client(output())
    with ResearchStore(tmp_path / "state.db") as store:
        report = analyze(store, client=client, persist_reports=False)[0]
        assert report.generation_context is not None
        assert store.get_report(report.id, now=NOW) is None
        cached = analyze(store, client=client, max_calls=0, persist_reports=False)[0]
        assert len(client.calls) == 1 and cached.extraction_status == "cached"
        assert cached.generation_context == report.generation_context
        assert store.get_report(report.id, now=NOW) is None


def test_no_generation_does_not_claim_current_settings_generated_the_report():
    assert analyze(ai_enabled=False)[0].generation_context is None
    assert analyze(evidence=evidence_from_item(item(content_text="", completeness="metadata"), now=NOW))[0].generation_context is None
