from __future__ import annotations

import json

import pytest

from research import browser_video, connectors
from research.network import ContentAccessError, FetchedContent


def make_item():
    return connectors._item(external_id='abc123DEF45', source_url='https://www.youtube.com/watch?v=abc123DEF45',
        title='Video', platform='youtube', quality_issues=['transcript_unavailable', 'recording_time_unknown'])


def test_native_json3_preserves_real_caption_clock_and_skips_layout():
    segments = connectors.parse_youtube_captions(json.dumps({'events': [
        {'id': 1, 'tStartMs': 0},
        {'tStartMs': 1520, 'dDurationMs': 2250, 'segs': [{'utf8': 'A quoted '}, {'utf8': 'statement.'}]},
        {'tStartMs': 4000, 'segs': [{'utf8': 'Another cue.'}]},
    ]}))
    assert segments == [
        {'start_seconds': 1.52, 'end_seconds': 3.77, 'text': 'A quoted statement.', 'speaker': ''},
        {'start_seconds': 4.0, 'end_seconds': None, 'text': 'Another cue.', 'speaker': ''},
    ]


@pytest.mark.parametrize('payload,code', [
    ({'events': [{'segs': [{'utf8': 'No timestamp'}]}]}, 'caption_timestamp_unknown'),
    ({'events': [{'tStartMs': -1, 'segs': [{'utf8': 'Bad time'}]}]}, 'invalid_caption_timestamp'),
    ({'events': [{'tStartMs': 0, 'dDurationMs': -1, 'segs': [{'utf8': 'Bad end'}]}]}, 'invalid_caption_timestamp'),
    ({'events': [{}] * 5001}, 'transcript_segment_limit'),
])
def test_invalid_native_caption_response_never_becomes_full(payload, code):
    with pytest.raises(ContentAccessError, match=code):
        connectors.parse_youtube_captions(json.dumps(payload))


def test_empty_native_caption_response_is_not_an_invalid_news_feed():
    with pytest.raises(ContentAccessError, match='transcript_unavailable'):
        connectors.parse_youtube_captions('')


class PublicPage:
    created = []
    closed = False
    metadata = {'video_id': 'abc123DEF45', 'title': 'Actual title', 'author': 'Publisher',
                'playability': 'OK', 'duration_seconds': 30}
    snapshot = {'url': 'https://space.bilibili.com/520819684', 'author': '小Lin说', 'videos': [
        {'url': 'https://www.bilibili.com/video/BV1ssac6oEL3', 'title': 'Public video'},
        {'url': 'https://www.bilibili.com/video/BV1ssac6oEL3?tracking=duplicate', 'title': 'Duplicate'},
        {'url': 'https://evil.example/video/BV1ssac6oEL3', 'title': 'External ad'},
    ]}
    responses = [{'video_id': 'abc123DEF45', 'language': 'en', 'machine': False,
                  'body': json.dumps({'events': [{'tStartMs': 2000, 'dDurationMs': 3000,
                            'segs': [{'utf8': 'The complete native caption response.'}]}]})}]

    def __init__(self, url, **kwargs):
        self.created.append((url, kwargs))
        if not kwargs.get('enabled'):
            raise ContentAccessError('browser_disabled')
    def __enter__(self):
        return self
    def __exit__(self, *args):
        type(self).closed = True
    def wait_for(self, *args, **kwargs):
        return True
    def evaluate(self, expression):
        if expression == browser_video._BILIBILI_CARDS:
            return self.snapshot
        if expression == browser_video._YOUTUBE_META:
            return self.metadata
        return None
    def caption_responses(self):
        return self.responses
    def click(self, *args):
        return True


