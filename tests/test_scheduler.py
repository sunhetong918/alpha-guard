from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from scheduler import (
    DEFAULT_MISFIRE_GRACE_TIME,
    MARKET_SCHEDULES,
    NEWS_HOURS,
    NEWS_TIMEZONE,
    build_scheduler,
    expected_market_scans_between,
    is_market_session,
    latest_expected_market_scan,
    market_freshness_context,
    next_market_run,
    next_news_run,
    run_daily_summary,
    run_market_scan,
)

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")


def test_market_mapping_and_exchange_holidays_are_separate() -> None:
    assert MARKET_SCHEDULES["US"].exchange == "XNYS"
    assert MARKET_SCHEDULES["HK"].exchange == "XHKG"
    assert MARKET_SCHEDULES["US"].scan_at.isoformat() == "09:25:00"
    assert MARKET_SCHEDULES["HK"].scan_at.isoformat() == "09:25:00"

    assert not is_market_session("US", date(2024, 7, 4))
    assert is_market_session("US", date(2024, 7, 5))
    assert not is_market_session("HK", date(2024, 10, 1))
    assert is_market_session("HK", date(2024, 10, 2))


def test_us_next_run_tracks_dst_and_skips_holiday() -> None:
    winter = next_market_run("US", datetime(2024, 1, 7, 12, tzinfo=UTC))
    summer = next_market_run("US", datetime(2024, 7, 7, 12, tzinfo=UTC))

    assert (winter.hour, winter.minute, winter.tzinfo) == (9, 25, NEW_YORK)
    assert (summer.hour, summer.minute, summer.tzinfo) == (9, 25, NEW_YORK)
    assert winter.astimezone(UTC).hour == 14
    assert summer.astimezone(UTC).hour == 13

    after_july_third_scan = datetime(2024, 7, 3, 10, tzinfo=NEW_YORK)
    assert next_market_run("US", after_july_third_scan).date() == date(2024, 7, 5)


def test_hk_next_run_has_stable_utc_offset() -> None:
    winter = next_market_run("HK", datetime(2024, 1, 7, 12, tzinfo=UTC))
    summer = next_market_run("HK", datetime(2024, 7, 7, 12, tzinfo=UTC))
    assert (winter.hour, winter.minute, winter.tzinfo) == (9, 25, HONG_KONG)
    assert (summer.hour, summer.minute, summer.tzinfo) == (9, 25, HONG_KONG)
    assert (winter.astimezone(UTC).hour, summer.astimezone(UTC).hour) == (1, 1)


def test_news_runs_are_fixed_shanghai_wall_clock_hours() -> None:
    assert NEWS_HOURS == (0, 4, 8, 12, 16, 20)
    before = datetime(2024, 6, 1, 3, 59, 59, tzinfo=NEWS_TIMEZONE)
    assert next_news_run(before) == datetime(2024, 6, 1, 4, 0, tzinfo=NEWS_TIMEZONE)
    exactly = datetime(2024, 6, 1, 20, 0, tzinfo=NEWS_TIMEZONE)
    assert next_news_run(exactly) == datetime(2024, 6, 2, 0, 0, tzinfo=NEWS_TIMEZONE)


def test_market_guard_supports_sync_and_async_callbacks() -> None:
    calls: list[str] = []

    async def callback(market: str) -> None:
        calls.append(market)

    holiday = datetime(2024, 7, 4, 9, 25, tzinfo=NEW_YORK)
    session = datetime(2024, 7, 5, 9, 25, tzinfo=NEW_YORK)
    assert not asyncio.run(run_market_scan("US", callback, holiday))
    assert asyncio.run(run_market_scan("US", callback, session))
    assert calls == ["US"]

    no_argument_summary_calls: list[bool] = []

    def summary() -> None:
        no_argument_summary_calls.append(True)

    assert asyncio.run(run_daily_summary("US", summary, session))
    assert no_argument_summary_calls == [True]


def test_build_scheduler_configures_local_cron_and_overlap_safety() -> None:
    async def scan_market(market: str) -> None:
        del market

    def scan_news() -> None:
        return None

    def summary(market: str) -> None:
        del market

    scheduler = build_scheduler(scan_market, scan_news, summary)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {
        "market-scan:US",
        "market-scan:HK",
        "daily-summary:US",
        "daily-summary:HK",
        "news-scan",
    }

    for job in jobs.values():
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == DEFAULT_MISFIRE_GRACE_TIME

    assert jobs["market-scan:US"].trigger.timezone == NEW_YORK
    assert jobs["market-scan:HK"].trigger.timezone == HONG_KONG
    assert jobs["news-scan"].trigger.timezone == NEWS_TIMEZONE


