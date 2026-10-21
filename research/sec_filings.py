"""Bounded SEC submissions adapter. Submitted filings are not future events."""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from .models import ResearchInstrument, aware
from .network import ContentAccessError, check_cancelled, safe_fetch

SOURCE = 'sec_submissions'
COVERAGE = 'SEC 美股近一年已提交披露；12 类定期/重大报告及修订，不含未来公告计划。'
FORMS = frozenset({'10-K', '10-K/A', '10-Q', '10-Q/A', '8-K', '8-K/A',
                   '20-F', '20-F/A', '40-F', '40-F/A', '6-K', '6-K/A'})
# Verified against SEC's issuer submissions; the live response must still list SPCX.
# This seed permits this issuer when the global ticker directory is unavailable.
CIK_SEEDS = {'SPCX': 1181412}
DIRECTORY = 'https://www.sec.gov/files/company_tickers.json'


class FilingMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cik: str = Field(pattern=r'^[0-9]{10}$')
    accession: str = Field(pattern=r'^[0-9]{10}-[0-9]{2}-[0-9]{6}$')
    form: str = Field(min_length=1, max_length=20)
    report_date: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    acceptance_original: str = Field(max_length=80)
    acceptance_precision: str = Field(pattern=r'^(second|subsecond|unknown_timezone|missing)$')
    mapping_source: str = Field(max_length=300)
    submissions_url: str = Field(max_length=300)
    amended: bool


def _date(value: Any, *, optional: bool = False) -> date | None:
    if optional and value in ('', None):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('invalid date')
    result = date.fromisoformat(value)
    if not date(1900, 1, 1) <= result <= date(2200, 12, 31):
        raise ValueError('date outside bounds')
    return result


def acceptance(value: Any) -> tuple[str | None, str]:
    if value in ('', None):
        return None, 'missing'
    if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?', value
    ):
        raise ValueError('invalid acceptance time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        return None, 'unknown_timezone'
    return aware(parsed).isoformat(), 'subsecond' if '.' in value else 'second'


def parse_filings(data: Any, *, instrument: ResearchInstrument, cik: int,
                  mapping_source: str, now: datetime, first: date, last: date) -> list[dict[str, Any]]:
    keys = ('accessionNumber', 'filingDate', 'reportDate', 'acceptanceDateTime', 'form', 'primaryDocument')
    if not isinstance(data, dict) or any(not isinstance(data.get(k), list) for k in keys):
        raise ValueError('invalid submission columns')
    count = len(data['accessionNumber'])
    if count > 5000 or any(len(data[k]) != count for k in keys):
        raise ValueError('submission columns mismatch')
    entries = []
    for index in range(count):
        form = data['form'][index]
        if not isinstance(form, str):
            raise ValueError('invalid form')
        day = _date(data['filingDate'][index])
        assert day is not None
        if form not in FORMS or not first <= day <= last:
            continue
        accession_id, document = data['accessionNumber'][index], data['primaryDocument'][index]
        if not isinstance(accession_id, str) or not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession_id):
            raise ValueError('invalid accession')
        if not isinstance(document, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,239}', document) or '..' in document:
            raise ValueError('invalid primary document')
        period = _date(data['reportDate'][index], optional=True)
        raw_acceptance = data['acceptanceDateTime'][index]
        published, precision = acceptance(raw_acceptance)
        if published is not None and datetime.fromisoformat(published) > now + timedelta(seconds=60):
            raise ValueError('acceptance in future')
        submissions = f'https://data.sec.gov/submissions/CIK{cik:010d}.json'
        url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession_id.replace("-", "")}/{quote(document, safe="")}'
        metadata = FilingMetadata(cik=f'{cik:010d}', accession=accession_id, form=form,
                                  report_date=period.isoformat() if period else None,
                                  acceptance_original=raw_acceptance or '', acceptance_precision=precision,
                                  mapping_source=mapping_source, submissions_url=submissions, amended=form.endswith('/A'))
        entries.append({'instrument': instrument.model_dump(mode='json'), 'event_type': 'announcement',
                        'source': SOURCE, 'source_event_id': accession_id, 'source_url': url,
                        'title': f'{form} · 已提交披露' + (f' · 报告期 {period}' if period else ''),
                        'event_date': day.isoformat(), 'event_at': None, 'event_timezone': 'America/New_York',
                        'published_at': published, 'observed_at': now.isoformat(), 'status': 'confirmed',
                        'notes': '日期为 SEC 披露日，不是未来计划；修订稿独立保存。原文读取需符合 SEC Fair Access 的 User-Agent（RESEARCH_SEC_CONTACT）。',
                        'filing': metadata.model_dump(mode='json')})
    return entries


