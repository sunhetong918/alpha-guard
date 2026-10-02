"""Point-in-time follow-up of saved reports, not a trading backtest.

Only material and market inputs observable when a report was created can enter
its baseline. Later observations measure a price change after report expiry;
no historical report is regenerated and no future observation changes a thesis.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import ResearchInstrument, ResearchModel, aware, parse_time
from .store import ResearchStore


class PriceObservation(ResearchModel):
    instrument: ResearchInstrument
    price: float = Field(gt=0)
    currency: Literal['USD', 'HKD', 'CNY']
    source_as_of: datetime
    observed_at: datetime
    provider: str = Field(min_length=1, max_length=120)
    granularity: Literal['intraday', 'daily']
    corporate_action: bool = False

    @model_validator(mode='after')
    def validate_observation(self):
        if aware(self.observed_at) < aware(self.source_as_of):
            raise ValueError('an observation cannot precede its source timestamp')
        if self.currency != {'US': 'USD', 'HK': 'HKD', 'CN': 'CNY'}[self.instrument.market]:
            raise ValueError('observation currency does not match the market')
        return self


def track_reports(store: ResearchStore, price_samples: Sequence[Mapping[str, Any]], *,
                  as_of: datetime, report_limit: int = 1000) -> dict[str, Any]:
    """Track the first supplied price near expiry, grouping market and horizon.

    JSON inputs require explicit source and observation clocks. Missing prices
    remain pending. Returns are unadjusted price changes, never profit or win rate.
    """
    cutoff = aware(as_of)
    if len(price_samples) > 10_000 or type(report_limit) is not int or not 1 <= report_limit <= 1000:
        raise ValueError('tracking input is too large')
    samples: list[PriceObservation] = []
    for raw in price_samples:
        value = dict(raw)
        for key in ('source_as_of', 'observed_at'):
            value[key] = parse_time(value.get(key))
        samples.append(PriceObservation.model_validate(value))
    by_security: dict[str, list[PriceObservation]] = defaultdict(list)
    for sample in samples:
        if aware(sample.observed_at) <= cutoff and aware(sample.source_as_of) <= cutoff:
            by_security[sample.instrument.key].append(sample)
    for values in by_security.values():
        values.sort(key=lambda value: (value.source_as_of, value.observed_at, value.provider))
    rows: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for report in store.historical_reports(limit=report_limit):
        if report.created_at > cutoff:
            continue
        row: dict[str, Any] = {
            'report_id': report.id, 'instrument': report.instrument.model_dump(mode='json'),
            'horizon': report.horizon, 'original_action': report.action,
            'created_at': report.created_at.isoformat(), 'target_at': report.valid_until.isoformat(),
            'status': 'excluded', 'reason': None, 'price_change_pct': None,
            'direction_aligned_price_change_pct': None,
        }
        material = store.get_evidence(report.evidence_id)
        quote_time = parse_time(report.market_context.get('source_as_of'))
        observed_time = parse_time(report.market_context.get('observed_at'))
        dependency_clocks = [parse_time(report.market_context[key])
                             for key in ('volume_source_as_of', 'fundamentals_observed_at')
                             if report.market_context.get(key) is not None]
        if report.status != 'ready' or report.action not in {'buy_candidate', 'reduce_review'}:
            row['reason'] = 'not_a_directional_candidate'
        elif material is None or material.content_hash != report.evidence_hash:
            row['reason'] = 'original_evidence_unavailable'
        elif (material.published_at is None or material.published_at > report.created_at
              or material.discovered_at > report.created_at or material.retrieved_at > report.created_at):
            row['reason'] = 'evidence_not_available_at_creation'
        elif quote_time is None or observed_time is None or max(quote_time, observed_time) > report.created_at:
            row['reason'] = 'market_not_available_at_creation'
        elif observed_time < quote_time:
            row['reason'] = 'market_clock_inconsistent'
        elif any(clock is None or clock > report.created_at for clock in dependency_clocks):
            row['reason'] = 'market_dependency_not_available_at_creation'
        elif (report.market_context.get('market') != report.instrument.market
              or report.market_context.get('ticker') != report.instrument.ticker):
            row['reason'] = 'baseline_security_unverifiable'
        elif report.created_at - quote_time >= {'short': timedelta(minutes=15), 'swing': timedelta(days=3), 'long': timedelta(days=7)}[report.horizon]:
            row['reason'] = 'market_stale_at_creation'
        elif report.market_context.get('currency') != {'US': 'USD', 'HK': 'HKD', 'CN': 'CNY'}[report.instrument.market]:
            row['reason'] = 'baseline_currency_unverifiable'
        elif (report.reference_price is None or not math.isfinite(report.reference_price)
              or report.reference_price <= 0 or report.market_context.get('price') != report.reference_price):
            row['reason'] = 'reference_price_unverifiable'
        elif report.horizon == 'short' and report.instrument.market == 'CN':
            row['reason'] = 'cn_intraday_not_validated'
        else:
            row['status'] = 'pending'
            row['reason'] = 'observation_window_not_complete' if cutoff < report.valid_until else 'end_price_unavailable'
            # A later price far from the requested endpoint is not silently used.
            tolerance = timedelta(minutes=30) if report.horizon == 'short' else timedelta(days=4)
            candidates = [sample for sample in by_security[report.instrument.key]
                          if report.valid_until <= sample.source_as_of <= report.valid_until + tolerance
                          and (report.horizon != 'short' or sample.granularity == 'intraday')]
            if cutoff >= report.valid_until and candidates:
                end = candidates[0]
                endpoint_prices = {sample.price for sample in candidates
                                   if sample.source_as_of == end.source_as_of}
                intermediate = [sample for sample in by_security[report.instrument.key]
                                if quote_time <= sample.source_as_of <= end.source_as_of]
                if len(endpoint_prices) != 1:
                    row.update(status='excluded', reason='conflicting_end_prices')
                elif any(sample.corporate_action for sample in intermediate):
                    row.update(status='excluded', reason='corporate_action_requires_adjustment')
                else:
                    change: float | None = (end.price / report.reference_price - 1) * 100
                    if change is not None and not math.isfinite(change):
                        row.update(status='excluded', reason='price_change_unrepresentable')
                        change = None
                    row.update(status='observed' if change is not None else 'excluded',
                               reason=None if change is not None else 'price_change_unrepresentable',
                               reference_price=report.reference_price,
                               end_price=end.price, end_source_as_of=end.source_as_of.isoformat(),
                               end_observed_at=end.observed_at.isoformat(), end_provider=end.provider,
                               price_change_pct=change,
                               direction_aligned_price_change_pct=(change if report.action == 'buy_candidate' else -change) if change is not None else None)
        group_key = report.instrument.market, report.horizon
        group = groups.setdefault(group_key, {'market': group_key[0], 'horizon': group_key[1], 'reports': 0,
                                        'observed': 0, 'pending': 0, 'excluded': 0, 'changes': []})
        group['reports'] += 1
        group[row['status']] += 1
        if row['status'] == 'observed':
            group['changes'].append(row['direction_aligned_price_change_pct'])
        rows.append(row)
    summaries = []
    for group in groups.values():
        changes = group.pop('changes')
        group['mean_direction_aligned_price_change_pct'] = sum(value / len(changes) for value in changes) if changes else None
        summaries.append(group)
    return {
        'kind': 'historical_report_followup', 'as_of': cutoff.isoformat(), 'rows': rows,
        'summary_by_market_horizon': sorted(summaries, key=lambda group: (group['market'], group['horizon'])),
        'report_limit': report_limit,
        'reports_truncated': any(report.created_at <= cutoff
                                 for report in store.historical_reports(limit=1, offset=report_limit)),
        'limitations': ['supplied_prices_not_independently_verified', 'not_a_trading_backtest',
                        'no_fill_liquidity_fees_slippage_or_portfolio_model',
                        'unadjusted_price_change_not_investment_profit',
                        'unknown_corporate_actions_can_distort_price_changes',
                        'no_model_accuracy_or_win_rate_claim'],
    }
