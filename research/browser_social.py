"""Visible author updates from Douyin and Xiaohongshu, with honest gating errors."""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from research.browser_source import BrowserPage
from research.network import ContentAccessError, check_cancelled, validate_public_url

_XHS_AUTHOR_JS = """(() => ({
  url:location.href, title:document.title,
  text:document.body.innerText.slice(0,12000),
  author:document.querySelector('.user-name')?.innerText || document.title.replace(/ - 小红书$/, ''),
  card_count:document.querySelectorAll('section.note-item').length,
  items:Array.from(document.querySelectorAll('section.note-item')).slice(0,20).map(e=>({
    title:e.querySelector('a.title')?.innerText || '',
    source_url:e.querySelector('a.cover[href],a.title[href]')?.href || '',
    author:e.querySelector('a.author')?.innerText || '',
    video:!!e.querySelector('[class*=play-icon],[class*=video-icon]')
  }))
}))()"""

_XHS_CONTENT_JS = """(() => {
  const id=location.pathname.split('/').filter(Boolean).pop();
  const note=window.__INITIAL_STATE__?.note?.noteDetailMap?.[id]?.note;
  const body=typeof note?.desc==='string'?note.desc:(document.querySelector('#detail-desc')?.innerText||'');
  return {url:location.href,external_id:note?.noteId||id,type:note?.type||'',
    author_id:note?.user?.userId||'',author:note?.user?.nickname||'',
    title:(note?.title||document.querySelector('#detail-title')?.innerText||'').slice(0,1000),
    body:body.slice(0,100000),body_complete:typeof note?.desc==='string'&&body.length<=100000,
    body_truncated:body.length>100000,published_ms:note?.time||null,
    image_count:Array.isArray(note?.imageList)?note.imageList.length:null,
    page_text:document.body.innerText.slice(0,12000)};
})()"""

_DOUYIN_AUTHOR_JS = """(() => {
  const root=document.querySelector('[data-e2e="user-post-list"]');
  return {url:location.href,title:document.title,
    text:(root?.innerText || document.body.innerText).slice(0,12000),
    login_text:document.body.innerText.includes('扫码登录'),
    author:document.querySelector('[data-e2e="user-info"]')?.innerText.split('\\n')[0] || '',
    card_count:root?.querySelectorAll('li,[data-e2e="user-post-list-item"]').length || 0,
    items:Array.from(root?.querySelectorAll('a[href*="/video/"]') || []).slice(0,20).map(a=>({
      source_url:a.href,title:a.getAttribute('title') || a.querySelector('img')?.alt || a.innerText,
      author:''
    }))};
})()"""


_DOUYIN_CONTENT_JS = """(() => ({
 url:location.href,
 author_ids:[...document.querySelectorAll('[data-e2e="related-video"] [data-e2e="user-info"] a[href*="/user/"]')]
  .map(a=>new URL(a.href).pathname.split('/').filter(Boolean)[1]).filter(Boolean),
 video_ready:[...document.querySelectorAll('video')].some(v=>v.getClientRects().length&&v.readyState>=2&&v.duration>0)
}))()"""