def fetch_filings(instrument: ResearchInstrument, *, now: datetime | None = None,
                  cancelled: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """At most 4 requests / 2 history pages / 20 seconds / 100 saved rows.

    Each response is <= 3 MB; no retries. Truncation fails the complete query
    instead of silently claiming an empty or complete date range.
    """
    if instrument.market != 'US':
        raise ValueError('sec_market_unsupported')
    from zoneinfo import ZoneInfo
    now = aware(now or datetime.now(UTC))
    last = now.astimezone(ZoneInfo('America/New_York')).date()
    first = last - timedelta(days=365)
    deadline, previous, requests = time.monotonic() + 20, 0.0, 0

    def read(url: str) -> Any:
        nonlocal previous, requests
        check_cancelled(cancelled)
        while time.monotonic() - previous < 0.25:
            check_cancelled(cancelled)
            time.sleep(0.025)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or requests >= 4:
            raise ContentAccessError('request_timeout')
        previous, requests = time.monotonic(), requests + 1
        response = safe_fetch(url, timeout=min(8, remaining), max_bytes=3_000_000, cancelled=cancelled)
        check_cancelled(cancelled)
        return json.loads(response.text)

    try:
        cik = CIK_SEEDS.get(instrument.ticker)
        mapping_source = f'https://data.sec.gov/submissions/CIK{cik:010d}.json' if cik else DIRECTORY
        if cik is None:
            directory = read(DIRECTORY)
            if not isinstance(directory, dict) or len(directory) > 20_000:
                raise ValueError('invalid ticker directory')
            matches = [row.get('cik_str') for row in directory.values() if isinstance(row, dict)
                       and row.get('ticker') == instrument.ticker]
            if len(matches) != 1 or type(matches[0]) is not int or not 0 < matches[0] < 10**10:
                raise ContentAccessError('sec_identity_unresolved')
            cik = matches[0]
        data = read(f'https://data.sec.gov/submissions/CIK{cik:010d}.json')
        if not isinstance(data, dict) or type(data.get('cik')) not in (int, str) or int(data['cik']) != cik:
            raise ContentAccessError('event_source_identity_mismatch')
        if not isinstance(data.get('tickers'), list) or instrument.ticker not in data['tickers']:
            raise ContentAccessError('event_source_identity_mismatch')
        filings = data['filings']
        files = filings['files']
        if not isinstance(files, list) or len(files) > 1000:
            raise ValueError('invalid history index')
        relevant = []
        for item in files:
            a, b = _date(item['filingFrom']), _date(item['filingTo'])
            assert a is not None and b is not None
            if a > b:
                raise ValueError('invalid history interval')
            if a <= last and b >= first:
                name = item['name']
                if not isinstance(name, str) or not re.fullmatch(rf'CIK{cik:010d}-submissions-\d{{3}}\.json', name):
                    raise ValueError('invalid history file')
                relevant.append(name)
        if len(set(relevant)) != len(relevant) or len(relevant) > 2:
            raise ContentAccessError('sec_history_limit')
        rows = parse_filings(filings['recent'], instrument=instrument, cik=cik,
                             mapping_source=mapping_source, now=now, first=first, last=last)
        for name in relevant:
            check_cancelled(cancelled)
            rows.extend(parse_filings(read('https://data.sec.gov/submissions/' + name), instrument=instrument,
                                      cik=cik, mapping_source=mapping_source, now=now, first=first, last=last))
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = row['source_event_id']
            if key in unique and unique[key] != row:
                raise ValueError('conflicting duplicate accession')
            unique[key] = row
        if len(unique) > 100:
            raise ContentAccessError('sec_record_limit')
        check_cancelled(cancelled)
        if time.monotonic() > deadline:
            raise ContentAccessError('request_timeout')
        return list(unique.values())
    except ContentAccessError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ContentAccessError('sec_source_invalid') from None
