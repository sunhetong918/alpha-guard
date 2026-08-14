"""Market-aware scheduling primitives for Alpha Guard.

Cron triggers express local wall-clock intent.  Exchange calendars remain a
separate execution guard so weekends and exchange holidays are deterministic
and directly testable without starting a scheduler.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Literal
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from reliability import FreshnessContext

NEWS_HOURS = (0, 4, 8, 12, 16, 20)
NEWS_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_MISFIRE_GRACE_TIME = 300
DEFAULT_PROTECTION_GRACE = timedelta(minutes=15)
DEFAULT_WATCHDOG_INTERVAL_MINUTES = 5


@dataclass(frozen=True, slots=True)
class MarketSchedule:
    """Wall-clock and exchange metadata for one business market."""

    market: str
    exchange: str
    timezone: ZoneInfo
    scan_at: time = time(9, 25)
    summary_at: time = time(16, 10)

    def __getitem__(self, field: str) -> Any:
        """Also support dictionary-style reads in configuration consumers."""

        return getattr(self, field)


@dataclass(frozen=True, slots=True)
class ExpectedMarketWindow:
    """One exchange-session scan promise and its user-facing deadline."""

    market: str
    exchange: str
    session: date
    expected_at: datetime
    deadline_at: datetime

    @property
    def key(self) -> str:
        return f"{self.market}:{self.session.isoformat()}"


MARKET_SCHEDULES: Mapping[str, MarketSchedule] = MappingProxyType(
    {
        "US": MarketSchedule(
            market="US",
            exchange="XNYS",
            timezone=ZoneInfo("America/New_York"),
        ),
        "HK": MarketSchedule(
            market="HK",
            exchange="XHKG",
            timezone=ZoneInfo("Asia/Hong_Kong"),
        ),
    }
)

_MARKET_BY_EXCHANGE = {
    schedule.exchange: market for market, schedule in MARKET_SCHEDULES.items()
}
_JOB_OPTIONS = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": DEFAULT_MISFIRE_GRACE_TIME,
}

MarketCallback = Callable[[str], object]
NoArgumentCallback = Callable[[], object]
SummaryCallback = Callable[..., object]
SchedulerEventCallback = Callable[[Any], object]


def _schedule_for(market: str) -> MarketSchedule:
    if not isinstance(market, str):
        raise TypeError("market must be a string")
    key = market.upper()
    key = _MARKET_BY_EXCHANGE.get(key, key)
    try:
        return MARKET_SCHEDULES[key]
    except KeyError as exc:
        supported = ", ".join(MARKET_SCHEDULES)
        raise ValueError(
            f"unsupported market {market!r}; expected one of {supported}"
        ) from exc


def _aware(moment: datetime | None) -> datetime:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


@lru_cache(maxsize=None)
def exchange_calendar(exchange: str) -> Any:
    """Return a cached exchange-calendars calendar by canonical name."""

    return xcals.get_calendar(exchange)


def is_exchange_session(exchange: str, day: date) -> bool:
    """Return whether ``day`` is a session on a canonical exchange calendar."""

    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError("day must be a date, not a datetime")
    session_label = pd.Timestamp(day.isoformat())
    return bool(exchange_calendar(exchange).is_session(session_label))


def is_market_session(market: str, value: date | datetime) -> bool:
    """Return whether the market-local date containing ``value`` is a session."""

    schedule = _schedule_for(market)
    if isinstance(value, datetime):
        local_day = _aware(value).astimezone(schedule.timezone).date()
    elif isinstance(value, date):
        local_day = value
    else:
        raise TypeError("value must be a date or timezone-aware datetime")
    return is_exchange_session(schedule.exchange, local_day)


def should_run_market(market: str, now: datetime | None = None) -> bool:
    """Guard a cron-fired market job against weekends and exchange holidays."""

    return is_market_session(market, _aware(now))


def _session_label_for_day(
    schedule: MarketSchedule,
    day: date,
    *,
    direction: str,
) -> pd.Timestamp:
    """Resolve a local date to an exchange session without inventing holidays."""

    calendar = exchange_calendar(schedule.exchange)
    return calendar.date_to_session(pd.Timestamp(day.isoformat()), direction=direction)


def _session_instant(calendar: Any, label: pd.Timestamp, field: str) -> datetime:
    value = getattr(calendar, field)(label)
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):  # pragma: no cover - calendar API guard
        raise TypeError(f"exchange calendar returned a non-datetime {field}")
    return _aware(value).astimezone(timezone.utc)


def _optional_session_instant(
    calendar: Any, label: pd.Timestamp, field: str
) -> datetime | None:
    """Return a session break instant, tolerating no-break calendars/NaT."""

    method = getattr(calendar, field, None)
    if not callable(method):
        return None
    value = method(label)
    if value is None or bool(pd.isna(value)):
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):  # pragma: no cover - calendar API guard
        return None
    return _aware(value).astimezone(timezone.utc)


def market_freshness_context(
    market: str,
    at: datetime | None = None,
) -> FreshnessContext:
    """Build event-time freshness context from the real exchange session.

    During pre-open, post-close, weekends and exchange holidays, the source
    watermark is the latest *completed* session close. During an open session,
    wall-clock freshness applies because a previous close is no longer enough.
    """

    schedule = _schedule_for(market)
    evaluated_at = _aware(at).astimezone(timezone.utc)
    local_day = evaluated_at.astimezone(schedule.timezone).date()
    calendar = exchange_calendar(schedule.exchange)

    if is_exchange_session(schedule.exchange, local_day):
        session = _session_label_for_day(schedule, local_day, direction="none")
        session_open = _session_instant(calendar, session, "session_open")
        session_close = _session_instant(calendar, session, "session_close")
        break_start = _optional_session_instant(
            calendar, session, "session_break_start"
        )
        break_end = _optional_session_instant(
            calendar, session, "session_break_end"
        )
        phase: Literal["closed", "pre_open", "post_close"]
        if evaluated_at < session_open:
            phase = "pre_open"
            completed = calendar.previous_session(session)
        elif (
            break_start is not None
            and break_end is not None
            and break_start <= evaluated_at < break_end
        ):
            return FreshnessContext(
                evaluated_at=evaluated_at,
                market_phase="closed",
                expected_source_after=break_start,
            )
        elif evaluated_at <= session_close:
            return FreshnessContext(
                evaluated_at=evaluated_at,
                market_phase="open",
                expected_source_after=None,
            )
        else:
            phase = "post_close"
            completed = session
    else:
        phase = "closed"
        completed = _session_label_for_day(schedule, local_day, direction="previous")

    return FreshnessContext(
        evaluated_at=evaluated_at,
        market_phase=phase,
        expected_source_after=_session_instant(
            calendar, completed, "session_close"
        ),
    )


def latest_expected_market_scan(
    market: str,
    at: datetime | None = None,
    *,
    grace: timedelta = DEFAULT_PROTECTION_GRACE,
) -> ExpectedMarketWindow:
    """Return the latest exchange-session scan that should have run by ``at``."""

    if grace < timedelta(0):
        raise ValueError("grace must be non-negative")
    schedule = _schedule_for(market)
    reference = _aware(at).astimezone(schedule.timezone)
    local_day = reference.date()
    if is_exchange_session(schedule.exchange, local_day):
        today_expected = datetime.combine(
            local_day, schedule.scan_at, tzinfo=schedule.timezone
        )
        if reference >= today_expected:
            session_day = local_day
        else:
            today_session = _session_label_for_day(
                schedule, local_day, direction="none"
            )
            session_day = calendar_session_date(
                exchange_calendar(schedule.exchange).previous_session(today_session)
            )
    else:
        session_day = calendar_session_date(
            _session_label_for_day(schedule, local_day, direction="previous")
        )

    expected = datetime.combine(
        session_day, schedule.scan_at, tzinfo=schedule.timezone
    )
    return ExpectedMarketWindow(
        market=schedule.market,
        exchange=schedule.exchange,
        session=session_day,
        expected_at=expected,
        deadline_at=expected + grace,
    )


def calendar_session_date(value: Any) -> date:
    """Convert a calendar session label to its date without timezone drift."""

    if hasattr(value, "date"):
        resolved = value.date()
        if isinstance(resolved, date):
            return resolved
    return date.fromisoformat(str(value)[:10])


def expected_market_scans_between(
    market: str,
    start: datetime,
    end: datetime,
    *,
    grace: timedelta = DEFAULT_PROTECTION_GRACE,
) -> tuple[ExpectedMarketWindow, ...]:
    """Enumerate due protection promises for a bounded SLO window."""

    if grace < timedelta(0):
        raise ValueError("grace must be non-negative")
    beginning = _aware(start)
    finish = _aware(end)
    if finish < beginning:
        raise ValueError("end cannot be earlier than start")
    schedule = _schedule_for(market)
    # A promise can be expected just before the lower bound while its deadline
    # falls inside it, so enumerate one prior local day then filter by deadline.
    local_start = beginning.astimezone(schedule.timezone).date() - timedelta(days=1)
    local_end = finish.astimezone(schedule.timezone).date()
    calendar = exchange_calendar(schedule.exchange)
    sessions = calendar.sessions_in_range(
        pd.Timestamp(local_start.isoformat()), pd.Timestamp(local_end.isoformat())
    )
    windows: list[ExpectedMarketWindow] = []
    for label in sessions:
        session_day = calendar_session_date(label)
        expected = datetime.combine(
            session_day, schedule.scan_at, tzinfo=schedule.timezone
        )
        deadline = expected + grace
        if deadline < beginning.astimezone(schedule.timezone):
            continue
        if deadline > finish.astimezone(schedule.timezone):
            continue
        windows.append(
            ExpectedMarketWindow(
                market=schedule.market,
                exchange=schedule.exchange,
                session=session_day,
                expected_at=expected,
                deadline_at=deadline,
            )
        )
    return tuple(windows)


def next_market_run(market: str, after: datetime | None = None) -> datetime:
    """Return the next valid 09:25 run in the exchange's local timezone."""

    schedule = _schedule_for(market)
    reference = _aware(after).astimezone(schedule.timezone)
    first_day = reference.date()

    # Ten years comfortably exceeds any realistic scheduler look-ahead while
    # still turning an out-of-range calendar into an explicit failure.
    for offset in range(3_660):
        candidate_day = first_day + timedelta(days=offset)
        candidate = datetime.combine(
            candidate_day,
            schedule.scan_at,
            tzinfo=schedule.timezone,
        )
        if candidate <= reference:
            continue
        if is_exchange_session(schedule.exchange, candidate_day):
            return candidate
    raise RuntimeError(f"no {schedule.exchange} session found in the next ten years")