def apply_douyin_content(item: dict[str, Any], snapshot: dict[str, Any],
                         metadata: dict[str, Any], *, author_id: str) -> None:
    """Bind native public metadata to both the exact page and subscribed author."""
    parsed = urlsplit(str(snapshot.get("url", "")))
    identity = str(item.get("external_id", ""))
    if ((parsed.hostname or "") not in {"www.douyin.com", "douyin.com"}
            or parsed.path.rstrip("/") != "/video/" + identity
            or metadata.get("video_id") != identity or not author_id
            or metadata.get("author_id") != author_id
            or author_id not in snapshot.get("author_ids", [])):
        raise ContentAccessError("author_identity_mismatch")
    if metadata.get("is_ads") is not False:
        raise ContentAccessError("media_advertisement_unverified")
    if snapshot.get("video_ready") is not True:
        raise ContentAccessError("media_unavailable")
    if metadata.get("description"):
        item["title"] = str(metadata["description"])[:500]
        item["summary"] = str(metadata["description"])[:10000]
    if metadata.get("author"):
        item["author"] = str(metadata["author"])[:200]
    timestamp = metadata.get("published_timestamp")
    if type(timestamp) is int and 0 < timestamp < 10**11:
        try:
            item["published_at"] = datetime.fromtimestamp(timestamp, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass


def parse_author_snapshot(platform: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Only accept actual post identifiers, never footer recommendations or titles as IDs."""
    if platform not in {"douyin", "xiaohongshu"}:
        raise ContentAccessError("unsupported_platform")
    text = str(snapshot.get("text", ""))
    if re.search(r"请完成.*验证|安全验证|滑块验证|验证码验证", text):
        raise ContentAccessError("browser_verification_required")
    if re.search(r"服务异常|访问频繁|访问受限|操作过于频繁", text):
        raise ContentAccessError("browser_source_unavailable")
    results = []
    seen: set[str] = set()
    for raw in snapshot.get("items", [])[:20]:
        if not isinstance(raw, dict):
            continue
        try:
            url, _ = validate_public_url(str(raw.get("source_url", "")), resolve=False)
        except ContentAccessError:
            continue
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        domain = "douyin.com" if platform == "douyin" else "xiaohongshu.com"
        if host != domain and not host.endswith("." + domain):
            continue
        pattern = (r"/video/(\d{10,30})/?$" if platform == "douyin"
                   else r"/(?:explore|search_result)/([a-fA-F0-9]{24})/?$|/user/profile/[a-fA-F0-9]{24}/([a-fA-F0-9]{24})/?$")
        match = re.fullmatch(pattern, parsed.path)
        if match is None:
            continue
        identity = next(value for value in match.groups() if value)
        title = str(raw.get("title", "")).strip()
        if identity in seen or not title:
            continue
        seen.add(identity)
        results.append({
            # The normal navigation href may contain ephemeral access tokens.
            # Use it only within discover_social; evidence keeps a stable URL.
            "external_id": identity, "source_url": (f"https://www.xiaohongshu.com/explore/{identity}" if platform == "xiaohongshu" else f"https://www.douyin.com/video/{identity}"), "title": title[:500],
            "author": str(raw.get("author") or snapshot.get("author") or "")[:200],
            "published_at": None, "summary": title[:10000], "content_text": "",
            "content_kind": "metadata", "completeness": "metadata", "segments": [],
            "platform": platform,
            "quality_issues": ["transcript_unavailable", "recording_time_unknown", "browser_metadata_only"],
        })
    if results:
        return results
    if (re.search(r"登录超限|请重新登录|登录后.*(?:查看|作品|笔记)|登录探索更多", text)
            or snapshot.get("login_text") or snapshot.get("card_count", 0)):
        raise ContentAccessError("browser_login_required")
    if re.search(r"暂无(?:作品|笔记)|还没有(?:发布|作品|笔记)", text):
        return []
    raise ContentAccessError("browser_content_unavailable")


def apply_xiaohongshu_content(item: dict[str, Any], snapshot: dict[str, Any], *,
                             author_id: str) -> bool:
    """Attach the complete textual component of an identity-checked note.

    Images remain an explicit review requirement. A video's description never
    becomes a full article or transcript. Return whether the note is a video.
    """
    actual_url = urlsplit(str(snapshot.get("url", "")))
    if ((actual_url.hostname or "") not in {"www.xiaohongshu.com", "xiaohongshu.com"}
            or not re.fullmatch(r"/(?:explore|search_result)/[a-fA-F0-9]{24}/?|/user/profile/[a-fA-F0-9]{24}/[a-fA-F0-9]{24}/?", actual_url.path)
            or actual_url.path.rstrip("/").split("/")[-1] != item.get("external_id")
            or snapshot.get("external_id") != item.get("external_id")
            or not author_id or snapshot.get("author_id") != author_id):
        raise ContentAccessError("author_identity_mismatch")
    body = str(snapshot.get("body") or "").strip()
    note_type = snapshot.get("type")
    timestamp = snapshot.get("published_ms")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and 0 < timestamp < 10**14:
        try:
            item["published_at"] = datetime.fromtimestamp(timestamp / 1000, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    if snapshot.get("title"):
        item["title"] = str(snapshot["title"])[:500]
    if snapshot.get("author"):
        item["author"] = str(snapshot["author"])[:200]
    if body:
        item["summary"] = body[:10000]
    if note_type == "video":
        return True
    if note_type != "normal":
        raise ContentAccessError("browser_content_type_unverified")
    # A poster can have a long caption composed solely of topic tags. Those
    # labels are metadata, even when the native description is untruncated.
    substantive = re.sub(r"#[^#\n]+#", "", body).strip()
    if len(substantive) < 40 or body == str(item.get("title", "")).strip():
        raise ContentAccessError("browser_content_unavailable")
    item.update(content_text=body[:100000], content_kind="article", segments=[],
                completeness="full" if snapshot.get("body_complete") is True else "partial")
    issues = [issue for issue in item.get("quality_issues", [])
              if issue not in {"transcript_unavailable", "recording_time_unknown", "browser_metadata_only"}]
    issues.append("browser_public_page")
    if snapshot.get("image_count") != 0:
        issues.append("visual_content_not_extracted")
    if snapshot.get("body_complete") is not True:
        issues.append("browser_content_extent_unverified")
    item["quality_issues"] = list(dict.fromkeys(issues))
    return False


def discover_social(subscription: dict[str, Any], *, enabled: bool = False,
                    cancelled: Callable[[], bool] | None = None,
                    max_content_items: int = 5,
                    media_handler: Callable[[BrowserPage, dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    platform = str(subscription.get("platform", ""))
    if platform not in {"douyin", "xiaohongshu"}:
        raise ContentAccessError("unsupported_platform")
    if type(max_content_items) is not int or not 0 <= max_content_items <= 5:
        raise ContentAccessError("invalid_content_limit")
    check_cancelled(cancelled)
    with BrowserPage(str(subscription.get("url", "")), enabled=enabled, cancelled=cancelled) as page:
        if platform == "douyin":
            page.wait_for("(() => {const e=document.querySelector('[data-e2e=\"user-post-list\"]');return !!e && (!!e.querySelector('a[href*=\"/video/\"]') || /服务异常|访问频繁|暂无作品|暂无视频|登录/.test(e.innerText));})()", timeout=10)
            expression = _DOUYIN_AUTHOR_JS
        else:
            page.wait_for("document.querySelectorAll('section.note-item').length > 0 || /登录超限|请重新登录|暂无笔记/.test(document.body.innerText)", timeout=8)
            expression = _XHS_AUTHOR_JS
        snapshot = page.evaluate(expression)
        if not isinstance(snapshot, dict):
            raise ContentAccessError("browser_page_error")
        pattern = (r"^/user/([^/]+)" if platform == "douyin"
                   else r"^/user/profile/([a-fA-F0-9]{24})")
        expected = re.match(pattern, urlsplit(str(subscription.get("url", ""))).path)
        actual = re.match(pattern, urlsplit(str(snapshot.get("url", ""))).path)
        if not actual or (expected and expected.group(1) != actual.group(1)):
            raise ContentAccessError("author_identity_mismatch")
        items = parse_author_snapshot(platform, snapshot)
        # Preserve native hrefs only in this operation's memory, before the
        # metadata parser strips query parameters from evidence source URLs.
        navigation: dict[str, str] = {}
        for raw in snapshot.get("items", [])[:20]:
            if isinstance(raw, dict):
                try:
                    native = parse_author_snapshot(platform, {"items": [raw]})
                except ContentAccessError:
                    continue
                if native:
                    navigation.setdefault(native[0]["external_id"], raw.get("source_url", ""))
        author_id = expected.group(1) if expected else ""
    for item in items[:max_content_items]:
        check_cancelled(cancelled)
        if platform == "douyin" and media_handler is None:
            continue
        try:
            with BrowserPage(navigation[item["external_id"]], enabled=enabled, cancelled=cancelled,
                             timeout=40) as detail_page:
                is_video = platform == "douyin"
                if platform == "xiaohongshu":
                    detail_page.wait_for("Boolean(document.querySelector('#detail-desc'))", timeout=10)
                    detail = detail_page.evaluate(_XHS_CONTENT_JS)
                    if not isinstance(detail, dict):
                        raise ContentAccessError("browser_page_error")
                    is_video = apply_xiaohongshu_content(item, detail, author_id=author_id)
                else:
                    detail_page.wait_for("Boolean(document.querySelector('[data-e2e=\"related-video\"] [data-e2e=\"user-info\"] a[href*=\"/user/\"]')) && [...document.querySelectorAll('video')].some(v=>v.getClientRects().length&&v.readyState>=2&&v.duration>0)", timeout=12)
                    deadline = time.monotonic() + 5
                    metadata = detail_page.douyin_video_metadata()
                    while metadata is None and time.monotonic() < deadline:
                        check_cancelled(cancelled)
                        time.sleep(.2)
                        metadata = detail_page.douyin_video_metadata()
                    if metadata is None:
                        raise ContentAccessError("browser_content_identity_unverified")
                    detail = detail_page.evaluate(_DOUYIN_CONTENT_JS)
                    if not isinstance(detail, dict):
                        raise ContentAccessError("browser_page_error")
                    apply_douyin_content(item, detail, metadata, author_id=author_id)
                if is_video and media_handler is not None:
                    media_handler(detail_page, item)
        except Exception as exc:
            check_cancelled(cancelled)
            code = str(exc) if isinstance(exc, ContentAccessError) else "content_fetch_failed"
            item["quality_issues"] = list(dict.fromkeys([*item["quality_issues"], code]))
    return items
