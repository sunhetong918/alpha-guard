from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
import json
import socket
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from research import connectors, ingest, market, network
from research.network import ContentAccessError, FetchedContent


RSS = '''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><guid>one</guid><title>A report</title><link>https://example.com/report</link>
<pubDate>Tue, 29 Sep 2026 10:00:00 +0000</pubDate><description>Brief summary</description>{content}</item>
</channel></rss>'''
ATOM = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<entry><id>yt:video:abc123DEF45</id><title>Interview</title><link href="https://www.youtube.com/watch?v=abc123DEF45"/>
<author><name>Creator</name></author><published>2026-09-29T10:00:00Z</published>
<media:group><media:description>The description is NOT the interview transcript.</media:description></media:group>
</entry></feed>'''


def _rss(*, full=False):
    body = '<content:encoded><![CDATA[<p>The company published audited cash flow details.</p>]]></content:encoded>' if full else ''
    return RSS.format(content=body)


def test_native_video_feed_never_promotes_description_to_transcript():
    item = connectors.parse_feed(ATOM, base_url='https://www.youtube.com/feed', platform='youtube')[0]
    assert item['content_kind'] == 'metadata'
    assert not item['content_text'] and not item['segments']
    assert item['published_at'] == '2026-09-29T10:00:00+00:00'
    assert 'transcript_unavailable' in item['quality_issues']


def test_feed_article_content_and_summary_have_distinct_completeness():
    item = connectors.parse_feed(_rss(full=True), base_url='https://example.com/feed')[0]
    assert item['content_kind'] == 'article' and item['completeness'] == 'full'
    assert item['content_text'] == 'The company published audited cash flow details.'
    summary = connectors.parse_feed(_rss(), base_url='https://example.com/feed')[0]
    assert summary['content_kind'] == 'summary' and summary['completeness'] == 'partial'
    assert not summary['content_text']


@pytest.mark.parametrize('xml', ['<!DOCTYPE x [<!ENTITY y "z">]><rss/>', '<html>login</html>', '<rss>'])
def test_invalid_or_entity_feeds_are_rejected(xml):
    with pytest.raises(ContentAccessError):
        connectors.parse_feed(xml, base_url='https://example.com')


def test_feed_unsafe_links_skipped_and_unknown_timestamp_not_invented():
    unsafe = _rss().replace('https://example.com/report', 'http://127.0.0.1/admin')
    assert connectors.parse_feed(unsafe, base_url='https://example.com') == []
    missing_timezone = _rss().replace('Tue, 29 Sep 2026 10:00:00 +0000', '2026-09-29T10:00:00')
    assert connectors.parse_feed(missing_timezone, base_url='https://example.com')[0]['published_at'] is None


@pytest.mark.parametrize('platform,url', [('douyin', 'https://www.douyin.com/user/a'), ('xiaohongshu', 'https://www.xiaohongshu.com/user/profile/a')])
def test_restricted_platforms_require_feed_and_never_request_without_it(platform, url):
    def forbidden(*args, **kwargs):
        raise AssertionError('network must remain idle')
    result = connectors.discover({'platform': platform, 'url': url}, fetch=forbidden)
    assert result['status'] == 'needs_configuration' and result['items'] == []
    result = connectors.discover({'platform': platform, 'url': url, 'feed_url': 'https://example.com/feed'},
                                 fetch=lambda *a, **k: FetchedContent(a[0], _rss(full=True)))
    assert result['status'] == 'success' and result['items'][0]['platform'] == platform
    assert result['items'][0]['completeness'] == 'full'


def test_disabled_subscription_does_not_fetch():
    result = connectors.discover({'platform': 'rss', 'url': 'https://example.com/feed', 'enabled': False},
                                 fetch=lambda *a, **k: pytest.fail('unexpected fetch'))
    assert result['status'] == 'disabled'


def test_cancelled_discovery_stops_before_content_requests():
    state = {'cancelled': False}
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        state['cancelled'] = True
        return FetchedContent(url, _rss())
    result = connectors.discover({'platform': 'rss', 'url': 'https://example.com/feed'}, fetch=fetch,
                                 cancelled=lambda: state['cancelled'])
    assert len(calls) == 1
    assert result['detail'] == 'cancelled'


@pytest.mark.parametrize('enabled', [False, True])
def test_youtube_failed_public_feed_uses_author_browser_only_when_enabled(monkeypatch, enabled):
    calls = []
    def denied(*args, **kwargs):
        raise ContentAccessError('http_error')
    def author(source, **kwargs):
        calls.append(source['url'])
        return connectors.parse_feed(ATOM, base_url=source['url'], platform='youtube')
    def transcript(value, **kwargs):
        connectors._attach_transcript(value, [{'start_seconds': 0, 'end_seconds': 5,
            'text': 'Actual native source content', 'speaker': ''}])
    monkeypatch.setattr('research.browser_video.discover_youtube', author, raising=False)
    monkeypatch.setattr('research.browser_video.youtube_transcript', transcript)
    result = connectors.discover({'platform': 'youtube',
        'url': 'https://www.youtube.com/channel/UC' + 'a' * 22}, fetch=denied, browser_enabled=enabled)
    if enabled:
        assert len(calls) == 1
        assert result['status'] == 'success'
        assert result['items'][0]['completeness'] == 'full'
        assert result['items'][0]['external_id'] == 'abc123DEF45'
    else:
        assert calls == [] and result['detail'] == 'http_error'


def test_youtube_channel_feed_and_public_captions():
    calls = []
    player = {'captions': {'playerCaptionsTracklistRenderer': {'captionTracks': [
        {'baseUrl': 'https://www.youtube.com/api/timedtext?v=abc123DEF45', 'kind': 'asr'}]}}}
    def fetch(url, **kwargs):
        calls.append(url)
        if '/feeds/' in url:
            return FetchedContent(url, ATOM)
        if '/watch' in url:
            return FetchedContent(url, 'var ytInitialPlayerResponse = ' + json.dumps(player) + ';')
        return FetchedContent(url, '<transcript><text start="1.2" dur="2">Revenue grew &amp;amp; margin improved.</text></transcript>')
    result = connectors.discover({'platform': 'youtube', 'url': 'https://www.youtube.com/channel/UC' + 'a' * 22}, fetch=fetch)
    assert len(calls) == 3
    item = result['items'][0]
    assert result['status'] == 'success'
    assert item['content_kind'] == 'transcript' and item['completeness'] == 'full'
    assert item['segments'][0]['start_seconds'] == 1.2
    assert item['segments'][0]['end_seconds'] == 3.2
    assert 'Revenue grew & margin improved.' == item['content_text']
    assert 'machine_transcription_unverified' in item['quality_issues']


def test_youtube_missing_captions_preserves_metadata():
    result = connectors.discover({'platform': 'youtube', 'url': 'https://www.youtube.com/channel/UC' + 'a' * 22},
                                 fetch=lambda url, **kw: FetchedContent(url, ATOM if '/feeds/' in url else '<html>login</html>'))
    assert result['status'] == 'partial'
    assert result['detail'] == 'waiting_for_content'
    assert result['items'][0]['completeness'] == 'metadata'


def test_youtube_handle_resolution_failure_is_configuration_state():
    result = connectors.discover({'platform': 'youtube', 'url': 'https://www.youtube.com/@author'},
                                 fetch=lambda url, **kw: '<html>blocked</html>')
    assert result['status'] == 'needs_configuration'


def test_bilibili_denial_is_not_successful_empty_feed():
    result = connectors.discover({'platform': 'bilibili', 'url': 'https://space.bilibili.com/123'},
                                 fetch=lambda url, **kw: '{"code":-352}')
    assert result['status'] == 'permission_required'


def test_bilibili_multipart_caption_is_partial_and_preserves_timestamps():
    def fetch(url, **kwargs):
        if 'arc/search' in url:
            payload = {'code': 0, 'data': {'list': {'vlist': [{'bvid': 'BV1234567890', 'title': 'Talk'}]}}}
        elif '/view?' in url:
            payload = {'code': 0, 'data': {'aid': 1, 'cid': 2, 'pages': [{}, {}]}}
        elif '/player/' in url:
            payload = {'code': 0, 'data': {'subtitle': {'subtitles': [{'subtitle_url': '//example.com/subtitle.json'}]}}}
        else:
            payload = {'body': [{'from': 2, 'to': 4, 'content': 'An actual quote.'}]}
        return FetchedContent(url, json.dumps(payload))
    result = connectors.discover({'platform': 'bilibili', 'url': 'https://space.bilibili.com/123'}, fetch=fetch)
    assert result['status'] == 'partial'
    item = result['items'][0]
    assert item['content_kind'] == 'transcript'
    assert item['completeness'] == 'partial'
    assert 'multipart_video_first_part_only' in item['quality_issues']


def test_article_requires_explicit_body_and_preserves_title():
    result = connectors.fetch_article('https://example.com', title='Original', fetch=lambda *a, **k: '<html><title>Paywall</title><p>Please subscribe</p></html>')
    assert result['status'] == 'partial' and result['item']['completeness'] == 'metadata'
    full = connectors.fetch_article('https://example.com', title='Original', fetch=lambda *a, **k:
                                    '<script type="application/ld+json">' + json.dumps({'@graph': [{'articleBody': 'Revenue and free cash flow are disclosed in this report. Operating margins and investment risks were also discussed.'}]}) + '</script>')
    assert full['status'] == 'success' and full['item']['title'] == 'Original'
    assert full['item']['completeness'] == 'full'


def test_article_error_does_not_expose_exception_secrets():
    def failing(*a, **k):
        raise RuntimeError('secret-token=abc')
    result = connectors.fetch_article('https://example.com', fetch=failing)
    assert result == {'status': 'failed', 'detail': 'content_fetch_failed'}


@pytest.mark.parametrize('url', ['http://127.0.0.1/', 'http://[::1]/', 'http://192.168.1.1/',
                                'http://169.254.169.254/', 'http://localhost/', 'https://example.com:444/',
                                'https://user:password@example.com/', 'file:///etc/passwd',
                                'https://example.com\\@127.0.0.1/', 'https://example.com/\nsecret'])
def test_unsafe_urls_rejected_before_dns(url):
    with pytest.raises(ContentAccessError):
        network.validate_public_url(url, resolve=False)


def test_dns_all_addresses_must_be_public(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443)),
    ])
    with pytest.raises(ContentAccessError, match='non_public_address'):
        network.validate_public_url('https://example.com')


class _Response:
    def __init__(self, status=200, body=b'hello', **headers):
        self.status, self.body = status, body
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name.replace('_', '-')] = value
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def read1(self, limit):
        chunk, self.body = self.body[:limit], self.body[limit:]
        return chunk


def _fake_transport(monkeypatch, responses):
    calls = []
    connections = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    monkeypatch.setattr(socket, 'create_connection', lambda address, *a: calls.append(address))
    class Connection:
        def __init__(self, host, **kwargs):
            self.host, self.sock = host, None
            connections.append(self)
        def request(self, method, path, **kwargs):
            self._create_connection((self.host, 443))
        def getresponse(self):
            return responses.pop(0)
        def close(self):
            pass
    monkeypatch.setattr(network.http.client, 'HTTPSConnection', Connection)
    return calls, connections


def test_safe_fetch_pins_ip_preserves_tls_hostname_and_reads_bounded_body(monkeypatch):
    calls, connections = _fake_transport(monkeypatch, [_Response(body='信息'.encode(), Content_Type='text/plain; charset=utf-8')])
    result = network.safe_fetch('https://example.com/a')
    assert result.text == '信息'
    assert calls == [('93.184.216.34', 443)]
    assert connections[0].host == 'example.com'


def test_safe_fetch_revalidates_redirect_and_rejects_local(monkeypatch):
    calls, _ = _fake_transport(monkeypatch, [_Response(302, Location='http://127.0.0.1/admin')])
    with pytest.raises(ContentAccessError, match='non_public_address'):
        network.safe_fetch('https://example.com')
    assert len(calls) == 1


def test_safe_fetch_rejects_oversize_and_limits_redirects(monkeypatch):
    _fake_transport(monkeypatch, [_Response(body=b'123456')])
    with pytest.raises(ContentAccessError, match='content_too_large'):
        network.safe_fetch('https://example.com', max_bytes=5)
    _fake_transport(monkeypatch, [_Response(302, Location='https://example.com/loop') for _ in range(4)])
    with pytest.raises(ContentAccessError, match='too_many_redirects'):
        network.safe_fetch('https://example.com')


def test_subtitle_import_keeps_cues_speaker_and_rejects_bad_timing():
    text = 'WEBVTT\n\n00:01.200 --> 00:03.500\n<v Analyst>Sales are growing.</v>\n\n00:04.000 --> 00:06.000\nBut risks remain.\n'
    result = ingest.ingest_text(text, 'vtt', source_url='https://example.com/video')
    assert result['content_kind'] == 'transcript'
    assert result['segments'][0] == {'start_seconds': 1.2, 'end_seconds': 3.5, 'text': 'Sales are growing.', 'speaker': 'Analyst'}
    assert result['content_text'] == 'Sales are growing.\nBut risks remain.'
    with pytest.raises(ValueError, match='invalid_subtitle_timestamp'):
        ingest.parse_subtitles('00:00:05,000 --> 00:00:02,000\nBad')


def test_plain_transcript_does_not_invent_timestamps_and_has_stable_identity():
    first = ingest.ingest_text('Evidence text', 'transcript')
    second = ingest.ingest_text('Evidence text', 'transcript')
    assert first['segments'] == []
    assert 'transcript_timestamps_unknown' in first['quality_issues']
    assert first['external_id'] == second['external_id']


def test_local_transcriber_is_real_argv_process_and_returns_machine_segments(tmp_path):
    media = tmp_path / 'input.wav'
    media.write_bytes(b'placeholder')
    script = ('import json,pathlib,sys; '
              'pathlib.Path(sys.argv[2],"result.json").write_text(json.dumps({"segments":'
              '[{"start":1,"end":2,"text":"A quoted statement"}]}))')
    result = ingest.transcribe_media(media, command=[sys.executable, '-c', script, '{input}', '{output_dir}'])
    assert result['status'] == 'success'
    assert result['item']['segments'][0]['start_seconds'] == 1
    assert 'machine_transcription_unverified' in result['item']['quality_issues']


def test_local_transcriber_missing_config_and_cancellation(tmp_path, monkeypatch):
    monkeypatch.delenv('ALPHA_GUARD_TRANSCRIPTION_COMMAND', raising=False)
    assert ingest.transcribe_media('missing.wav')['status'] == 'needs_configuration'
    media = tmp_path / 'input.wav'
    media.write_bytes(b'placeholder')
    calls = {'n': 0}
    def cancelled():
        calls['n'] += 1
        return calls['n'] > 1
    result = ingest.transcribe_media(media, command=[sys.executable, '-c', 'import time; time.sleep(20)', '{input}', '{output_dir}'], cancelled=cancelled)
    assert result == {'status': 'failed', 'detail': 'cancelled'}


def test_market_does_not_substitute_fetch_time_for_quote_time():
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    result = market.fetch_market_context({'ticker': 'AAPL', 'market': 'US'}, now=now,
                                        stock_fetcher=lambda *a, **k: {'price': 200, 'volume': 1000, 'retrieved_at': now.isoformat()})
    assert result['source_as_of'] is None and not result['short_term_allowed']
    assert 'quote_source_time_unknown' in result['quality_issues']


@pytest.mark.parametrize('as_of,allowed', [('2026-09-30T11:58:00+00:00', True), ('2026-09-29T11:58:00+00:00', False), ('2026-09-30T13:00:00+00:00', False)])
def test_market_short_term_gates_true_source_time_and_preserves_fundamentals(as_of, allowed):
    result = market.fetch_market_context({'ticker': 'AAPL', 'market': 'US'}, now=datetime(2026, 9, 30, 12, tzinfo=UTC),
                                        stock_fetcher=lambda *a, **k: {'price': 200, 'volume': 1000, 'as_of': as_of, 'volume_source_as_of': as_of,
                                                                    'free_cashflow': 123, 'revenue_growth': .12, 'debt_to_equity': 5})
    assert result['short_term_allowed'] is allowed
    assert result['free_cashflow'] == 123 and result['revenue_growth'] == .12


def test_cn_completed_daily_bar_filters_today_and_blocks_short_term():
    captured = {}
    def daily(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame([{'日期': '2026-09-29', '收盘': 20, '成交量': 1000}, {'日期': '2026-09-30', '收盘': 21, '成交量': 1500}])
    result = market.fetch_market_context({'ticker': '600519', 'market': 'CN'}, now=datetime(2026, 9, 30, 2, tzinfo=UTC),
                                        cn_provider=SimpleNamespace(stock_zh_a_hist=daily))
    assert result['price'] == 20 and result['source_as_of'] == '2026-09-29T07:00:00+00:00'
    assert result['granularity'] == 'daily' and result['short_term_allowed'] is False
    assert result['exchange'] == 'SSE' and result['currency'] == 'CNY'
    assert captured['timeout'] == 10 and captured['end_date'] == '20260929'


def test_cancelled_market_context_never_invokes_provider():
    result = market.fetch_market_context({'ticker': 'AAPL', 'market': 'US'}, cancelled=lambda: True,
                                        stock_fetcher=lambda *a, **k: pytest.fail('must not fetch'))
    assert result['quality_issues'] == ['cancelled']


def test_eastmoney_news_uses_bounded_direct_request_and_preserves_summary():
    from config import Settings
    from news.sources import _fetch_akshare_news_result
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return FetchedContent(url, json.dumps({'data': {'fastNewsList': [
            {'title': '标题', 'summary': '实际摘要', 'showTime': '2026-09-30 10:00:00', 'code': '20260930123'}]}}))
    result = _fetch_akshare_news_result(settings=Settings(_env_file=None, http_timeout_seconds=5), fetch=fetch)
    assert result.status == 'success' and result.articles[0]['summary'] == '实际摘要'
    assert result.articles[0]['datetime'] == '2026-09-30T02:00:00+00:00'
    assert calls[0][1] == {'timeout': 5, 'max_bytes': 2_000_000}


def test_semantic_nested_article_container_produces_full_body():
    body = '<div id="article"><div class="heading">Quarterly report</div><div><p>' + ('The published cash flow statement supports this observation. ' * 3) + '</p></div></div><footer>Not article evidence</footer>'
    text, completeness = connectors.extract_article(body)
    assert completeness == 'full' and 'published cash flow' in text
    assert 'Not article evidence' not in text
    assert connectors.extract_article('<article>Subscribe to read the rest of this exclusive article. ' + ('Please subscribe. ' * 8) + '</article>') == ('', 'metadata')


def test_generated_transcript_items_validate_against_persisted_evidence_schema():
    from research.models import evidence_from_item
    result = ingest.ingest_text('00:00:01,000 --> 00:00:03,000\n' + ('A verifiable statement from the interview. ' * 2), 'srt', source_url='https://example.com/video')
    record = evidence_from_item(result)
    assert record.research_ready
    assert record.segments[0].speaker == ''


def test_subscription_credentials_are_rejected_instead_of_persisted():
    with pytest.raises(ValueError, match='credential_url_not_allowed'):
        connectors.normalize_subscription({'platform': 'rss', 'url': 'https://example.com/feed?token=secret'})


def test_nonpublic_multicast_and_b_share_symbols_are_not_misclassified():
    with pytest.raises(ContentAccessError):
        network.validate_public_url('http://224.0.0.1/', resolve=False)
    result = market.fetch_market_context({'market': 'CN', 'ticker': '900901'})
    assert result['quality_issues'] == ['invalid_cn_ticker']


def test_dns_resolution_timeout_is_bounded(monkeypatch):
    import time
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: (time.sleep(.15) or []))
    before = time.monotonic()
    with pytest.raises(ContentAccessError, match='request_timeout'):
        network.validate_public_url('https://example.com', dns_timeout=.01)
    assert time.monotonic() - before < .12


def test_oversized_subtitle_segment_count_is_not_silently_marked_full():
    text = '\n\n'.join(f'{index}\n00:00:01,000 --> 00:00:02,000\nx' for index in range(5001))
    assert len(text) < ingest.MAX_TEXT_CHARS
    with pytest.raises(ValueError, match='too_many_subtitle_segments'):
        ingest.ingest_text(text, 'srt', source_url='https://example.com/video')


def test_long_paywalled_article_previews_are_partial_in_html_and_jsonld():
    preview = 'This is a partial preview of a company interview. ' * 20 + 'Subscribe to continue reading.'
    for text in ['<article>' + preview + '</article>', '<script type="application/ld+json">' + json.dumps({'articleBody': preview}) + '</script>']:
        body, completeness = connectors.extract_article(text)
        assert body and completeness == 'partial'


def test_youtube_caption_timestamps_are_never_invented():
    item = {'source_url': 'https://youtube.com/watch?v=abc123DEF45', 'quality_issues': ['transcript_unavailable']}
    player = {'captions': {'playerCaptionsTracklistRenderer': {'captionTracks': [{'baseUrl': 'https://example.com/captions'}]}}}
    def fetch(url, **kwargs):
        return FetchedContent(url, 'ytInitialPlayerResponse = ' + json.dumps(player) if '/watch' in url else '<transcript><text>No timestamp.</text></transcript>')
    with pytest.raises(ContentAccessError, match='caption_timestamp_unknown'):
        connectors._youtube_content(item, fetch, None)
    assert 'content_kind' not in item


def test_market_runtime_prevents_new_provider_operations_after_stop(monkeypatch):
    from data import fetcher
    from reliability import ProviderKey
    stopped = {'value': False}
    calls = []
    def fake_get_stock(ticker, market, *, provider_runtime, **kwargs):
        def first():
            calls.append('info')
            stopped['value'] = True
            return {'price': 100}
        provider_runtime.execute(ProviderKey(provider='test', operation='info', market='US'), first, idempotent=True)
        provider_runtime.execute(ProviderKey(provider='test', operation='history', market='US'), lambda: calls.append('history'), idempotent=True)
        return {'price': 100}
    monkeypatch.setattr(fetcher, 'get_stock', fake_get_stock)
    result = market.fetch_market_context({'ticker': 'AAPL', 'market': 'US'}, cancelled=lambda: stopped['value'])
    assert calls == ['info'] and result['quality_issues'] == ['cancelled']


def test_fresh_futu_price_cannot_refresh_older_yahoo_volume(monkeypatch):
    from data import fetcher
    from data.futu_provider import FutuQuote
    old_time = datetime(2026, 9, 29, 20, tzinfo=UTC)
    fresh_time = '2026-09-30T11:59:00+00:00'
    fake_ticker = SimpleNamespace(
        info={'regularMarketPrice': 200, 'regularMarketVolume': 12345,
              'regularMarketTime': old_time.timestamp(), 'currency': 'USD'},
        history=lambda **kwargs: pd.DataFrame({'High': [201], 'Low': [199]}),
    )
    monkeypatch.setattr(fetcher, 'yf', SimpleNamespace(Ticker=lambda symbol: fake_ticker))
    quote = FutuQuote(name='Apple', price=205,
                      source_as_of=fresh_time, observed_at=fresh_time, usable_for_signal=True)
    monkeypatch.setattr(fetcher, '_find_futu_quote', lambda *a, **k: quote)
    raw = fetcher.get_us_stock('AAPL')
    assert raw['price'] == 205 and raw['as_of'] == fresh_time
    assert raw['volume'] == 12345 and raw['volume_source_as_of'] == old_time.isoformat()
    assert raw['volume_provider'] == 'yfinance'
    context = market.fetch_market_context({'ticker': 'AAPL', 'market': 'US'},
                                          now=datetime(2026, 9, 30, 12, tzinfo=UTC),
                                          stock_fetcher=lambda *a, **k: raw)
    assert context['short_term_allowed'] is False
    assert 'volume_not_fresh_for_short_term' in context['quality_issues']


def test_volume_number_without_its_own_timestamp_does_not_enable_short_term():
    result = market.fetch_market_context({'ticker': 'AAPL', 'market': 'US'},
        now=datetime(2026, 9, 30, 12, tzinfo=UTC),
        stock_fetcher=lambda *a, **k: {'price': 200, 'volume': 12345, 'as_of': '2026-09-30T11:59:00+00:00'})
    assert not result['short_term_allowed']
    assert 'volume_source_time_unknown' in result['quality_issues']


def test_windows_transcriber_cleanup_targets_only_own_process_tree(monkeypatch):
    calls = []
    process = SimpleNamespace(pid=123456, poll=lambda: None,
                              wait=lambda **kwargs: calls.append(('wait', kwargs)),
                              kill=lambda: calls.append(('kill',)))
    monkeypatch.setattr(ingest.subprocess, 'run', lambda args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(ingest.os, 'killpg', lambda *args: pytest.fail('Windows must not use killpg'), raising=False)
    ingest._stop_process(process, platform='nt')
    args, kwargs = calls[0]
    assert args[0].endswith(r'\System32\taskkill.exe')
    assert args[1:] == ['/PID', '123456', '/T', '/F']
    assert kwargs['shell'] is False and kwargs['timeout'] == 3
    assert calls[1] == ('wait', {'timeout': 2})


def test_posix_transcriber_cleanup_signals_own_process_group(monkeypatch):
    import signal
    calls = []
    process = SimpleNamespace(pid=123456, poll=lambda: None, wait=lambda **kwargs: None)
    monkeypatch.setattr(ingest.os, 'killpg', lambda pid, sig: calls.append((pid, sig)), raising=False)
    ingest._stop_process(process, platform='posix')
    assert calls == [(123456, signal.SIGTERM)]


def test_cn_daily_fallback_preserves_provider_units_and_never_enables_short_term():
    def primary(**kwargs):
        raise ConnectionError('provider unavailable')
    history = pd.DataFrame({'Close': [20, 21], 'Volume': [1000, 1200]},
                           index=pd.DatetimeIndex(['2026-09-29', '2026-09-30'], tz='Asia/Shanghai'))
    result = market.fetch_market_context({'ticker': '600519', 'market': 'CN'},
        now=datetime(2026, 9, 30, 2, tzinfo=UTC),
        cn_provider=SimpleNamespace(stock_zh_a_hist=primary), cn_history_fetcher=lambda **kw: history)
    assert result['provider'] == 'yfinance_cn_daily'
    assert result['volume_unit'] == 'shares' and result['price'] == 20
    assert result['source_as_of'] == '2026-09-29T07:00:00+00:00'
    assert not result['short_term_allowed']
    assert 'cn_primary_provider_unavailable' in result['quality_issues']


def test_stop_after_cn_primary_prevents_fallback_request():
    state = {'stopped': False}
    def primary(**kwargs):
        state['stopped'] = True
        return pd.DataFrame()
    result = market.fetch_market_context({'ticker': '600519', 'market': 'CN'},
        cn_provider=SimpleNamespace(stock_zh_a_hist=primary), cancelled=lambda: state['stopped'],
        cn_history_fetcher=lambda **kw: pytest.fail('cancelled fallback must not run'))
    assert result['quality_issues'] == ['cancelled']


@pytest.mark.parametrize('selected,actual', [('US', 'HKD'), ('HK', 'USD'), ('US', 'EUR')])
def test_mismatched_currency_cannot_produce_a_candidate_quote(selected, actual):
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    result = market.fetch_market_context({'market': selected, 'ticker': 'AAPL' if selected == 'US' else '00700'},
        now=now, stock_fetcher=lambda *a, **kw: {'price': 200, 'volume': 1000, 'currency': actual,
        'as_of': now.isoformat(), 'retrieved_at': now.isoformat(), 'volume_source_as_of': now.isoformat()})
    assert result['currency'] == actual
    assert result['price'] is None and not result['short_term_allowed']
    assert 'market_currency_mismatch' in result['quality_issues']