def test_browser_transcript_binds_native_response_to_same_video(monkeypatch):
    monkeypatch.setattr(browser_video, 'BrowserPage', PublicPage)
    item = make_item()
    browser_video.youtube_transcript(item, enabled=True)
    assert item['completeness'] == 'full' and item['content_kind'] == 'transcript'
    assert item['segments'][0]['start_seconds'] == 2.0
    assert item['title'] == 'Actual title' and item['author'] == 'Publisher'
    assert 'machine_transcription_unverified' not in item['quality_issues']
    assert 'browser_public_captions' in item['quality_issues'] and PublicPage.closed


def test_browser_video_navigation_identity_mismatch_closes_without_evidence(monkeypatch):
    monkeypatch.setattr(browser_video, 'BrowserPage', PublicPage)
    monkeypatch.setattr(PublicPage, 'metadata', {'video_id': 'different'})
    item = make_item()
    with pytest.raises(ContentAccessError, match='video_identity_mismatch'):
        browser_video.youtube_transcript(item, enabled=True)
    assert item['completeness'] != 'full' and PublicPage.closed


def test_browser_author_snapshot_never_uses_ads_or_duplicate_cards(monkeypatch):
    monkeypatch.setattr(browser_video, 'BrowserPage', PublicPage)
    items = browser_video.discover_bilibili({'url': 'https://space.bilibili.com/520819684', 'label': 'Test'}, enabled=True)
    assert len(items) == 1 and items[0]['author'] == '小Lin说'
    assert items[0]['published_at'] is None and items[0]['completeness'] != 'full'
    assert items[0]['source_url'] == 'https://www.bilibili.com/video/BV1ssac6oEL3'


def test_dom_cues_do_not_invent_end_times_or_missing_start():
    assert browser_video._dom_segments({'rows': [{'timestamp': '1:02:03', 'text': 'Statement'}]}) == [
        {'start_seconds': 3723, 'end_seconds': None, 'text': 'Statement', 'speaker': ''}]
    with pytest.raises(ContentAccessError, match='caption_timestamp_unknown'):
        browser_video._dom_segments({'rows': [{'timestamp': '', 'text': 'Statement'}]})
    assert browser_video._dom_segments({'pending': True, 'rows': [{'timestamp': '0:00', 'text': 'Preview'}]}) == []


def test_browser_disabled_never_enters_production_transport(monkeypatch):
    monkeypatch.setattr('research.browser_source._connect_browser', lambda *args: pytest.fail('must not connect'))
    with pytest.raises(ContentAccessError, match='browser_disabled'):
        browser_video.youtube_transcript(make_item())


def test_bilibili_metadata_keeps_real_publication_when_captions_missing():
    item = connectors._item(external_id='BV1ssac6oEL3', source_url='https://www.bilibili.com/video/BV1ssac6oEL3',
                            title='Card', platform='bilibili')
    def fetch(url, **kwargs):
        data = ({'bvid': 'BV1ssac6oEL3', 'aid': 1, 'cid': 2, 'title': 'Actual title',
                 'pubdate': 1790415000, 'desc': 'Actual description', 'owner': {'name': 'Publisher'}}
                if 'web-interface/view' in url else {'subtitle': {'subtitles': []}})
        return FetchedContent(url, json.dumps({'code': 0, 'data': data}), 'application/json')
    connectors._bilibili_content(item, fetch, None)
    assert item['published_at'] == '2026-09-26T09:30:00+00:00'
    assert item['title'] == 'Actual title' and item['author'] == 'Publisher'
    assert item['completeness'] != 'full'


