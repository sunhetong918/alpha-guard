from __future__ import annotations

import pytest

from config import Settings
from guardian.application import GuardianApplication
from guardian.backend import AlphaGuardBackend
from guardian.preferences import PreferencesStore
from guardian.protocol import RpcErrorResponse, RpcRequest, RpcSuccessResponse, serialize_response

TOKEN = 'a' * 43


def request(method, params=None, token=TOKEN):
    return RpcRequest.model_validate({'jsonrpc': '2.0', 'id': 'research-test', 'method': method,
                                     'params': params or {}, 'auth_token': token})


@pytest.fixture
def application(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr('guardian.backend.STATE_PATH', path)
    backend = AlphaGuardBackend(settings_loader=lambda: Settings(_env_file=None),
                                preferences_store=PreferencesStore(tmp_path / 'preferences.json'))
    yield GuardianApplication(TOKEN, dispatcher=backend), path
    backend.close_research()


def test_research_methods_reject_unauthenticated_before_read_or_mutation(application):
    app, path = application
    for method in ('research.get', 'research.command', 'research.detail'):
        response = app.handle(request(method, token='b' * 43))
        assert isinstance(response, RpcErrorResponse)
        assert response.error.data.kind == 'unauthorized'
    assert not path.exists()


def test_research_ipc_import_and_paginated_detail_are_complete_and_bounded(application):
    app, _ = application
    content = '可核验的访谈原文与事实陈述。' * 15000
    response = app.handle(request('research.command', {'command': 'import', 'material': {
        'text': content, 'title': '访谈', 'source_url': 'https://example.org/interview'}}))
    assert isinstance(response, RpcSuccessResponse)
    identity = response.result['result']['id']
    offset, parts = 0, []
    while True:
        response = app.handle(request('research.detail', {'kind': 'evidence', 'id': identity, 'offset': offset}))
        assert isinstance(response, RpcSuccessResponse)
        assert len(serialize_response(response)) < 1_048_576
        parts.append(response.result['content_text'])
        offset = response.result['content_next_offset']
        if offset is None:
            break
    assert ''.join(parts) == content
    state = app.handle(request('research.get'))
    assert isinstance(state, RpcSuccessResponse)
    assert state.result['runtime']['state'] == 'STOPPED'


def test_invalid_research_command_does_not_reflect_secret_values(application):
    app, _ = application
    response = app.handle(request('research.command', {'command': 'configure', 'settings': {
        'api_key': 'super-private-credential'}}))
    assert isinstance(response, RpcErrorResponse)
    assert response.error.data.kind == 'invalid_params'
    assert 'super-private' not in response.model_dump_json()


@pytest.mark.parametrize('params', [{'page': True}, {'page': -1}, {'page': '1'}, {'page': 0, 'api_key': 'secret'}])
def test_research_snapshot_rejects_invalid_page(application, params):
    app, _ = application
    response = app.handle(request('research.get', params))
    assert isinstance(response, RpcErrorResponse)
    assert response.error.data.kind == 'invalid_params'


def test_large_snapshot_pages_are_bounded_and_keep_all_recent_items(application):
    from datetime import UTC, datetime, timedelta
    from research.models import ResearchInstrument, ResearchReport
    from research.store import ResearchStore
    app, path = application
    now = datetime.now(UTC)
    emoji = '🚀'
    with ResearchStore(path) as store:
        store.save_settings({'instruments': [{'market': 'US', 'ticker': f'TEST{i}', 'name': emoji * 200}
                                             for i in range(50)]})
        evidence_ids, report_ids = [], []
        for index in range(105):
            url = f'https://example.org/{index}/' + emoji * 2000
            if index < 100:
                store.upsert_subscription({'id': str(index), 'platform': 'rss', 'url': url,
                                           'feed_url': url + '?rss', 'label': emoji * 200})
            saved = store.upsert_evidence({'source_url': url, 'title': emoji * 1000,
                'author': emoji * 300, 'summary': emoji * 20000, 'content_text': emoji * 1000 + str(index),
                'content_kind': 'article', 'completeness': 'full', 'published_at': now.isoformat(),
                'quality_issues': [emoji * 1000] * 100})
            evidence_ids.append(saved.id)
            report = ResearchReport(id=str(index), evidence_id=saved.id, evidence_hash=saved.content_hash,
                instrument=ResearchInstrument(market='US', ticker='AAPL', name=emoji * 200), horizon='swing',
                action='watch', status='insufficient_evidence', extraction_status='analyzed',
                summary=emoji * 10000, thesis=emoji * 40000, quality_issues=[emoji * 1000] * 100,
                created_at=now + timedelta(microseconds=index), valid_until=now + timedelta(hours=1), analyzer_version='test')
            store.save_report(report)
            report_ids.append(report.id)
        sources = [{'subscription_id': str(i), 'label': emoji * 200, 'status': 'failed',
                    'detail': 'source_failed', 'fetched': 0} for i in range(100)]
        sources[0]['sources'] = [{'provider': 'newsapi', 'status': 'failed', 'operation': 'search',
                                  'error_code': 'provider_failed', 'article_count': 0}] * 100
        for _ in range(3):
            store.record_run('failed', stats={'fetched': 0, 'sources': sources})
    merged = {name: [] for name in ('subscriptions', 'evidence', 'reports', 'runs', 'sources')}
    page = 0
    while True:
        response = app.handle(request('research.get', {'page': page}))
        assert isinstance(response, RpcSuccessResponse)
        assert len(serialize_response(response)) < 1_048_576
        result = response.result
        assert result['page'] == page and result['window_limit'] == 100
        for key in ('subscriptions', 'evidence', 'reports', 'runs'):
            assert len(result[key]) <= 20
            merged[key].extend(result[key])
        merged['sources'].extend(result['runtime']['last_run']['sources'])
        page = result['next_page']
        if page is None:
            break
        assert page < 5
    assert len(merged['subscriptions']) == len(merged['evidence']) == len(merged['reports']) == len(merged['sources']) == 100
    assert [value['id'] for value in merged['evidence']] == evidence_ids[-100:][::-1]
    assert [value['id'] for value in merged['reports']] == report_ids[-100:][::-1]
    assert len(merged['runs']) == 3
    assert all('sources' not in run['stats'] for run in merged['runs'])
    assert app.handle(request('research.get', {'page': 99})).result['next_page'] is None
    with ResearchStore(path) as store:
        assert store.get_evidence(evidence_ids[0]) is not None
        assert store.get_report(report_ids[0]) is not None



def test_long_report_pages_every_explanation_list_without_loss(application):
    from datetime import UTC, datetime, timedelta
    from research.models import Citation, ResearchClaim, ResearchInstrument, ResearchReport
    from research.store import ResearchStore
    app, path = application
    now = datetime.now(UTC)
    emoji = '🚀'
    with ResearchStore(path) as store:
        saved = store.upsert_evidence({'source_url': 'https://example.org/' + emoji * 2000,
            'title': 'Long interview', 'content_text': emoji * 1200, 'content_kind': 'article',
            'completeness': 'full', 'published_at': now.isoformat()})
        lists = {key: [str(index) + emoji * 1190 for index in range(45)] for key in
                 ('catalysts', 'counter_evidence', 'conditions', 'invalidation', 'verification_steps')}
        claims = [ResearchClaim(kind='fact', text=emoji * 1600, ticker='AAPL', direction='positive',
            relationship=emoji * 1200, citation=Citation(evidence_id=saved.id,
            source_url=saved.source_url, quote=emoji * 1200)) for _ in range(21)]
        report = ResearchReport(id='long-report', evidence_id=saved.id, evidence_hash=saved.content_hash,
            instrument=ResearchInstrument(market='US', ticker='AAPL'), horizon='long', action='watch',
            status='insufficient_evidence', extraction_status='analyzed', summary='Research',
            thesis=emoji * 40000, claims=claims, created_at=now, valid_until=now + timedelta(days=1),
            analyzer_version='test', **lists)
        store.save_report(report)
    offset = 0
    merged = {key: [] for key in ('claims', *lists)}
    while True:
        response = app.handle(request('research.detail', {'kind': 'report', 'id': report.id, 'offset': offset}))
        assert isinstance(response, RpcSuccessResponse)
        assert len(serialize_response(response)) < 1_048_576
        for key in merged:
            assert len(response.result[key]) <= 10
            merged[key].extend(response.result[key])
        offset = response.result['claim_next_offset']
        if offset is None:
            break
    assert merged['claims'] == [claim.model_dump(mode='json') for claim in claims]
    assert all(merged[key] == values for key, values in lists.items())


def test_login_ipc_requires_auth_before_opening_browser(application, monkeypatch):
    from research.service import ResearchService
    monkeypatch.setattr(ResearchService, 'open_platform_login', lambda *_: pytest.fail('must authenticate first'))
    app, path = application
    response = app.handle(request('research.command', {'command': 'open_login', 'platform': 'youtube'}, token='b' * 43))
    assert isinstance(response, RpcErrorResponse)
    assert response.error.data.kind == 'unauthorized'
    assert not path.exists()


@pytest.mark.parametrize('params', [
    {'command': 'open_login', 'platform': 'https://example.org/private'},
    {'command': 'open_login', 'platform': 'youtube', 'url': 'https://example.org/private'},
    {'command': 'open_login', 'platform': ['youtube']},
    {'command': 'open_login', 'platform': 'rss'},
])
def test_login_ipc_rejects_arbitrary_fields_or_platforms(application, monkeypatch, params):
    from research.service import ResearchService
    monkeypatch.setattr(ResearchService, 'open_platform_login', lambda *_: pytest.fail('must validate first'))
    app, _ = application
    response = app.handle(request('research.command', params))
    assert isinstance(response, RpcErrorResponse)
    assert response.error.data.kind == 'invalid_params'
    assert 'example.org' not in response.model_dump_json()


def test_login_ipc_default_disabled_and_existing_mode_are_explicit(application, monkeypatch):
    from research import browser_source
    app, _ = application
    monkeypatch.setattr(browser_source, '_chrome_executable', lambda: pytest.fail('must not launch'))
    for enabled, mode, code in [(False, 'dedicated', 'browser_disabled'),
                                (True, 'existing', 'browser_login_requires_dedicated')]:
        monkeypatch.setattr('config.get_settings', lambda: Settings(_env_file=None,
            ALPHA_GUARD_BROWSER_ENABLED=enabled, ALPHA_GUARD_BROWSER_MODE=mode))
        response = app.handle(request('research.command', {'command': 'open_login', 'platform': 'youtube'}))
        assert isinstance(response, RpcSuccessResponse)
        assert response.result['accepted'] is False
        assert response.result['result']['detail'] == code
        assert response.result['result']['login_verified'] is False


def test_login_ipc_does_not_collect_or_activate_and_restart_stays_stopped(application, monkeypatch):
    from research import browser_source
    from research.service import ResearchService
    app, path = application
    monkeypatch.setattr('config.get_settings', lambda: Settings(_env_file=None,
        ALPHA_GUARD_BROWSER_ENABLED=True, ALPHA_GUARD_BROWSER_MODE='dedicated'))
    calls = []
    def open_login(platform, **options):
        calls.append((platform, options))
        return {'status': 'opening', 'platform': platform, 'login_verified': False}
    monkeypatch.setattr(browser_source, 'open_platform_login', open_login)
    monkeypatch.setattr(ResearchService, 'launch', lambda *a, **kw: pytest.fail('login cannot start research'))
    response = app.handle(request('research.command', {'command': 'open_login', 'platform': 'xiaohongshu'}))
    assert isinstance(response, RpcSuccessResponse) and response.result['accepted'] is True
    assert calls == [('xiaohongshu', {'enabled': True, 'mode': 'dedicated'})]
    assert not path.exists()
    state = app.handle(request('research.get')).result
    assert state['runtime']['state'] == 'STOPPED' and state['runtime']['next_run_at'] is None
    assert state['runs'] == [] and state['capabilities']['browser_mode'] == 'dedicated'
    restarted = ResearchService(path)
    try:
        state = restarted.snapshot()
        assert state['runtime']['state'] == 'STOPPED'
        assert state['runtime']['next_run_at'] is None and state['runs'] == []
    finally:
        restarted.close()