def next_news_run(after: datetime | None = None) -> datetime:
    """Return the next fixed Shanghai news scan, strictly after ``after``."""

    reference = _aware(after).astimezone(NEWS_TIMEZONE)
    for offset in range(2):
        candidate_day = reference.date() + timedelta(days=offset)
        for hour in NEWS_HOURS:
            candidate = datetime.combine(
                candidate_day,
                time(hour),
                tzinfo=NEWS_TIMEZONE,
            )
            if candidate > reference:
                return candidate
    raise AssertionError("a daily fixed-hour news run should always exist")


async def _invoke(callback: Callable[..., object], *args: object) -> None:
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


async def run_market_scan(
    market: str,
    callback: MarketCallback,
    now: datetime | None = None,
) -> bool:
    """Run a sync or async market callback only on an exchange session."""

    if not should_run_market(market, now):
        return False
    await _invoke(callback, _schedule_for(market).market)
    return True


async def run_news_scan(callback: NoArgumentCallback) -> None:
    """Run a sync or async news callback."""

    await _invoke(callback)


def _summary_accepts_market(callback: SummaryCallback, market: str) -> bool:
    try:
        inspect.signature(callback).bind(market)
    except (TypeError, ValueError):
        return False
    return True


async def run_daily_summary(
    market: str,
    callback: SummaryCallback,
    now: datetime | None = None,
) -> bool:
    """Run an optional summary callback on a session, with market if accepted."""

    if not should_run_market(market, now):
        return False
    business_market = _schedule_for(market).market
    if _summary_accepts_market(callback, business_market):
        await _invoke(callback, business_market)
    else:
        await _invoke(callback)
    return True


