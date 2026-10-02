from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from research.models import Citation, ResearchClaim, ResearchInstrument, ResearchReport
from research.replay import track_reports
from research.store import ResearchStore

NOW = datetime(2026, 9, 29, 14, tzinfo=UTC)


def create_report(store, *, market='US', horizon='swing', action='buy_candidate', evidence_changes=None, context_changes=None):
    instrument = ResearchInstrument(market=market, ticker={'US': 'AAPL', 'HK': '00700', 'CN': '600519'}[market])
    evidence = store.upsert_evidence({
        'title': f'{market} Filing', 'source_url': f'https://example.org/{market}/{horizon}',
        'published_at': (NOW - timedelta(hours=1)).isoformat(),
        'content_text': 'The company reported higher recurring revenue and stable cash generation.',
        'content_kind': 'article', 'completeness': 'full', **(evidence_changes or {})
    }, now=NOW - timedelta(minutes=2))
    context = {'ticker': instrument.ticker, 'market': market, 'price': 100.0,
               'currency': {'US': 'USD', 'HK': 'HKD', 'CN': 'CNY'}[market],
               'source_as_of': (NOW - timedelta(minutes=1)).isoformat(),
               'observed_at': (NOW - timedelta(seconds=20)).isoformat(), **(context_changes or {})}
    report = ResearchReport(id=f'{market}-{horizon}-{action}', evidence_id=evidence.id,
        evidence_hash=evidence.content_hash, instrument=instrument, horizon=horizon, action=action,
        status='ready', extraction_status='analyzed', analyzer_version='test', summary='A conditional research candidate',
        claims=[ResearchClaim(kind='fact', text='Reported recurring revenue', ticker=instrument.ticker,
            direction='positive', relationship='Company revenue', citation=Citation(
                evidence_id=evidence.id, source_url=evidence.source_url, quote='reported higher recurring revenue'))],
        reference_price=100.0, market_context=context, conditions=['Verify next filing'],
        invalidation=['Revenue growth reverses'], created_at=NOW, valid_until=NOW + timedelta(hours=1))
    store.save_report(report)
    return report


def sample(report, **changes):
    return {'instrument': report.instrument.model_dump(mode='json'), 'price': 110.0,
            'currency': report.market_context['currency'], 'source_as_of': report.valid_until.isoformat(),
            'observed_at': (report.valid_until + timedelta(minutes=1)).isoformat(),
            'provider': 'explicit-test-fixture', 'granularity': 'intraday', **changes}


def test_followup_groups_markets_and_keeps_original_expired_decision(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        reports = [create_report(store, market=market) for market in ('US', 'HK', 'CN')]
        result = track_reports(store, [sample(report) for report in reports], as_of=NOW + timedelta(days=1))
        assert len(result['summary_by_market_horizon']) == 3
        assert all(row['status'] == 'observed' for row in result['rows'])
        assert all(row['price_change_pct'] == pytest.approx(10) for row in result['rows'])
        assert store.historical_reports()[0].action == 'buy_candidate'
        assert 'no_fill_liquidity_fees_slippage_or_portfolio_model' in result['limitations']
        assert 'win_rate' not in result


@pytest.mark.parametrize('context_changes,reason', [
    ({'source_as_of': (NOW + timedelta(seconds=1)).isoformat()}, 'market_not_available_at_creation'),
    ({'observed_at': (NOW + timedelta(seconds=1)).isoformat()}, 'market_not_available_at_creation'),
    ({'source_as_of': (NOW - timedelta(days=10)).isoformat()}, 'market_stale_at_creation'),
    ({'price': 150}, 'reference_price_unverifiable'),
    ({'currency': 'CAD'}, 'baseline_currency_unverifiable'),
])
def test_future_or_unverifiable_baseline_is_excluded(tmp_path, context_changes, reason):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store, context_changes=context_changes)
        row = track_reports(store, [sample(report, currency='USD')], as_of=NOW + timedelta(days=1))['rows'][0]
        assert row['status'] == 'excluded'
        assert row['reason'] == reason
        assert row['price_change_pct'] is None


def test_future_material_never_enters_historical_baseline(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store, evidence_changes={'published_at': (NOW + timedelta(days=1)).isoformat()})
        row = track_reports(store, [sample(report)], as_of=NOW + timedelta(days=2))['rows'][0]
        assert row['reason'] == 'evidence_not_available_at_creation'


