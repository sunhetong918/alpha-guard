"""Current views must not act on a superseded source version."""
from datetime import UTC, datetime, timedelta

import pytest

from research.analysis import analyze_evidence
from research.models import Citation, ResearchClaim, ResearchInstrument, ResearchReport
from research.replay import track_reports
from research.store import ResearchStore

NOW = datetime(2026, 10, 2, 3, tzinfo=UTC)


def material(**changes):
    return {'platform': 'rss', 'external_id': 'filing',
        'source_url': 'https://example.org/company/filing', 'title': 'Company filing',
        'author': 'Company', 'published_at': (NOW - timedelta(hours=1)).isoformat(),
        'content_text': 'The company reported higher recurring revenue and positive cash flow.',
        'content_kind': 'article', 'completeness': 'full', **changes}


def report_for(store, evidence, action='buy_candidate'):
    report = ResearchReport(id=evidence.id + action, evidence_id=evidence.id,
        evidence_hash=evidence.content_hash, instrument=ResearchInstrument(market='US', ticker='AAPL'),
        horizon='swing', action=action, status='ready', extraction_status='analyzed',
        summary='Conditional research candidate', analyzer_version='test',
        claims=[ResearchClaim(kind='fact', text='A source statement', ticker='AAPL',
            direction='positive' if action == 'buy_candidate' else 'negative', relationship='Revenue',
            citation=Citation(evidence_id=evidence.id, source_url=evidence.source_url,
                quote=evidence.content_text))],
        reference_price=100.0, conditions=['Check next filing'], invalidation=['Source withdraws the statement'],
        market_context={'ticker': 'AAPL', 'market': 'US', 'currency': 'USD', 'price': 100.0,
            'source_as_of': (NOW - timedelta(minutes=1)).isoformat(),
            'observed_at': (NOW - timedelta(seconds=20)).isoformat()},
        created_at=NOW, valid_until=NOW + timedelta(hours=1))
    return store.save_report(report)


@pytest.mark.parametrize('revision', [
    {'content_text': 'The company withdrew the previous revenue guidance and its earlier statement.'},
    {'content_text': '', 'summary': 'Only current source metadata is available.',
     'content_kind': 'metadata', 'completeness': 'metadata'},
    {'title': 'Corrected company filing'},
])
def test_current_report_views_downgrade_changed_source_without_mutating_history(tmp_path, revision):
    with ResearchStore(tmp_path / 'state.db') as store:
        evidence = store.upsert_evidence(material(), now=NOW - timedelta(minutes=2))
        original = report_for(store, evidence)
        newer = store.upsert_evidence(material(**revision), now=NOW + timedelta(minutes=1))
        assert newer.id != evidence.id
        for view in [store.get_report(original.id, now=NOW + timedelta(minutes=2)),
                     store.list_reports(now=NOW + timedelta(minutes=2))[0]]:
            assert view.action == 'watch' and view.status == 'insufficient_evidence'
            assert 'source_evidence_superseded' in view.quality_issues
            assert '新版本' in view.summary
            assert view.valid_until <= original.valid_until
            assert view.evidence_id == original.evidence_id and view.claims == original.claims
        assert store.historical_reports() == [original]
        assert store.get_evidence(evidence.id).content_text == evidence.content_text
        replay = track_reports(store, [], as_of=NOW + timedelta(hours=2))
        assert replay['rows'][0]['original_action'] == 'buy_candidate'
        assert replay['rows'][0]['reason'] != 'not_a_directional_candidate'


