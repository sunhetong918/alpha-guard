from __future__ import annotations

import json

import pytest

from research import browser_source as browser
from research.network import ContentAccessError
from research.browser_social import parse_author_snapshot, discover_social


@pytest.fixture(autouse=True)
def release_browser_connection(monkeypatch):
    monkeypatch.setattr(browser, "_configured_mode", lambda: "existing")
    browser.disconnect_browser()
    yield
    browser.disconnect_browser()


class FakeSocket:
    def __init__(self, url="https://www.douyin.com/user/public", *, fail_attach=False):
        self.url = url
        self.fail_attach = fail_attach
        self.messages = []
        self.queue = []
        self.closed = False

    def send(self, raw):
        value = json.loads(raw)
        self.messages.append(value)
        method = value["method"]
        result = {}
        if method == "Target.createTarget":
            result = {"targetId": "owned-tab"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "owned-session"}
            if self.fail_attach:
                self.queue.append(json.dumps({"id": value["id"], "error": {"message": "secret must never appear"}}))
                return
        elif method == "Runtime.evaluate":
            expression = value["params"]["expression"]
            if expression == "location.href":
                payload = self.url
            elif expression == "({url:location.href,ready:document.readyState})":
                payload = {"url": self.url, "ready": "complete"}
            elif expression == browser._SNAPSHOT_JS:
                payload = {"url": self.url, "title": "Public author", "text": "visible", "links": []}
            elif "document.elementFromPoint" in expression:
                payload = {"x": 25, "y": 50}
            else:
                payload = True
            result = {"result": {"value": payload}}
        elif method == "Network.getResponseBody":
            result = {"body": '<transcript><text start="1">captions</text></transcript>'}
        self.queue.append(json.dumps({"id": value["id"], "result": result}))

    def recv(self, *, timeout):
        return self.queue.pop(0)

    def close(self):
        self.closed = True


def test_douyin_passive_metadata_only_accepts_owned_exact_video_and_exports_no_urls(monkeypatch):
    identity = "7686727666610883903"
    connection = FakeSocket("https://www.douyin.com/video/" + identity)
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with browser.BrowserPage(connection.url, enabled=True) as page:
        payload = {"status_code": 0, "aweme_detail": {"aweme_id": identity,
            "author": {"sec_uid": "public-author", "nickname": "作者", "private": "not exported"},
            "desc": "公开描述", "create_time": 1789999999, "is_ads": False,
            "video": {"duration": 31000, "play_addr": {"url_list": ["signed secret"]}}}}
        monkeypatch.setattr(page, "_response_body", lambda _: json.dumps(payload))
        def record(request, video=identity, session="owned-session", host="www.douyin.com", finished=True):
            page._record_event({"sessionId": session, "method": "Network.responseReceived", "params": {
                "requestId": request, "response": {"status": 200,
                "url": f"https://{host}/aweme/v1/web/aweme/detail/?aweme_id={video}&signature=never-export"}}})
            if finished:
                page._record_event({"sessionId": session, "method": "Network.loadingFinished", "params": {"requestId": request}})
        record("other-tab", session="user-tab")
        record("other-video", video="7686727666610883904")
        record("other-host", host="evil.test")
        record("pending", finished=False)
        assert page.douyin_video_metadata() is None
        record("complete")
        value = page.douyin_video_metadata()
        assert value == {"video_id": identity, "author_id": "public-author", "author": "作者",
            "description": "公开描述", "published_timestamp": 1789999999, "is_ads": False,
            "duration_milliseconds": 31000}
        assert page._douyin_responses == {"pending", "complete"}
        assert "secret" not in json.dumps(value) and "signature" not in json.dumps(value)
        payload["aweme_detail"]["aweme_id"] = "7686727666610883904"
        assert page.douyin_video_metadata() is None
    methods = [message["method"] for message in connection.messages]
    assert "Network.enable" in methods
    assert "Network.loadNetworkResource" not in methods


