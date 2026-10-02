from __future__ import annotations

import threading
from pathlib import Path

import pytest

from research.ipc import execute
from research.service import ResearchService
from research.store import ResearchStore


def configured(tmp_path: Path, **kwargs) -> ResearchService:
    service = ResearchService(tmp_path / 'state.db', **kwargs)
    service.configure({'news_enabled': False})
    return service


def subscribe(service: ResearchService, slug: str = 'creator') -> dict:
    return service.add_subscription({'platform': 'rss', 'url': f'https://example.org/{slug}.xml', 'label': slug})


def test_read_configure_subscribe_and_restart_never_start_network(tmp_path):
    calls = []
    service = configured(tmp_path, discoverer=lambda *a, **kw: calls.append('network'))
    first = subscribe(service)
    second = subscribe(service)
    assert first['id'] == second['id']
    assert service.snapshot()['runtime']['state'] == 'STOPPED'
    assert len(service.snapshot()['subscriptions']) == 1
    restarted = ResearchService(service.path)
    assert restarted.snapshot()['runtime']['state'] == 'STOPPED'
    assert restarted.snapshot()['runtime']['next_run_at'] is None
    assert calls == []


@pytest.mark.parametrize("enabled", [False, True])
def test_browser_media_requires_explicit_option_and_shares_one_round_budget(tmp_path, monkeypatch, enabled):
    from types import SimpleNamespace

    handlers = []
    monkeypatch.setattr("config.get_settings", lambda: SimpleNamespace(
        research_browser_enabled=True,
        transcription_command='["asr", "{input}", "{output_dir}"]'))
    def discover(*args, **kwargs):
        handlers.append(kwargs.get("media_handler"))
        return {"status": "success", "items": []}
    monkeypatch.setattr("research.connectors.discover", discover)
    service = configured(tmp_path)
    service.configure({"news_enabled": False, "media_enabled": enabled, "max_media_items": 1})
    subscribe(service, "one")
    subscribe(service, "two")
    assert handlers == []
    service._scan()
    assert len(handlers) == 2
    assert handlers[0] is handlers[1]
    if enabled:
        assert handlers[0].budget.max_calls == 1
    else:
        assert handlers == [None, None]


def test_once_finishes_without_timer_and_persists_source_failure(tmp_path):
    calls = []
    def discover(*args, **kwargs):
        calls.append('once')
        return {'status': 'permission_required', 'detail': 'source_authorization_required', 'items': []}
    service = configured(tmp_path, discoverer=discover)
    subscribe(service)
    execute(service, {'command': 'once'})
    service.wait(3)
    snapshot = service.snapshot()
    assert calls == ['once']
    assert snapshot['runtime']['state'] == 'STOPPED'
    assert snapshot['runtime']['next_run_at'] is None
    assert snapshot['runs'][0]['status'] == 'failed'
    assert snapshot['runtime']['last_run']['sources'][0]['status'] == 'permission_required'


def test_scan_analyzes_only_current_source_versions_and_preserves_history(tmp_path):
    analyzed = []

    def analyze(material, *args, **kwargs):
        analyzed.append(material.id)
        return []

    service = configured(tmp_path, analyzer=analyze)
    material = {'source_url': 'https://example.org/revised', 'platform': 'news',
                'title': 'Company update', 'author': 'Company',
                'content_text': 'The company announced increased revenue.',
                'content_kind': 'article', 'completeness': 'full'}
    with ResearchStore(service.path) as store:
        old = store.upsert_evidence(material)
        current = store.upsert_evidence({**material, 'title': 'Correction',
                                        'content_text': 'The earlier revenue claim is withdrawn.'})
        store.upsert_evidence(old)
    service._scan()
    assert analyzed == [current.id]
    with ResearchStore(service.path) as store:
        assert {item.id for item in store.list_evidence()} == {old.id, current.id}