def test_opt_in_youtube_uses_browser_after_empty_native_response(monkeypatch):
    monkeypatch.setattr(browser_video, 'BrowserPage', PublicPage)
    feed = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>abc123DEF45</id><title>Video</title>
    <link href="https://www.youtube.com/watch?v=abc123DEF45"/><published>2026-09-30T12:00:00Z</published></entry></feed>'''
    def fetch(url, **kwargs):
        return FetchedContent(url, feed if '/feeds/' in url else '<html>Native caption unavailable</html>')
    result = connectors.discover({'platform': 'youtube', 'url': 'https://www.youtube.com/channel/UCvJJ_dzjViJCoLf5uKUTwoA'},
                                 fetch=fetch, browser_enabled=True)
    assert result['status'] == 'success' and result['items'][0]['completeness'] == 'full'


@pytest.mark.parametrize('platform,url', [('douyin', 'https://www.douyin.com/user/author'),
                                         ('xiaohongshu', 'https://www.xiaohongshu.com/user/profile/123456789012345678901234')])
def test_opt_in_social_routes_to_browser_and_preserves_permission_state(monkeypatch, platform, url):
    seen = []
    def discover(source, **kwargs):
        seen.append(kwargs)
        raise ContentAccessError('browser_login_required')
    monkeypatch.setattr('research.browser_social.discover_social', discover)
    subscription = {'platform': platform, 'url': url}
    assert connectors.discover(subscription)['status'] == 'needs_configuration'
    assert not seen
    result = connectors.discover(subscription, browser_enabled=True)
    assert seen[0]['enabled'] is True
    assert result['status'] == 'permission_required' and result['detail'] == 'browser_login_required'


@pytest.mark.parametrize('platform,url', [('youtube', 'https://evil.example/watch?v=abc123DEF45'),
                                        ('bilibili', 'https://evil.example/video/BV1ssac6oEL3')])
def test_authorized_feed_cannot_rebind_native_caption_to_external_source_url(platform, url):
    item = connectors._item(external_id='test', source_url=url, title='Untrusted feed item', platform=platform)
    callback = connectors._youtube_content if platform == 'youtube' else connectors._bilibili_content
    with pytest.raises(ContentAccessError, match='invalid_video_url'):
        callback(item, lambda *a, **kw: pytest.fail('wrong-domain source must not fetch'), None)


def test_native_player_response_identity_is_checked_before_caption_request():
    def fetch(url, **kwargs):
        return FetchedContent(url, 'ytInitialPlayerResponse = ' + json.dumps({'videoDetails': {'videoId': 'WRONG123456'}}))
    with pytest.raises(ContentAccessError, match='video_identity_mismatch'):
        connectors._youtube_content(make_item(), fetch, None)


@pytest.mark.parametrize('payload,code', [
    ({'body': [{'content': 'No clock'}]}, 'caption_timestamp_unknown'),
    ({'body': [{'from': True, 'to': 2, 'content': 'Boolean clock'}]}, 'invalid_caption_timestamp'),
    ({'body': [{'from': 2, 'to': 1, 'content': 'Reversed clock'}]}, 'invalid_caption_timestamp'),
    ({'body': [{}] * 5001}, 'transcript_segment_limit'),
])
def test_bilibili_caption_clock_and_size_rejection(payload, code):
    with pytest.raises(ContentAccessError, match=code):
        connectors.parse_bilibili_captions(json.dumps(payload))


@pytest.mark.parametrize('part_count,expected', [(1, 'full'), (2, 'partial')])
def test_browser_bilibili_subtitles_bind_part_and_preserve_multipart_limit(monkeypatch, part_count, expected):
    class BiliPage(PublicPage):
        def evaluate(self, expression):
            if expression == browser_video._BILIBILI_META:
                return {'video_id': 'BV1ssac6oEL3', 'aid': 10, 'cid': 20, 'duration_seconds': 8,
                        'title': 'Real video title', 'author': 'Publisher', 'part_count': part_count,
                        'published_at': 1790415000}
            return super().evaluate(expression)
        def bilibili_caption_responses(self, *, bvid, aid, cid):
            assert (bvid, aid, cid) == ('BV1ssac6oEL3', 10, 20)
            return [{'body': json.dumps({'body': [{'from': 2, 'to': 5, 'content': 'Real timestamped statement.'}]}),
                     'video_id': bvid, 'segment_id': cid, 'machine': True, 'language': 'ai-zh'}]
    monkeypatch.setattr(browser_video, 'BrowserPage', BiliPage)
    item = connectors._item(external_id='BV1ssac6oEL3', source_url='https://www.bilibili.com/video/BV1ssac6oEL3',
                            title='Card', platform='bilibili', quality_issues=['transcript_unavailable'])
    browser_video.bilibili_transcript(item, enabled=True)
    assert item['completeness'] == expected and item['content_kind'] == 'transcript'
    assert item['segments'][0]['start_seconds'] == 2 and item['segments'][0]['end_seconds'] == 5
    assert item['published_at'] == '2026-09-26T09:30:00+00:00'
    assert 'machine_transcription_unverified' in item['quality_issues']
    if part_count > 1:
        assert 'multipart_video_first_part_only' in item['quality_issues']



def test_bilibili_explicit_subtitle_login_flag_is_not_treated_as_no_captions():
    item = connectors._item(external_id='BV1ssac6oEL3', source_url='https://www.bilibili.com/video/BV1ssac6oEL3',
                            title='Card', platform='bilibili')
    def fetch(url, **kwargs):
        data = {'bvid': 'BV1ssac6oEL3', 'aid': 10, 'cid': 20} if 'web-interface/view' in url else {
            'need_login_subtitle': True, 'subtitle': {'subtitles': []}}
        return FetchedContent(url, json.dumps({'code': 0, 'data': data}))
    with pytest.raises(ContentAccessError, match='subtitle_login_required'):
        connectors._bilibili_content(item, fetch, None)
    assert item['completeness'] != 'full'


def test_bilibili_opens_language_menu_and_selects_native_chinese_track(monkeypatch):
    calls = []

    class BiliMenuPage(PublicPage):
        selected = False
        tracks_ready = False

        def wait_for(self, expression, **kwargs):
            if "language-item') !== null" in expression:
                self.tracks_ready = True
            return True

        def evaluate(self, expression):
            if expression == browser_video._BILIBILI_META:
                return {'video_id': 'BV1ssac6oEL3', 'aid': 10, 'cid': 20,
                        'duration_seconds': 30, 'part_count': 1}
            if expression == "Boolean(document.querySelector('video')?.paused)":
                return True
            return None

        def hover(self, selector):
            assert calls[0] == 'read_caption_binding'
            assert self.tracks_ready, 'The label alone is not a loaded subtitle menu.'
            calls.append(('hover', selector))
            return True

        def click(self, selector):
            calls.append(('click', selector))
            if selector.endswith('[data-lan="ai-zh"]'):
                self.selected = True
                return True
            return selector == '.bpx-player-ctrl-play'

        def bilibili_caption_responses(self, *, bvid, aid, cid):
            calls.append('read_caption_binding')
            if not self.selected:
                return []
            return [{'body': json.dumps({'body': [
                {'from': 2, 'to': 5, 'content': '完整的带时间戳的字幕内容。'}]}),
                     'video_id': bvid, 'segment_id': cid, 'machine': True, 'language': 'ai-zh'}]

    monkeypatch.setattr(browser_video, 'BrowserPage', BiliMenuPage)
    monkeypatch.setattr(browser_video.time, 'sleep', lambda _: None)
    item = connectors._item(external_id='BV1ssac6oEL3',
                            source_url='https://www.bilibili.com/video/BV1ssac6oEL3',
                            title='Public video', platform='bilibili')
    browser_video.bilibili_transcript(item, enabled=True)
    assert item['completeness'] == 'full'
    assert item['segments'][0]['start_seconds'] == 2
    assert 'machine_transcription_unverified' in item['quality_issues']
    assert ('hover', '.bpx-player-ctrl-subtitle') in calls
    assert ('click', '.bpx-player-ctrl-play') in calls
    assert ('click', '.bpx-player-ctrl-subtitle') not in calls


@pytest.mark.parametrize('state,error', [
    ({'video_id': 'different', 'page_video_id': 'abc123DEF45', 'ad': False}, 'video_identity_mismatch'),
    ({'video_id': 'abc123DEF45', 'page_video_id': 'different', 'ad': False}, 'video_identity_mismatch'),
    ({'video_id': 'abc123DEF45', 'page_video_id': 'abc123DEF45', 'ad': True}, 'browser_media_advertisement'),
    ({'video_id': 'abc123DEF45', 'page_video_id': 'abc123DEF45'}, 'browser_media_advertisement'),
])
def test_youtube_media_fallback_rejects_wrong_video_or_ads(state, error):
    class Page:
        def evaluate(self, expression):
            assert expression == browser_video._YOUTUBE_PLAYBACK_IDENTITY
            return state

        def click(self, _):
            return False

        def extend_for_media_seconds(self, _):
            pytest.fail('The sole media extension must remain available for actual capture.')

        def wait_for(self, *_args, **_kwargs):
            return None

    with pytest.raises(ContentAccessError, match=error):
        browser_video._youtube_media_fallback(Page(), make_item(), 'abc123DEF45',
            lambda *_: pytest.fail('No recording may start before source identity is verified.'))


def test_youtube_native_exhaustion_calls_media_handler_with_real_identity(monkeypatch):
    class MediaPage(PublicPage):
        responses = []

        def evaluate(self, expression):
            if expression == browser_video._YOUTUBE_PLAYBACK_IDENTITY:
                return {'video_id': 'abc123DEF45', 'page_video_id': 'abc123DEF45', 'ad': False}
            return super().evaluate(expression)

    monkeypatch.setattr(browser_video, 'BrowserPage', MediaPage)
    clock = iter([0., 11.])
    monkeypatch.setattr(browser_video.time, 'monotonic', lambda: next(clock))
    item = make_item()
    item['external_id'] = 'yt:video:abc123DEF45'
    item['published_at'] = '2026-09-26T09:30:00+00:00'

    def handler(page, current):
        assert current['external_id'] == 'abc123DEF45'
        assert current['title'] == 'Actual title' and current['author'] == 'Publisher'
        assert current['published_at'] == '2026-09-26T09:30:00+00:00'
        connectors._attach_transcript(current, [{'start_seconds': 0, 'end_seconds': 20,
            'text': 'A complete locally transcribed public video sample.', 'speaker': ''}])

    browser_video.youtube_transcript(item, enabled=True, media_handler=handler)
    assert item['completeness'] == 'full'
    assert 'machine_transcription_unverified' in item['quality_issues']


def test_youtube_media_noop_does_not_promote_metadata():
    class Page:
        def evaluate(self, _):
            return {'video_id': 'abc123DEF45', 'page_video_id': 'abc123DEF45', 'ad': False}

    item = make_item()
    assert browser_video._youtube_media_fallback(Page(), item, 'abc123DEF45', lambda *_: None) is False
    assert item['completeness'] == 'metadata'


def test_bad_native_caption_track_does_not_hide_a_valid_track(monkeypatch):
    class MixedPage(PublicPage):
        responses = [
            {'video_id': 'abc123DEF45', 'machine': False,
             'body': json.dumps({'events': [{'segs': [{'utf8': 'Missing clock'}]}]})},
            *PublicPage.responses,
        ]

    monkeypatch.setattr(browser_video, 'BrowserPage', MixedPage)
    item = make_item()
    browser_video.youtube_transcript(item, enabled=True)
    assert item['completeness'] == 'full'
    assert item['segments'][0]['start_seconds'] == 2


def test_bad_native_caption_track_can_fall_back_to_media(monkeypatch):
    class BadTrackPage(PublicPage):
        responses = [{'video_id': 'abc123DEF45', 'machine': False,
                      'body': json.dumps({'events': [{'segs': [{'utf8': 'Missing clock'}]}]})}]

        def evaluate(self, expression):
            if expression == browser_video._YOUTUBE_PLAYBACK_IDENTITY:
                return {'video_id': 'abc123DEF45', 'page_video_id': 'abc123DEF45', 'ad': False}
            return super().evaluate(expression)

    monkeypatch.setattr(browser_video, 'BrowserPage', BadTrackPage)
    clock = iter([0., 11.])
    monkeypatch.setattr(browser_video.time, 'monotonic', lambda: next(clock))

    def handler(page, item):
        connectors._attach_transcript(item, [{'start_seconds': 0, 'end_seconds': 10,
            'text': 'A real local transcription can replace unavailable platform captions.', 'speaker': ''}])

    item = make_item()
    browser_video.youtube_transcript(item, enabled=True, media_handler=handler)
    assert item['completeness'] == 'full'


def youtube_author_snapshot():
    return {'url': 'https://www.youtube.com/@jawed/videos',
            'channel_id': 'UC4QobU6STFB0P71PMvOGN5A', 'author': 'jawed',
            'vanity_url': 'https://www.youtube.com/@jawed',
            'selected_tab_url': '/@jawed/videos', 'selected_tab_title': 'Videos',
            'sort_label': 'Latest', 'empty': False,
            'videos': [
                {'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw', 'title': 'Me at the zoo'},
                {'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw&feature=featured', 'title': 'Duplicate'},
                {'url': 'https://evil.example/watch?v=abc123DEF45', 'title': 'External recommendation'},
                {'url': 'https://www.youtube.com/shorts/abc123DEF45', 'title': 'Author short'},
            ]}


@pytest.mark.parametrize('author_url', [
    'https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A',
    'https://www.youtube.com/@jawed',
])
def test_youtube_author_discovery_is_scoped_and_deduplicated(monkeypatch, author_url):
    class AuthorPage(PublicPage):
        def evaluate(self, expression):
            assert expression == browser_video._YOUTUBE_AUTHOR_VIDEOS
            return youtube_author_snapshot()

        def __enter__(self):
            assert self.created[-1][0].endswith('/videos')
            return self

    monkeypatch.setattr(browser_video, 'BrowserPage', AuthorPage)
    rows = browser_video.discover_youtube({'url': author_url, 'label': 'User label'}, enabled=True)
    assert [row['external_id'] for row in rows] == ['jNQXAC9IVRw', 'abc123DEF45']
    assert all(row['author'] == 'jawed' and row['published_at'] is None for row in rows)
    assert all(row['completeness'] == 'metadata' and not row['content_text'] for row in rows)


@pytest.mark.parametrize('changes,error', [
    ({'channel_id': 'UCvJJ_dzjViJCoLf5uKUTwoA'}, 'author_identity_mismatch'),
    ({'selected_tab_url': '/@jawed/featured', 'selected_tab_title': 'Home'}, 'browser_author_updates_unavailable'),
    ({'sort_label': 'Popular'}, 'browser_latest_videos_unavailable'),
    ({'empty': False, 'videos': []}, 'browser_author_updates_unavailable'),
])
def test_youtube_home_wrong_author_and_unknown_empty_are_not_latest_uploads(monkeypatch, changes, error):
    class AuthorPage(PublicPage):
        def evaluate(self, _):
            return {**youtube_author_snapshot(), **changes}

    monkeypatch.setattr(browser_video, 'BrowserPage', AuthorPage)
    with pytest.raises(ContentAccessError, match=error):
        browser_video.discover_youtube({'url': 'https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A',
                                      'label': 'jawed'}, enabled=True)


def test_youtube_handle_redirect_cannot_replace_subscribed_author(monkeypatch):
    class AuthorPage(PublicPage):
        def evaluate(self, _):
            return {**youtube_author_snapshot(), 'url': 'https://www.youtube.com/@other/videos'}

    monkeypatch.setattr(browser_video, 'BrowserPage', AuthorPage)
    with pytest.raises(ContentAccessError, match='author_identity_mismatch'):
        browser_video.discover_youtube({'url': 'https://www.youtube.com/@jawed', 'label': 'jawed'}, enabled=True)


def test_youtube_verified_author_without_videos_is_successful_empty(monkeypatch):
    class AuthorPage(PublicPage):
        def evaluate(self, _):
            return {**youtube_author_snapshot(), 'empty': True, 'videos': []}

    monkeypatch.setattr(browser_video, 'BrowserPage', AuthorPage)
    assert browser_video.discover_youtube({'url': 'https://www.youtube.com/@jawed',
                                         'label': 'jawed'}, enabled=True) == []


def test_youtube_author_browser_fallback_remains_opt_in():
    with pytest.raises(ContentAccessError, match='browser_disabled'):
        browser_video.discover_youtube({'url': 'https://www.youtube.com/@jawed', 'label': 'jawed'})


def test_youtube_waits_for_normal_ad_end_before_recording():
    events = []

    class Page:
        def evaluate(self, _):
            return {'video_id': 'abc123DEF45', 'page_video_id': 'abc123DEF45', 'ad': True}

        def click(self, selector):
            events.append('skip_if_available')
            return False

        def extend_for_media_seconds(self, value):
            pytest.fail('Waiting for advertisements must not consume the recording extension.')

        def wait_for(self, _, *, timeout):
            assert timeout == 30
            events.append('ad_finished')
            return {'video_id': 'abc123DEF45', 'page_video_id': 'abc123DEF45', 'ad': False}

    def handler(page, item):
        assert events[-1] == 'ad_finished'
        events.append('recording_started')
        connectors._attach_transcript(item, [{'start_seconds': 0, 'end_seconds': 10,
            'text': 'The actual video begins after the advertisement ends.', 'speaker': ''}])

    assert browser_video._youtube_media_fallback(Page(), make_item(), 'abc123DEF45', handler)
    assert events[-1] == 'recording_started'


@pytest.mark.parametrize('actual_cid,part_count,expected', [(20, 1, 'full'), (20, 2, None), (21, 1, None)])
def test_bilibili_media_fallback_rechecks_current_part(monkeypatch, tmp_path, actual_cid, part_count, expected):
    from research.store import ResearchStore

    class BiliPage(PublicPage):
        def evaluate(self, expression):
            if expression == browser_video._BILIBILI_META:
                return {'video_id': 'BV1ssac6oEL3', 'aid': 10, 'cid': 20,
                        'duration_seconds': 30, 'part_count': part_count}
            if 'duration:document.querySelector' in expression:
                return {'video_id': 'BV1ssac6oEL3', 'aid': 10, 'cid': actual_cid,
                        'duration': 30., 'ad': False}
            return None

        def hover(self, _):
            return True

        def bilibili_caption_responses(self, **_):
            return []

    clock = iter([0., 9.])
    monkeypatch.setattr(browser_video.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(browser_video, 'BrowserPage', BiliPage)
    item = connectors._item(external_id='BV1ssac6oEL3',
                            source_url='https://www.bilibili.com/video/BV1ssac6oEL3',
                            title='Public video', platform='bilibili')

    def handler(page, current):
        assert expected is not None
        connectors._attach_transcript(current, [{'start_seconds': 0, 'end_seconds': 20,
            'text': 'The correctly identified original part of the video.', 'speaker': ''}])
        with ResearchStore(tmp_path / 'media.db') as store:
            store.upsert_evidence(current)

    if expected is not None:
        browser_video.bilibili_transcript(item, enabled=True, media_handler=handler)
        assert item['completeness'] == expected
    else:
        error = 'browser_transcript_unavailable' if part_count > 1 else 'video_identity_mismatch'
        with pytest.raises(ContentAccessError, match=error):
            browser_video.bilibili_transcript(item, enabled=True, media_handler=handler)
        assert item['completeness'] != 'full'
        with ResearchStore(tmp_path / 'media.db') as store:
            assert store.list_evidence() == []
        if part_count > 1:
            assert 'multipart_video_first_part_only' in item['quality_issues']