def test_observation_not_yet_available_is_pending_and_report_not_rewritten(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store)
        result = track_reports(store, [sample(report, observed_at=(NOW + timedelta(days=2)).isoformat())],
                               as_of=NOW + timedelta(days=1))
        assert result['rows'][0]['status'] == 'pending'
        assert result['rows'][0]['price_change_pct'] is None
        assert store.historical_reports()[0].reference_price == 100
        assert track_reports(store, [], as_of=NOW - timedelta(seconds=1))['rows'] == []


def test_daily_price_cannot_score_short_report_and_distant_price_is_not_substituted(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store, horizon='short')
        values = [sample(report, granularity='daily'), sample(report,
                    source_as_of=(NOW + timedelta(days=2)).isoformat(), observed_at=(NOW + timedelta(days=2)).isoformat())]
        row = track_reports(store, values, as_of=NOW + timedelta(days=3))['rows'][0]
        assert row['status'] == 'pending'
        assert row['reason'] == 'end_price_unavailable'


def test_reduce_review_price_change_is_labeled_without_claiming_profit(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store, action='reduce_review')
        row = track_reports(store, [sample(report, price=90.0)], as_of=NOW + timedelta(days=1))['rows'][0]
        assert row['price_change_pct'] == pytest.approx(-10)
        assert row['direction_aligned_price_change_pct'] == pytest.approx(10)
        assert 'profit' not in row


def test_corporate_actions_are_not_misreported_as_thesis_returns(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store)
        row = track_reports(store, [sample(report, price=55, corporate_action=True)], as_of=NOW + timedelta(days=1))['rows'][0]
        assert row['reason'] == 'corporate_action_requires_adjustment'
        assert row['price_change_pct'] is None


def test_naive_missing_and_nonfinite_price_inputs_rejected(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store)
        for changes in ({'source_as_of': '2026-09-29T15:00:00'}, {'price': float('nan')}, {'observed_at': None}, {'currency': 'CAD'}):
            with pytest.raises(ValueError):
                track_reports(store, [sample(report, **changes)], as_of=NOW + timedelta(days=1))


@pytest.mark.parametrize('context_changes,reason', [
    ({'ticker': 'MSFT'}, 'baseline_security_unverifiable'),
    ({'market': 'HK'}, 'baseline_security_unverifiable'),
    ({'observed_at': (NOW - timedelta(minutes=2)).isoformat()}, 'market_clock_inconsistent'),
    ({'volume_source_as_of': (NOW + timedelta(seconds=1)).isoformat()}, 'market_dependency_not_available_at_creation'),
    ({'fundamentals_observed_at': (NOW + timedelta(seconds=1)).isoformat()}, 'market_dependency_not_available_at_creation'),
])
def test_baseline_identity_and_all_known_dependency_clocks_are_verifiable(tmp_path, context_changes, reason):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store, context_changes=context_changes)
        row = track_reports(store, [sample(report)], as_of=NOW + timedelta(days=1))['rows'][0]
        assert row['status'] == 'excluded'
        assert row['reason'] == reason
        assert row['price_change_pct'] is None


def test_corporate_action_since_baseline_quote_prevents_false_price_change(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store)
        split = sample(report, price=50.0, corporate_action=True,
                       source_as_of=(NOW - timedelta(seconds=30)).isoformat(),
                       observed_at=NOW.isoformat())
        row = track_reports(store, [split, sample(report, price=55)],
                            as_of=NOW + timedelta(days=1))['rows'][0]
        assert row['reason'] == 'corporate_action_requires_adjustment'
        assert row['price_change_pct'] is None


def test_conflicting_endpoint_prices_are_not_selected_by_input_order(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        report = create_report(store)
        values = [sample(report, price=90.0), sample(report, price=110.0)]
        for samples in (values, list(reversed(values))):
            row = track_reports(store, samples, as_of=NOW + timedelta(days=1))['rows'][0]
            assert row['status'] == 'excluded' and row['reason'] == 'conflicting_end_prices'
            assert row['price_change_pct'] is None
        duplicate = track_reports(store, [values[0], values[0]], as_of=NOW + timedelta(days=1))['rows'][0]
        assert duplicate['status'] == 'observed'


def test_bounded_report_tracking_discloses_truncated_history(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        create_report(store, market='US')
        create_report(store, market='HK')
        result = track_reports(store, [], as_of=NOW + timedelta(days=1), report_limit=1)
        assert len(result['rows']) == 1 and result['reports_truncated'] is True
        assert track_reports(store, [], as_of=NOW + timedelta(days=1))['reports_truncated'] is False