def test_same_version_reread_keeps_report_and_clocks(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        evidence = store.upsert_evidence(material(), now=NOW - timedelta(minutes=2))
        original = report_for(store, evidence)
        same = store.upsert_evidence(material(), now=NOW + timedelta(minutes=1))
        assert same.id == evidence.id and same.retrieved_at == evidence.retrieved_at
        assert store.get_report(original.id, now=NOW + timedelta(minutes=2)) == original


def test_analyzing_old_record_does_not_restore_a_superseded_report(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        evidence = store.upsert_evidence(material(), now=NOW - timedelta(minutes=2))
        original = report_for(store, evidence)
        store.upsert_evidence(material(content_text='', summary='Newest metadata',
            content_kind='metadata', completeness='metadata'), now=NOW + timedelta(minutes=1))
        analyze_evidence(evidence, [], horizons=['swing'], store=store, ai_enabled=False)
        store.upsert_evidence(evidence)
        view = store.get_report(original.id, now=NOW + timedelta(minutes=2))
        assert view.action == 'watch' and 'source_evidence_superseded' in view.quality_issues
        assert store.historical_reports() == [original]


def test_superseded_expired_report_never_gets_new_validity(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        evidence = store.upsert_evidence(material(), now=NOW - timedelta(minutes=2))
        original = report_for(store, evidence)
        store.upsert_evidence(material(content_text='Corrected source text.'), now=NOW + timedelta(minutes=1))
        view = store.get_report(original.id, now=NOW + timedelta(hours=2))
        assert view.action == 'watch' and view.status == 'expired'
        assert view.valid_until == original.valid_until
        assert {'source_evidence_superseded', 'recommendation_expired'} <= set(view.quality_issues)


def test_legacy_no_head_uses_latest_source_and_preserves_unchanged_report(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        evidence = store.upsert_evidence(material(), now=NOW - timedelta(minutes=2))
        original = report_for(store, evidence)
        with store._connection:
            store._connection.execute('DELETE FROM research_source_heads')
        assert store.get_report(original.id, now=NOW) == original
        store.upsert_evidence(material(content_text='The earlier statement has been withdrawn.'),
            now=NOW + timedelta(minutes=1))
        with store._connection:
            store._connection.execute('DELETE FROM research_source_heads')
        assert store.get_report(original.id, now=NOW + timedelta(minutes=2)).action == 'watch'
        assert store.historical_reports() == [original]


@pytest.mark.parametrize('legacy', [False, True])
def test_retracted_direction_does_not_conflict_with_its_current_revision(tmp_path, legacy):
    with ResearchStore(tmp_path / 'state.db') as store:
        first = store.upsert_evidence(material(), now=NOW - timedelta(minutes=2))
        report_for(store, first, 'buy_candidate')
        revised = store.upsert_evidence(material(content_text='The company reported lower revenue and withdrew guidance.'),
            now=NOW - timedelta(minutes=1))
        current = report_for(store, revised, 'reduce_review')
        if legacy:
            with store._connection:
                store._connection.execute('DELETE FROM research_source_heads')
        assert store.conflicting_profiles(now=NOW) == set()
        assert store.get_report(current.id, now=NOW).action == 'reduce_review'
        independent = store.upsert_evidence(material(external_id='independent',
            source_url='https://other.example/independent-filing'), now=NOW - timedelta(seconds=30))
        report_for(store, independent, 'buy_candidate')
        assert store.conflicting_profiles(now=NOW) == {('US', 'AAPL', 'swing')}
        assert len(store.historical_reports()) == 3


def test_current_evidence_pages_heads_before_limit_and_keeps_history(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        initial = store.upsert_evidence(material(), now=NOW)
        other = store.upsert_evidence(material(external_id='other', source_url='https://other.example/filing'), now=NOW)
        for index in range(5):
            latest = store.upsert_evidence(material(content_text=f'Revision number {index}'),
                now=NOW + timedelta(minutes=index + 1))
        assert [item.id for item in store.list_current_evidence(limit=2)] == [latest.id, other.id]
        assert [item.id for item in store.list_current_evidence(limit=1, offset=1)] == [other.id]
        assert len(store.list_evidence()) == 7
        store.upsert_evidence(material(), now=NOW + timedelta(days=1))
        current = store.list_current_evidence()
        assert {item.id for item in current} == {initial.id, other.id}
        assert next(item for item in current if item.id == initial.id).retrieved_at == NOW
        # A legacy source without a recorded head still has one current version.
        with store._connection:
            store._connection.execute('DELETE FROM research_source_heads')
        assert {item.id for item in store.list_current_evidence()} == {latest.id, other.id}