def test_stop_prevents_next_source_and_analysis_duplicate_start_rejected(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def discover(*args, **kwargs):
        calls.append('source')
        entered.set()
        assert release.wait(3)
        assert kwargs['cancelled']()
        return {'status': 'cancelled', 'items': []}
    service = configured(tmp_path, discoverer=discover, analyzer=lambda *a, **kw: calls.append('analysis'))
    subscribe(service, 'one')
    subscribe(service, 'two')
    service.launch(continuous=True)
    try:
        assert entered.wait(3)
        with pytest.raises(ValueError, match='already running'):
            service.launch()
        with pytest.raises(ValueError, match='stop monitoring'):
            service.configure({})
        assert service.stop()['state'] == 'STOPPING'
        assert service.snapshot()['runtime']['next_run_at'] is None
    finally:
        release.set()
        service.wait(3)
        service.close()
    assert calls == ['source']
    assert service.snapshot()['runtime']['state'] == 'STOPPED'
    assert service.snapshot()['runs'][0]['status'] == 'cancelled'


def test_explicit_continuous_wait_can_stop_immediately(tmp_path):
    scanned = threading.Event()
    def discover(*a, **kw):
        scanned.set()
        return {'status': 'success', 'items': []}
    service = configured(tmp_path, discoverer=discover)
    subscribe(service)
    service.launch(continuous=True)
    assert scanned.wait(3)
    service.close(timeout=3)
    assert service.snapshot()['runtime']['state'] == 'STOPPED'
    restarted = ResearchService(service.path)
    assert restarted.snapshot()['runtime']['state'] == 'STOPPED'
    assert restarted.snapshot()['settings']['interval_minutes'] == 15


@pytest.mark.parametrize('params', [
    {'command': 'start', 'interval': 1},
    {'command': 'configure', 'settings': {'interval_minutes': True}},
    {'command': 'configure', 'settings': {'ai_enabled': 'yes'}},
    {'command': 'configure', 'settings': {'markets': []}},
    {'command': 'transcribe', 'path': '/tmp/x.mp4', 'shell_command': ['sh']},
    {'command': 'import', 'material': {'text': 'x', 'execute': 'shell'}},
])
def test_invalid_ipc_commands_do_not_start_work(tmp_path, params):
    service = configured(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        execute(service, params)
    assert service.snapshot()['runtime']['state'] == 'STOPPED'


def test_full_material_is_paginated_without_loss(tmp_path):
    service = configured(tmp_path)
    content = '这是有原文来源的研究材料。' * 8000
    saved = service.import_text({'text': content, 'source_url': 'https://example.org/article', 'title': '研究材料'})
    parts, offset = [], 0
    while True:
        page = service.detail('evidence', saved['id'], offset=offset)
        parts.append(page['content_text'])
        offset = page['content_next_offset']
        if offset is None:
            break
    assert ''.join(parts) == service.detail('evidence', saved['id'], full=True)['content_text']
    assert len(service.snapshot()['evidence'][0]['content_text']) <= 240
    with ResearchStore(service.path) as store:
        assert len(store.list_evidence()) == 1


def test_separate_sessions_cannot_scan_or_reconfigure_same_database(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def discover(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {'status': 'success', 'items': []}
    service = configured(tmp_path, discoverer=discover)
    subscribe(service)
    other = ResearchService(service.path, discoverer=lambda *a, **kw: {"status": "success", "items": []})
    service.launch()
    try:
        assert entered.wait(3)
        with pytest.raises(ValueError, match='another session'):
            other.launch()
        with pytest.raises(ValueError, match='another session'):
            other.configure({})
    finally:
        release.set()
        service.wait(3)
    other.launch()
    other.stop()
    other.wait(3)
    assert other.snapshot()['runtime']['state'] == 'STOPPED'


def test_source_health_survives_restart_without_resuming_monitor(tmp_path):
    service = configured(tmp_path, discoverer=lambda *a, **kw: {
        'status': 'needs_configuration', 'detail': 'authorized_feed_required', 'items': []})
    saved = subscribe(service)
    service.launch()
    service.wait(3)
    restarted = ResearchService(service.path)
    snapshot = restarted.snapshot()
    assert snapshot['runtime']['state'] == 'STOPPED'
    assert snapshot['runtime']['last_run']['restored_from_history'] is True
    assert snapshot['runtime']['last_run']['sources'][0]['subscription_id'] == saved['id']
    assert snapshot['runtime']['last_run']['sources'][0]['status'] == 'needs_configuration'


def test_opposing_active_reports_are_downgraded_without_rewriting_history(tmp_path):
    from datetime import UTC, datetime, timedelta
    from research.models import ResearchReport, ResearchInstrument, ResearchClaim, Citation
    now = datetime.now(UTC)
    service = configured(tmp_path)
    saved = service.import_text({'text': 'A complete source-backed material about company performance and future demand.',
                                'source_url': 'https://example.org/filing', 'title': 'Filing', 'published_at': now.isoformat()})
    with ResearchStore(service.path) as store:
        for identity, action in [('positive', 'buy_candidate'), ('negative', 'reduce_review')]:
            store.save_report(ResearchReport(
                id=identity, evidence_id=saved['id'], evidence_hash=saved['content_hash'],
                instrument=ResearchInstrument(market='US', ticker='AAPL'), horizon='swing',
                action=action, status='ready', extraction_status='analyzed', summary='Conditional hypothesis',
                created_at=now, valid_until=now + timedelta(hours=1), analyzer_version='test',
                reference_price=100.0, conditions=['Verify current conditions'], invalidation=['Thesis disproved'],
                claims=[ResearchClaim(kind='fact', text='Source claim', ticker='AAPL', direction='positive',
                    relationship='Company performance', citation=Citation(evidence_id=saved['id'],
                    source_url=saved['source_url'], quote='company performance'))]))
    reports = service.snapshot()['reports']
    assert all(r['action'] == 'watch' for r in reports)
    assert all('conflicting_sources_requires_review' in r['quality_issues'] for r in reports)
    assert service.detail('report', 'positive')['original_action'] == 'buy_candidate'
    with ResearchStore(service.path) as store:
        assert store.get_report('positive').action == 'buy_candidate'
        assert not store.conflicting_profiles(now=now + timedelta(hours=2))


@pytest.mark.parametrize('enriched', [
    {'content_text': '', 'summary': '', 'content_kind': 'metadata', 'completeness': 'metadata',
     'quality_issues': ['paywall_detected']},
    {'content_text': 'This is the publicly available portion of the article describing company results.',
     'summary': '', 'content_kind': 'article', 'completeness': 'partial',
     'quality_issues': ['paywall_detected'], 'external_id': 'different'},
])
def test_news_enrichment_preserves_original_summary_and_identity(tmp_path, monkeypatch, enriched):
    import hashlib
    from news.sources import NewsFetchReport, ProviderFetchResult
    original = {'title': 'Company results', 'url': 'https://example.org/results',
                'summary': 'Original provider summary remains useful when full text is inaccessible.',
                'source': 'Publisher'}
    report = NewsFetchReport([original], (ProviderFetchResult('newsapi', 'search', 'success', [original]),))
    monkeypatch.setattr('research.connectors.fetch_article', lambda *a, **kw: {'status': 'partial', 'item': enriched})
    service = ResearchService(tmp_path / 'state.db', collect_news=lambda *a, **kw: report,
                              analyzer=lambda *a, **kw: [])
    service.launch()
    service.wait(3)
    evidence = service.snapshot()['evidence'][0]
    assert evidence['summary'] == original['summary']
    assert evidence['external_id'] == hashlib.sha256(original['url'].encode()).hexdigest()
    assert evidence['completeness'] == 'partial'
    assert 'paywall_detected' in evidence['quality_issues']


@pytest.mark.parametrize('selection,expected', [([], {'finnhub'}), (['akshare'], {'akshare'})])
def test_news_source_selection_uses_configured_defaults_only_when_empty(tmp_path, monkeypatch, selection, expected):
    from config import NewsConfig
    from research.service import ResearchOptions
    config = NewsConfig.model_validate({'sources': {'finnhub': {'enabled': True}}})
    monkeypatch.setattr('config.load_news_config', lambda: config)
    captured = []
    service = ResearchService(tmp_path / 'sources.db', collect_news=lambda _stocks, _queries, sources, **kw:
                              captured.append(sources))
    service._news([], ResearchOptions(news_sources=selection))
    sources = captured[0]
    assert {name for name in ('finnhub', 'newsapi', 'akshare') if getattr(sources, name).enabled} == expected
    assert config.sources.finnhub.enabled is True
    assert config.sources.akshare.enabled is False


def test_symlink_database_cannot_bypass_cross_session_lease(tmp_path):
    from research.locking import ResearchLease
    database = tmp_path / 'state.db'
    database.touch()
    alias = tmp_path / 'alias.db'
    try:
        alias.symlink_to(database)
    except OSError:
        pytest.skip('symlinks unavailable on this host')
    with ResearchLease(database):
        with pytest.raises(ValueError, match='another session'):
            ResearchLease(alias)
    with ResearchLease(alias):
        pass
    assert ResearchService(alias).path == ResearchService(database).path


def test_store_pagination_is_ordered_and_validates_offsets(tmp_path):
    service = configured(tmp_path)
    for index in range(5):
        service.add_subscription({'platform': 'rss', 'url': f'https://example.org/{index}',
                                  'enabled': index % 2 == 0})
        service.import_text({'title': f'Article {index}', 'text': f'Original evidence {index}',
                             'source_url': f'https://example.org/article/{index}'})
    with ResearchStore(service.path) as store:
        assert [item.title for item in store.list_evidence(limit=2, offset=1)] == ['Article 3', 'Article 2']
        assert [item.url for item in store.list_subscriptions(enabled_only=True, limit=1, offset=1)] == ['https://example.org/2']
        for method in (store.list_evidence, store.list_reports, store.list_subscriptions):
            with pytest.raises(ValueError):
                method(offset=-1)
            with pytest.raises(ValueError):
                method(offset=True)