def build_scheduler(
    scan_market: MarketCallback,
    scan_news: NoArgumentCallback,
    daily_summary: SummaryCallback | None = None,
    *,
    scheduler: AsyncIOScheduler | None = None,
    trust_watchdog: NoArgumentCallback | None = None,
    watchdog_interval_minutes: int = DEFAULT_WATCHDOG_INTERVAL_MINUTES,
    event_listener: SchedulerEventCallback | None = None,
) -> AsyncIOScheduler:
    """Configure (but do not start) the Alpha Guard APScheduler instance."""

    if watchdog_interval_minutes < 1:
        raise ValueError("watchdog_interval_minutes must be positive")

    configured = scheduler or AsyncIOScheduler(
        timezone=NEWS_TIMEZONE,
        job_defaults=dict(_JOB_OPTIONS),
    )

    for market, schedule in MARKET_SCHEDULES.items():
        configured.add_job(
            run_market_scan,
            CronTrigger(
                hour=schedule.scan_at.hour,
                minute=schedule.scan_at.minute,
                timezone=schedule.timezone,
            ),
            args=(market, scan_market),
            id=f"market-scan:{market}",
            name=f"{market} pre-open scan",
            replace_existing=True,
            **_JOB_OPTIONS,
        )
        if daily_summary is not None:
            configured.add_job(
                run_daily_summary,
                CronTrigger(
                    hour=schedule.summary_at.hour,
                    minute=schedule.summary_at.minute,
                    timezone=schedule.timezone,
                ),
                args=(market, daily_summary),
                id=f"daily-summary:{market}",
                name=f"{market} daily summary",
                replace_existing=True,
                **_JOB_OPTIONS,
            )

    configured.add_job(
        run_news_scan,
        CronTrigger(
            hour=",".join(str(hour) for hour in NEWS_HOURS),
            minute=0,
            timezone=NEWS_TIMEZONE,
        ),
        args=(scan_news,),
        id="news-scan",
        name="Fixed-hour news scan",
        replace_existing=True,
        **_JOB_OPTIONS,
    )
    if trust_watchdog is not None:
        configured.add_job(
            run_news_scan,
            IntervalTrigger(
                minutes=watchdog_interval_minutes,
                timezone=NEWS_TIMEZONE,
            ),
            args=(trust_watchdog,),
            id="trust-watchdog",
            name="Trusted-silence deadline watchdog",
            replace_existing=True,
            **_JOB_OPTIONS,
        )
    if event_listener is not None:
        configured.add_listener(
            event_listener,
            EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
        )
    return configured


# Readable compatibility aliases for callers that phrase these as scans.
is_market_open_day = is_market_session
next_market_scan = next_market_run


__all__ = [
    "DEFAULT_MISFIRE_GRACE_TIME",
    "DEFAULT_PROTECTION_GRACE",
    "DEFAULT_WATCHDOG_INTERVAL_MINUTES",
    "MARKET_SCHEDULES",
    "NEWS_HOURS",
    "NEWS_TIMEZONE",
    "MarketSchedule",
    "ExpectedMarketWindow",
    "build_scheduler",
    "exchange_calendar",
    "is_exchange_session",
    "is_market_open_day",
    "is_market_session",
    "latest_expected_market_scan",
    "market_freshness_context",
    "next_market_run",
    "next_market_scan",
    "next_news_run",
    "expected_market_scans_between",
    "run_daily_summary",
    "run_market_scan",
    "run_news_scan",
    "should_run_market",
]