def test_owned_media_is_muted_before_navigation_without_system_or_other_tab_changes(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with browser.BrowserPage(connection.url, enabled=True):
        pass
    messages = connection.messages
    mute = next(value for value in messages if value["method"] == "Page.addScriptToEvaluateOnNewDocument")
    assert mute["sessionId"] == "owned-session"
    assert "v.muted=true" in mute["params"]["source"]
    assert messages.index(mute) < next(i for i, value in enumerate(messages) if value["method"] == "Page.navigate")


def test_disabled_browser_never_discovers_or_connects(monkeypatch):
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: pytest.fail("must not connect"))
    with pytest.raises(ContentAccessError, match="^browser_disabled$"):
        browser.BrowserPage("https://www.douyin.com/user/public")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "https://example.com/", "https://douyin.com.evil.test/", "file:///tmp/private", "https://www.douyin.com:444/"])
def test_browser_rejects_non_platform_urls_before_connection(monkeypatch, url):
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: pytest.fail("must not connect"))
    with pytest.raises(ContentAccessError):
        browser.BrowserPage(url, enabled=True)


def test_browser_owns_one_background_tab_and_never_lists_or_reads_cookies(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with browser.BrowserPage(connection.url, enabled=True) as page:
        assert page.snapshot()["text"] == "visible"
        assert page.click("button.visible") is True
    assert not connection.closed  # Reuse authorization for the next owned tab.
    methods = [message["method"] for message in connection.messages]
    assert methods[0] == "Target.createTarget"
    assert connection.messages[0]["params"] == {"url": "about:blank", "background": True}
    assert methods[-1] == "Target.closeTarget"
    assert connection.messages[-1]["params"] == {"targetId": "owned-tab"}
    assert not {"Target.getTargets", "Network.getCookies", "Storage.getCookies"}.intersection(methods)
    clicks = [message for message in connection.messages if message["method"] == "Input.dispatchMouseEvent"]
    assert [message["params"]["type"] for message in clicks] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert all(message["sessionId"] == "owned-session" for message in clicks)


def test_redirect_to_other_origin_is_rejected_and_owned_tab_closed(monkeypatch):
    connection = FakeSocket("https://evil.test/login")
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with pytest.raises(ContentAccessError, match="^browser_navigation_rejected$"):
        with browser.BrowserPage("https://www.douyin.com/user/public", enabled=True):
            pytest.fail("redirect must fail")
    assert not connection.closed
    assert connection.messages[-1]["method"] == "Target.closeTarget"


def test_attach_failure_is_sanitized_and_still_closes_owned_tab(monkeypatch):
    connection = FakeSocket(fail_attach=True)
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with pytest.raises(ContentAccessError, match="^browser_protocol_error$"):
        with browser.BrowserPage(connection.url, enabled=True):
            pytest.fail("attach must fail")
    assert not connection.closed
    assert connection.messages[-1]["method"] == "Target.closeTarget"


def test_cancelled_read_still_closes_only_owned_tab(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    cancelled = False
    with pytest.raises(ContentAccessError, match="^cancelled$"):
        with browser.BrowserPage(connection.url, enabled=True, cancelled=lambda: cancelled) as page:
            cancelled = True
            page.snapshot()
    assert not connection.closed
    assert connection.messages[-1]["params"] == {"targetId": "owned-tab"}


def test_browser_connection_is_reused_and_disconnect_closes_no_existing_tabs(monkeypatch):
    connection = FakeSocket()
    calls = []
    def connect(timeout):
        calls.append(timeout)
        return connection
    monkeypatch.setattr(browser, "_connect_browser", connect)
    for _ in range(2):
        with browser.BrowserPage(connection.url, enabled=True) as page:
            assert page.snapshot()["text"] == "visible"
            assert browser.disconnect_browser() is False
    assert len(calls) == 1
    messages_before = len(connection.messages)
    assert browser.disconnect_browser() is True
    assert connection.closed and len(connection.messages) == messages_before


def test_caption_capture_requires_owned_session_original_video_and_finished_response(monkeypatch):
    url = "https://www.youtube.com/watch?v=original-video"
    connection = FakeSocket(url)
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with browser.BrowserPage(url, enabled=True) as page:
        def event(identity, video, *, session="owned-session"):
            return {"sessionId": session, "method": "Network.responseReceived", "params": {
                "requestId": identity, "response": {"status": 200, "mimeType": "text/xml",
                "url": f"https://www.youtube.com/api/timedtext?v={video}&lang=en&kind=asr&pot=never-export"},
            }}
        page._record_event(event("wrong-tab", "original-video", session="private-user-tab"))
        page._record_event(event("wrong-video", "ad-video"))
        page._record_event(event("pending", "original-video"))
        page._record_event(event("correct", "original-video"))
        page._record_event({"sessionId": "owned-session", "method": "Network.loadingFinished", "params": {"requestId": "correct"}})
        values = page.caption_responses()
        assert len(values) == 1
        assert values[0]["video_id"] == "original-video"
        assert values[0]["language"] == "en" and values[0]["machine"] is True
        assert "captions" in values[0]["body"]
        assert "never-export" not in json.dumps(values)
        bodies = [message for message in connection.messages if message["method"] == "Network.getResponseBody"]
        assert [message["params"]["requestId"] for message in bodies] == ["correct"]


def test_xhs_visible_cards_without_real_ids_are_login_required_not_fake_updates():
    snapshot = {"author": "新闻晨报", "text": "电脑设备登录超限，请重新登录",
                "card_count": 12, "items": [{"title": "公开标题", "source_url":
                "https://www.xiaohongshu.com/user/profile/5c00a062000000000501b916/"}]}
    with pytest.raises(ContentAccessError, match="^browser_login_required$"):
        parse_author_snapshot("xiaohongshu", snapshot)


def test_douyin_service_error_does_not_become_an_empty_success():
    with pytest.raises(ContentAccessError, match="^browser_source_unavailable$"):
        parse_author_snapshot("douyin", {"text": "服务异常，重新刷新拉取数据", "items": []})


def test_social_parser_accepts_only_same_platform_real_post_links_as_metadata():
    url = "https://www.xiaohongshu.com/explore/6a91c254000000002501b6ef"
    snapshot = {"author": "作者", "text": "作者笔记", "items": [
        {"title": "最新观点", "source_url": url},
        {"title": "重复", "source_url": url},
        {"title": "伪造链接", "source_url": "https://example.com/explore/6a91c254000000002501b6ef"},
        {"title": "本地链接", "source_url": "http://127.0.0.1/private"},
    ]}
    results = parse_author_snapshot("xiaohongshu", snapshot)
    assert len(results) == 1
    assert results[0]["external_id"] == "6a91c254000000002501b6ef"
    assert results[0]["author"] == "作者"
    assert results[0]["content_text"] == ""
    assert results[0]["completeness"] == "metadata"
    assert results[0]["published_at"] is None


@pytest.mark.parametrize("text", ["请完成安全验证", "滑块验证"])
def test_browser_verification_challenges_are_reported_without_attempts(text):
    with pytest.raises(ContentAccessError, match="^browser_verification_required$"):
        parse_author_snapshot("douyin", {"text": text, "items": []})


def test_explicit_empty_author_feed_is_a_valid_empty_result():
    assert parse_author_snapshot("xiaohongshu", {"text": "暂无笔记", "items": []}) == []


def test_social_discovery_never_opens_disabled_browser():
    with pytest.raises(ContentAccessError, match="^browser_disabled$"):
        discover_social({"platform": "xiaohongshu", "url": "https://www.xiaohongshu.com/user/profile/5c00a062000000000501b916"})


def test_dedicated_browser_launch_uses_own_profile_and_loopback_without_stealth(monkeypatch, tmp_path):
    profile = tmp_path / "application-browser-profile"
    monkeypatch.setattr(browser, "dedicated_profile_path", lambda: profile)
    monkeypatch.setattr(browser, "_chrome_executable", lambda: "/known/chrome")
    monkeypatch.setattr(browser, "_DEDICATED_PROCESS", None)
    launched = []
    class Process:
        def poll(self):
            return None
    def start(arguments, **options):
        launched.append((arguments, options))
        (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/own-session\n")
        return Process()
    monkeypatch.setattr(browser.subprocess, "Popen", start)
    assert browser._dedicated_socket_url(2) == "ws://127.0.0.1:12345/devtools/browser/own-session"
    arguments = launched[0][0]
    assert f"--user-data-dir={profile}" in arguments
    assert "--remote-debugging-port=0" in arguments
    assert "--remote-debugging-address=127.0.0.1" in arguments
    assert not any("headless" in value or "stealth" in value or "disable-blink" in value for value in arguments)
    assert list(profile.iterdir()) == [profile / "DevToolsActivePort"]


def test_dedicated_browser_reuses_own_profile_server_without_launching(monkeypatch, tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/own-session\n")
    monkeypatch.setattr(browser, "dedicated_profile_path", lambda: tmp_path)
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    destinations = []
    def connect(destination, **kwargs):
        destinations.append(destination)
        return Connection()
    monkeypatch.setattr(browser.socket, "create_connection", connect)
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("already running"))
    assert browser._dedicated_socket_url(2).startswith("ws://127.0.0.1:12345/")
    assert destinations == [("127.0.0.1", 12345)]


def test_cancel_before_browser_entry_does_not_launch(monkeypatch):
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: pytest.fail("cancelled before connection"))
    with pytest.raises(ContentAccessError, match="^cancelled$"):
        with browser.BrowserPage("https://www.douyin.com/user/public", enabled=True, cancelled=lambda: True):
            pytest.fail("must not enter")


def test_front_is_only_allowed_for_owned_dedicated_tab(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with browser.BrowserPage(connection.url, enabled=True) as page:
        assert page.bring_to_front() is False
    assert "Page.bringToFront" not in [message["method"] for message in connection.messages]
    monkeypatch.setattr(browser, "_configured_mode", lambda: "dedicated")
    with browser.BrowserPage(connection.url, enabled=True) as page:
        assert page.bring_to_front() is True
    commands = [message for message in connection.messages if message["method"] == "Page.bringToFront"]
    assert len(commands) == 2  # Entry plus explicit request both target our tab.
    assert all(command["sessionId"] == "owned-session" for command in commands)


def _bili_response(page, identity, url, *, session="owned-session", finished=True):
    page._record_event({"sessionId": session, "method": "Network.responseReceived", "params": {
        "requestId": identity, "response": {"url": url, "status": 200},
    }})
    if finished:
        page._record_event({"sessionId": session, "method": "Network.loadingFinished", "params": {"requestId": identity}})


@pytest.mark.parametrize('subtitle_path', ['/bfs/subtitle/example.json', '/bfs/ai_subtitle/example.json'])
def test_bilibili_capture_binds_exact_player_part_and_never_exports_urls(monkeypatch, subtitle_path):
    url = "https://www.bilibili.com/video/BV123example"
    connection = FakeSocket(url)
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    track_url = "https://aisubtitle.hdslb.com" + subtitle_path + "?auth_key=never-export"
    bodies = {
        "player": json.dumps({"code": 0, "data": {"subtitle": {"subtitles": [
            {"lan": "ai-zh", "subtitle_url": track_url},
        ]}}}),
        "caption": '{"body":[{"from":0,"to":2,"content":"原文"}]}',
    }
    read = []
    def body(identity):
        read.append(identity)
        return bodies[identity]
    with browser.BrowserPage(url, enabled=True) as page:
        monkeypatch.setattr(page, "_response_body", body)
        _bili_response(page, "private", "https://api.bilibili.com/x/player/v2?aid=10&cid=20", session="other-tab")
        _bili_response(page, "other-part", "https://api.bilibili.com/x/player/v2?aid=10&cid=99")
        _bili_response(page, "player", "https://api.bilibili.com/x/player/v2?aid=10&cid=20")
        _bili_response(page, "caption", track_url)
        _bili_response(page, "unbound", "https://aisubtitle.hdslb.com/bfs/subtitle/unrelated.json")
        _bili_response(page, "pending", track_url, finished=False)
        result = page.bilibili_caption_responses(bvid="BV123example", aid=10, cid=20)
        assert read == ["player", "caption"]
        assert len(result) == 1 and result[0]["video_id"] == "BV123example"
        assert result[0]["segment_id"] == 20 and result[0]["machine"] is True
        assert "never-export" not in json.dumps(result) and "https://" not in json.dumps(result)
        assert any(message["method"] == "Network.enable" for message in connection.messages)


def test_bilibili_login_gate_is_explicit_and_unmatched_part_does_not_trigger_it(monkeypatch):
    url = "https://www.bilibili.com/video/BV123example"
    connection = FakeSocket(url)
    monkeypatch.setattr(browser, "_connect_browser", lambda *_: connection)
    with browser.BrowserPage(url, enabled=True) as page:
        monkeypatch.setattr(page, "_response_body", lambda _id: json.dumps({"code": 0, "data": {
            "need_login_subtitle": True, "subtitle": {"subtitles": []},
        }}))
        _bili_response(page, "player", "https://api.bilibili.com/x/player/wbi/v2?aid=10&cid=20")
        assert page.bilibili_caption_responses(bvid="BV123example", aid=10, cid=21) == []
        with pytest.raises(ContentAccessError, match="^browser_login_required$"):
            page.bilibili_caption_responses(bvid="BV123example", aid=10, cid=20)
        with pytest.raises(ContentAccessError, match="^browser_caption_identity_mismatch$"):
            page.bilibili_caption_responses(bvid="BVother", aid=10, cid=20)


def test_bilibili_subtitle_url_filter_rejects_credentials_local_or_foreign_hosts():
    for url in ("http://aisubtitle.hdslb.com/bfs/subtitle/a", "https://user:secret@hdslb.com/bfs/subtitle/a",
                "https://127.0.0.1/bfs/subtitle/a", "https://hdslb.com.evil.test/bfs/subtitle/a",
                "https://hdslb.com:444/bfs/subtitle/a", "https://hdslb.com/not-subtitle/a"):
        assert browser.BrowserPage._subtitle_url_hash(url) is None


def test_bilibili_rotated_auth_key_matches_only_the_same_declared_resource():
    fingerprint = browser.BrowserPage._subtitle_url_hash
    original = 'https://aisubtitle.hdslb.com/bfs/ai_subtitle/known.json?auth_key=old&lang=zh'
    refreshed = 'https://aisubtitle.hdslb.com/bfs/ai_subtitle/known.json?auth_key=new&lang=zh'
    assert fingerprint(original) == fingerprint(refreshed)
    assert fingerprint(original) != fingerprint(refreshed.replace('known.json', 'other.json'))
    assert fingerprint(original) != fingerprint(refreshed.replace('lang=zh', 'lang=en'))
    assert fingerprint(original) != fingerprint(refreshed.replace('aisubtitle.hdslb.com', 'other.hdslb.com'))


def test_media_deadline_is_bounded_once_and_cannot_extend_an_unowned_page(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(browser, '_connect_browser', lambda *_: connection)
    with browser.BrowserPage(connection.url, enabled=True) as page:
        before = page._deadline
        with pytest.raises(ContentAccessError, match='browser_invalid_media_budget'):
            page.extend_for_media_seconds(631)
        page.extend_for_media_seconds(90)
        assert 50 < page._deadline - before < 91
        with pytest.raises(ContentAccessError, match='browser_invalid_media_budget'):
            page.extend_for_media_seconds(90)
    with pytest.raises(ContentAccessError, match='browser_invalid_media_budget'):
        page.extend_for_media_seconds(10)


def test_completed_caption_body_survives_chrome_eviction_and_missing_tracks_do_not_abort(monkeypatch):
    page = browser.BrowserPage('https://www.youtube.com/watch?v=abcdefghijk', enabled=True)
    calls = []
    def command(method, params):
        assert method == 'Network.getResponseBody'
        calls.append(params['requestId'])
        if params['requestId'] == 'evicted':
            raise ContentAccessError('browser_protocol_error')
        return {'body': 'caption text', 'base64Encoded': False}
    monkeypatch.setattr(page, '_command', command)
    assert page._response_body('available') == 'caption text'
    assert page._response_body('available') == 'caption text'
    assert page._response_body('evicted') == ''
    assert page._response_body('evicted') == ''
    assert calls == ['available', 'evicted']


def test_hover_sends_only_mouse_motion_to_owned_tab(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(browser, '_connect_browser', lambda *_: connection)
    with browser.BrowserPage(connection.url, enabled=True) as page:
        assert page.hover('.subtitle-button')
    events = [message for message in connection.messages if message['method'] == 'Input.dispatchMouseEvent']
    assert len(events) == 1 and events[0]['params']['type'] == 'mouseMoved'
    assert events[0]['sessionId'] == 'owned-session'


def test_caption_body_cache_is_bounded_and_cancellation_is_not_swallowed(monkeypatch):
    page = browser.BrowserPage('https://www.youtube.com/watch?v=abcdefghijk', enabled=True)
    monkeypatch.setattr(page, '_command', lambda *_: {'body': 'x' * 1_000_000})
    for index in range(3):
        assert len(page._response_body(str(index))) == 1_000_000
    assert page._response_body('fourth') == ''
    assert page._response_cache_bytes == 3_000_000
    def cancelled(*_):
        raise ContentAccessError('cancelled')
    monkeypatch.setattr(page, '_command', cancelled)
    with pytest.raises(ContentAccessError, match='cancelled'):
        page._response_body('cancelled')


def test_login_action_defaults_disabled_before_profile_or_process_access(monkeypatch):
    monkeypatch.setattr(browser, '_chrome_executable', lambda: pytest.fail('disabled action must not launch'))
    with pytest.raises(ContentAccessError, match='^browser_disabled$'):
        browser.open_platform_login('youtube')


@pytest.mark.parametrize('platform,url', [
    ('youtube', 'https://www.youtube.com/'), ('bilibili', 'https://www.bilibili.com/'),
    ('douyin', 'https://www.douyin.com/'), ('xiaohongshu', 'https://www.xiaohongshu.com/explore'),
])
def test_login_action_opens_only_fixed_homepage_without_cdp_or_wait(monkeypatch, tmp_path, platform, url):
    import threading
    process_started, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Process:
        def wait(self):
            process_started.set()
            release.wait(2)
            finished.set()
        def poll(self):
            return None
    calls = []
    monkeypatch.setattr(browser, '_DEDICATED_PROCESS', None)
    monkeypatch.setattr(browser, '_chrome_executable', lambda: '/known/chrome')
    monkeypatch.setattr(browser, 'dedicated_profile_path', lambda: tmp_path / 'own-profile')
    monkeypatch.setattr(browser, '_connect_browser', lambda *args: pytest.fail('login must not wait for CDP'))
    monkeypatch.setattr(browser.subprocess, 'Popen', lambda args, **kw: (calls.append((args, kw)), Process())[1])
    try:
        result = browser.open_platform_login(platform, enabled=True)
        assert process_started.wait(.5) and not finished.is_set()
        assert result == {'status': 'opening', 'platform': platform, 'login_verified': False}
        arguments, options = calls[0]
        assert arguments[-1] == url
        assert arguments[0] == '/known/chrome'
        assert f'--user-data-dir={tmp_path / "own-profile"}' in arguments
        assert '--remote-debugging-address=127.0.0.1' in arguments
        assert options['shell'] is False
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize('platform,mode,code', [
    ('youtube', 'existing', 'browser_login_requires_dedicated'),
    ('https://example.org', 'dedicated', 'unsupported_login_platform'),
    ('--user-data-dir=private', 'dedicated', 'unsupported_login_platform'),
    ('rss', 'dedicated', 'unsupported_login_platform'),
])
def test_login_action_rejects_other_modes_and_untrusted_platforms_before_launch(monkeypatch, platform, mode, code):
    monkeypatch.setattr(browser, '_chrome_executable', lambda: pytest.fail('rejected action must not launch'))
    with pytest.raises(ContentAccessError, match=f'^{code}$'):
        browser.open_platform_login(platform, enabled=True, mode=mode)