def test_freshness_context_uses_completed_exchange_sessions_not_fixed_age() -> None:
    july_four = datetime(2024, 7, 4, 12, tzinfo=NEW_YORK)
    holiday = market_freshness_context("US", july_four)
    assert holiday.market_phase == "closed"
    # July 3, 2024 was an exchange early close; the calendar, not a hard-coded
    # 24-hour offset, supplies the 17:00 UTC watermark.
    assert holiday.expected_source_after == datetime(2024, 7, 3, 17, tzinfo=UTC)

    pre_open = market_freshness_context(
        "US", datetime(2024, 7, 5, 9, 25, tzinfo=NEW_YORK)
    )
    assert pre_open.market_phase == "pre_open"
    assert pre_open.expected_source_after == holiday.expected_source_after

    open_session = market_freshness_context(
        "US", datetime(2024, 7, 5, 10, 0, tzinfo=NEW_YORK)
    )
    assert open_session.market_phase == "open"
    assert open_session.expected_source_after is None

    # XNYS has no lunch break, so noon remains an open-session context.
    us_noon = market_freshness_context(
        "US", datetime(2024, 7, 5, 12, 0, tzinfo=NEW_YORK)
    )
    assert us_noon.market_phase == "open"


def test_hk_lunch_break_uses_break_watermark_then_reopens() -> None:
    before_break = market_freshness_context(
        "HK", datetime(2024, 7, 5, 12, 0, tzinfo=HONG_KONG)
    )
    during_break = market_freshness_context(
        "HK", datetime(2024, 7, 5, 12, 45, tzinfo=HONG_KONG)
    )
    resumed = market_freshness_context(
        "HK", datetime(2024, 7, 5, 13, 0, tzinfo=HONG_KONG)
    )

    assert before_break.market_phase == "closed"
    assert before_break.expected_source_after == datetime(
        2024, 7, 5, 4, 0, tzinfo=UTC
    )
    assert during_break.market_phase == "closed"
    assert during_break.expected_source_after == before_break.expected_source_after
    assert resumed.market_phase == "open"
    assert resumed.expected_source_after is None


def test_latest_expected_window_skips_holiday_and_tracks_dst_deadline() -> None:
    before_friday_scan = datetime(2024, 7, 5, 9, 0, tzinfo=NEW_YORK)
    previous = latest_expected_market_scan("US", before_friday_scan)
    assert previous.session == date(2024, 7, 3)
    assert previous.expected_at.hour == 9
    assert previous.deadline_at.hour == 9
    assert previous.deadline_at.minute == 40

    after_friday_scan = datetime(2024, 7, 5, 9, 41, tzinfo=NEW_YORK)
    due = latest_expected_market_scan("US", after_friday_scan)
    assert due.session == date(2024, 7, 5)
    assert due.deadline_at.astimezone(UTC).hour == 13

    winter = latest_expected_market_scan(
        "US", datetime(2024, 1, 8, 10, 0, tzinfo=NEW_YORK)
    )
    assert winter.deadline_at.astimezone(UTC).hour == 14


def test_expected_slo_windows_include_sessions_only() -> None:
    windows = expected_market_scans_between(
        "US",
        datetime(2024, 7, 1, tzinfo=UTC),
        datetime(2024, 7, 7, 23, 59, tzinfo=UTC),
    )
    assert [window.session for window in windows] == [
        date(2024, 7, 1),
        date(2024, 7, 2),
        date(2024, 7, 3),
        date(2024, 7, 5),
    ]

    before_grace = expected_market_scans_between(
        "US",
        datetime(2024, 7, 5, 0, 0, tzinfo=NEW_YORK),
        datetime(2024, 7, 5, 9, 30, tzinfo=NEW_YORK),
    )
    after_grace = expected_market_scans_between(
        "US",
        datetime(2024, 7, 5, 0, 0, tzinfo=NEW_YORK),
        datetime(2024, 7, 5, 9, 40, tzinfo=NEW_YORK),
    )
    assert before_grace == ()
    assert [window.session for window in after_grace] == [date(2024, 7, 5)]


def test_scheduler_optionally_adds_watchdog_without_changing_default_jobs() -> None:
    async def scan_market(market: str) -> None:
        del market

    events: list[object] = []
    scheduler = build_scheduler(
        scan_market,
        lambda: None,
        trust_watchdog=lambda: None,
        event_listener=events.append,
    )
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert "trust-watchdog" in jobs
    assert jobs["trust-watchdog"].max_instances == 1


@pytest.mark.parametrize("market", ["US", "HK", "XNYS", "XHKG"])
def test_market_helpers_reject_naive_time_but_accept_exchange_aliases(
    market: str,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_market_run(market, datetime(2024, 1, 1))
