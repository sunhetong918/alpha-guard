"""Public discovery and honest content-completeness adapters.

Public endpoints can change or reject a request. Failure remains a source status;
metadata is never promoted to a transcript when captions are unavailable.
"""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import uuid
from xml.etree import ElementTree as ET

from research.ingest import MAX_TEXT_CHARS, parse_subtitles
from research.network import (ContentAccessError, FetchedContent, check_cancelled,
                              safe_fetch, validate_public_url)

MAX_ITEMS = 20
MAX_ENRICH = 5
_PLATFORMS = {"rss", "youtube", "bilibili", "douyin", "xiaohongshu"}


def capabilities() -> list[dict[str, Any]]:
    return [
        {"platform": "rss", "discovery": True, "content": "feed_or_public_article", "status": "supported"},
        {"platform": "youtube", "discovery": True, "content": "public_captions_when_accessible", "status": "conditional"},
        {"platform": "bilibili", "discovery": True, "content": "public_subtitles_when_accessible", "status": "conditional"},
        {"platform": "douyin", "discovery": True, "content": "opt_in_browser_and_local_transcription", "status": "conditional"},
        {"platform": "xiaohongshu", "discovery": True, "content": "opt_in_browser_text_and_local_transcription", "status": "conditional"},
    ]


def normalize_subscription(subscription: dict[str, Any]) -> dict[str, Any]:
    platform = str(subscription.get("platform", "rss")).strip().lower()
    if platform not in _PLATFORMS:
        raise ValueError("unsupported_platform")
    url, _ = validate_public_url(str(subscription.get("url", "")).strip(), resolve=False)
    host = urlsplit(url).hostname or ""
    allowed = {"youtube": ("youtube.com", "youtu.be"), "bilibili": ("bilibili.com",),
               "douyin": ("douyin.com",), "xiaohongshu": ("xiaohongshu.com", "xhslink.com")}
    if platform in allowed and not any(host == domain or host.endswith("." + domain) for domain in allowed[platform]):
        raise ValueError("platform_url_mismatch")
    if any(key.lower() in {"access_token", "api_key", "apikey", "token", "authorization", "password"}
           for key in parse_qs(urlsplit(url).query)):
        raise ValueError("credential_url_not_allowed")
    feed_url = str(subscription.get("feed_url") or "").strip()
    if feed_url:
        feed_url, _ = validate_public_url(feed_url, resolve=False)
        if any(key.lower() in {"access_token", "api_key", "apikey", "token", "authorization", "password"}
               for key in parse_qs(urlsplit(feed_url).query)):
            raise ValueError("credential_url_not_allowed")
    label = str(subscription.get("label") or host).strip()
    if not label or len(label) > 200:
        raise ValueError("invalid_subscription_label")
    enabled = subscription.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("invalid_subscription_enabled")
    return {"id": str(subscription.get("id") or uuid.uuid4().hex), "platform": platform,
            "url": url, "label": label, "enabled": enabled, "feed_url": feed_url or None}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _HTMLText()
    parser.feed(value)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", "".join(parser.parts))).strip()


def _read(fetch: Callable, url: str, cancelled: Callable | None) -> FetchedContent:
    check_cancelled(cancelled)
    result = fetch(url, cancelled=cancelled)
    if isinstance(result, str):
        return FetchedContent(url, result)
    if not isinstance(result, FetchedContent):
        raise ContentAccessError("invalid_fetch_result")
    if len(result.text) > 2_000_000:
        raise ContentAccessError("content_too_large")
    return result


def _date(value: str | int | float | None) -> str | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC).isoformat()
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            result = parsedate_to_datetime(str(value))
        # Feed times without timezone are unknown; do not guess server timezone.
        return result.astimezone(UTC).isoformat() if result.tzinfo else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _item(**values) -> dict[str, Any]:
    result: dict[str, Any] = {"external_id": "", "source_url": "", "title": "", "author": "",
              "published_at": None, "summary": "", "content_text": "",
              "content_kind": "metadata", "completeness": "metadata", "segments": [],
              "platform": "rss", "quality_issues": []}
    result.update(values)
    result["title"] = str(result["title"])[:500]
    result["summary"] = str(result["summary"])[:10000]
    result["author"] = str(result["author"])[:200]
    if not result["external_id"]:
        result["external_id"] = hashlib.sha256(result["source_url"].encode()).hexdigest()
    return result


def _xml(text: str) -> ET.Element:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise ContentAccessError("unsafe_xml")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        raise ContentAccessError("invalid_feed") from None


def _looks_partial(text: str) -> bool:
    return bool(re.search(
        r"subscribe to (?:read|continue|access|unlock)|sign in to (?:read|continue|access)|"
        r"订阅后阅读|登录后查看全文|付费阅读全文|开通会员.*(?:全文|阅读)|\[\+\d+ chars\]", text, re.I
    ))


def parse_feed(text: str, *, base_url: str, platform: str = "rss") -> list[dict[str, Any]]:
    root = _xml(text)
    ns = {"atom": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/",
          "content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/"}
    is_atom = root.tag == "{http://www.w3.org/2005/Atom}feed"
    if not is_atom and root.tag not in {"rss", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"}:
        raise ContentAccessError("invalid_feed")
    nodes = root.findall("atom:entry", ns) if is_atom else root.findall("./channel/item")
    items = []
    for entry in nodes[:MAX_ITEMS]:
        def find(path):
            return entry.findtext(path, default="", namespaces=ns).strip()
        if is_atom:
            links = entry.findall("atom:link", ns)
            link = next((node.get("href", "") for node in links if node.get("rel", "alternate") == "alternate"), "")
            title, identifier = find("atom:title"), find("atom:id")
            author, published = find("atom:author/atom:name"), find("atom:published")
            summary, content = find("atom:summary"), find("atom:content")
            summary = summary or find("media:group/media:description")
        else:
            link, title, identifier = find("link"), find("title"), find("guid")
            author, published = find("dc:creator") or find("author"), find("pubDate")
            summary, content = find("description"), find("content:encoded")
        if not link:
            continue
        try:
            link, _ = validate_public_url(urljoin(base_url, link), resolve=False)
        except ContentAccessError:
            continue
        text_content = plain_text(content)[:MAX_TEXT_CHARS]
        # A video description is metadata even if a platform feed calls it content.
        is_video = platform in {"youtube", "bilibili", "douyin", "xiaohongshu"}
        has_body = bool(text_content) and not is_video
        item = _item(external_id=identifier[:500], source_url=link, title=plain_text(title),
                     author=plain_text(author), published_at=_date(published), summary=plain_text(summary),
                     content_text=text_content if has_body else "", platform=platform,
                     content_kind="article" if has_body else ("summary" if summary and not is_video else "metadata"),
                     completeness="full" if has_body else ("partial" if summary and not is_video else "metadata"))
        if is_video:
            item["quality_issues"].extend(["transcript_unavailable", "recording_time_unknown"])
        if len(plain_text(content)) > MAX_TEXT_CHARS or _looks_partial(text_content):
            item["completeness"] = "partial"
            item["quality_issues"].append("content_truncated_or_gated")
        items.append(item)
    return items


class _ArticleBody(_HTMLText):
    """Read common semantic article containers, including nested div bodies."""
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _identities = {"article", "article-body", "articlebody", "article-content", "story-body", "news-content", "contentbody"}

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.finished = False

    def handle_starttag(self, tag, attrs):
        if self.finished:
            return
        values = dict(attrs)
        identities = {str(values.get("id") or "").lower(), *str(values.get("class") or "").lower().split()}
        if not self.depth and tag not in self._void and (tag == "article" or values.get("itemprop") == "articleBody" or identities & self._identities):
            self.depth = 1
        elif self.depth and tag not in self._void:
            self.depth += 1
        if self.depth:
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.depth:
            super().handle_endtag(tag)
            if tag not in self._void:
                self.depth -= 1
                if not self.depth:
                    self.finished = True

    def handle_data(self, data):
        if self.depth:
            super().handle_data(data)


def extract_article(text: str) -> tuple[str, str]:
    """Require an explicit article body before marking a webpage complete."""
    for script in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(script)
            stack = data if isinstance(data, list) else [data]
            inspected = 0
            while stack and inspected < 50:
                node = stack.pop(0)
                inspected += 1
                if isinstance(node, dict):
                    body = node.get("articleBody")
                    if isinstance(body, str) and body.strip():
                        content = plain_text(body)
                        if len(content) >= 80:
                            complete = len(content) <= MAX_TEXT_CHARS and not _looks_partial(content)
                            return content[:MAX_TEXT_CHARS], "full" if complete else "partial"
                    graph = node.get("@graph", [])
                    if isinstance(graph, list):
                        stack.extend(graph[:50])
        except (ValueError, TypeError):
            continue
    parser = _ArticleBody()
    parser.feed(text)
    body = re.sub(r"\n\s*\n+", "\n", "".join(parser.parts)).strip()
    paywall = _looks_partial(body)
    if len(body) >= 80 and not (paywall and len(body) < 600):
        complete = len(body) <= MAX_TEXT_CHARS and not paywall
        return body[:MAX_TEXT_CHARS], "full" if complete else "partial"
    return "", "metadata"


def fetch_article(
    url: str, *, title: str = "", author: str = "", platform: str = "news",
    published_at: str | None = None, fetch: Callable = safe_fetch,
    cancelled: Callable | None = None,
) -> dict[str, Any]:
    try:
        response = _read(fetch, url, cancelled)
        body, completeness = extract_article(response.text)
        page_title = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.I | re.S)
        item = _item(source_url=response.url,
                     title=title or (plain_text(page_title.group(1)) if page_title else "网页材料"),
                     author=author, platform=platform, published_at=published_at,
                     content_text=body, content_kind="article" if body else "metadata",
                     completeness=completeness,
                     quality_issues=[] if body else ["article_body_unavailable"])
        return {"status": "success" if body else "partial",
                "detail": "article_complete" if completeness == "full" else "article_body_unavailable",
                "item": item}
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ContentAccessError) else "content_fetch_failed"
        return {"status": "permission_required" if detail == "permission_required" else "failed", "detail": detail}


def _json_after(text: str, marker: str) -> dict[str, Any]:
    offset = text.find(marker)
    if offset < 0:
        return {}
    start = text.find("{", offset + len(marker))
    if start < 0:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _youtube_feed(url: str, fetch: Callable, cancelled: Callable | None) -> str:
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    match = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)", parsed.path)
    channel_id = match.group(1) if match else params.get("channel_id", [""])[0]
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        page = _read(fetch, url, cancelled).text
        found = re.search(r'"(?:channelId|externalId)"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', page)
        if not found:
            found = re.search(r"channel_id=(UC[A-Za-z0-9_-]{22})", page)
        if not found:
            raise ContentAccessError("channel_id_unavailable")
        channel_id = found.group(1)
    return "https://www.youtube.com/feeds/videos.xml?" + urlencode({"channel_id": channel_id})


def _youtube_content(item: dict[str, Any], fetch: Callable, cancelled: Callable | None):
    from .browser_video import _video_id

    identity = _video_id(item["source_url"])
    page = _read(fetch, item["source_url"], cancelled).text
    player = _json_after(page, "ytInitialPlayerResponse")
    actual_identity = player.get("videoDetails", {}).get("videoId")
    if actual_identity is not None and actual_identity != identity:
        raise ContentAccessError("video_identity_mismatch")
    tracks = player.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    if not isinstance(tracks, list) or not tracks:
        return
    track = next((value for value in tracks if value.get("kind") != "asr"), tracks[0])
    url = track.get("baseUrl")
    if not isinstance(url, str):
        return
    response = _read(fetch, url, cancelled)
    segments = parse_youtube_captions(response.text)
    _attach_transcript(item, segments, machine=track.get("kind") == "asr")


def parse_youtube_captions(text: str) -> list[dict[str, Any]]:
    """Parse a complete native caption response (JSON3, srv1/srv3 or VTT).

    Empty responses and missing clocks are unavailable content, never invented
    zero-second evidence. Non-text window/layout events do not create claims.
    """
    if not text.strip():
        raise ContentAccessError("transcript_unavailable")
    segments = parse_subtitles(text)
    if segments:
        return segments
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
            events = payload.get("events", [])
            if not isinstance(events, list) or len(events) > 5000:
                raise ContentAccessError("transcript_segment_limit")
            for event in events:
                parts = event.get("segs", [])
                if not isinstance(parts, list):
                    raise ContentAccessError("invalid_caption_response")
                body = "".join(str(part.get("utf8", "")) for part in parts).strip()
                if not body:
                    continue
                if "tStartMs" not in event:
                    raise ContentAccessError("caption_timestamp_unknown")
                start = float(event["tStartMs"]) / 1000
                end = start + float(event["dDurationMs"]) / 1000 if "dDurationMs" in event else None
                if not 0 <= start <= 86400 or (end is not None and not start <= end <= 86400):
                    raise ContentAccessError("invalid_caption_timestamp")
                segments.append({"start_seconds": start, "end_seconds": end, "text": body, "speaker": ""})
        except ContentAccessError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise ContentAccessError("invalid_caption_response") from None
        return segments
    root = _xml(text)
    nodes = [node for node in root.iter() if node.tag in {"text", "p"}]
    if len(nodes) > 5000:
        raise ContentAccessError("transcript_segment_limit")
    for node in nodes:
        timestamp_key, duration_key, scale = ("start", "dur", 1) if node.tag == "text" else ("t", "d", 1000)
        if node.get(timestamp_key) is None:
            raise ContentAccessError("caption_timestamp_unknown")
        start = float(node.attrib[timestamp_key]) / scale
        duration = float(node.attrib[duration_key]) / scale if node.get(duration_key) is not None else None
        end = start + duration if duration is not None else None
        if not 0 <= start <= 86400 or (end is not None and not start <= end <= 86400):
            raise ContentAccessError("invalid_caption_timestamp")
        body = html.unescape("".join(node.itertext())).strip()
        if body:
            segments.append({"start_seconds": start, "end_seconds": end, "text": body, "speaker": ""})
    return segments


def _attach_transcript(item: dict[str, Any], segments: list[dict], *, machine: bool = True):
    if not segments:
        return
    text = "\n".join(segment["text"] for segment in segments)
    if len(text) > MAX_TEXT_CHARS:
        item["quality_issues"].append("transcript_too_large")
        return
    item.update(content_text=text, segments=segments, content_kind="transcript", completeness="full")
    item["quality_issues"] = [issue for issue in item["quality_issues"] if issue != "transcript_unavailable"]
    if machine:
        item["quality_issues"].append("machine_transcription_unverified")


def _bilibili_discover(subscription: dict, fetch: Callable, cancelled: Callable | None) -> list[dict]:
    match = re.search(r"^/(\d+)(?:/|$)", urlsplit(subscription["url"]).path)
    if not match:
        raise ContentAccessError("author_id_unavailable")
    mid = match.group(1)
    response = _read(fetch, "https://api.bilibili.com/x/space/arc/search?" +
                     urlencode({"mid": mid, "pn": 1, "ps": MAX_ITEMS, "order": "pubdate"}), cancelled)
    payload = json.loads(response.text)
    if payload.get("code") != 0:
        raise ContentAccessError("permission_required" if payload.get("code") in {-101, -352, -412, -403} else "provider_rejected")
    videos = payload.get("data", {}).get("list", {}).get("vlist")
    if not isinstance(videos, list):
        raise ContentAccessError("invalid_provider_response")
    result = []
    for video in videos[:MAX_ITEMS]:
        bvid = video.get("bvid", "")
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            continue
        result.append(_item(external_id=bvid, source_url=f"https://www.bilibili.com/video/{bvid}",
                            title=video.get("title", ""), author=video.get("author", subscription["label"]),
                            published_at=_date(video.get("created")), summary=video.get("description", ""),
                            platform="bilibili", quality_issues=["transcript_unavailable", "recording_time_unknown"]))
    return result


def _bilibili_content(item: dict, fetch: Callable, cancelled: Callable | None):
    parsed = urlsplit(item["source_url"])
    host = parsed.hostname or ""
    if host != "bilibili.com" and not host.endswith(".bilibili.com"):
        raise ContentAccessError("invalid_video_url")
    bvid_match = re.search(r"^/video/(BV[A-Za-z0-9]{10})(?:/|$)", parsed.path)
    if not bvid_match:
        return
    response = _read(fetch, "https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": bvid_match.group(1)}), cancelled)
    payload = json.loads(response.text)
    if payload.get("code") != 0:
        raise ContentAccessError("provider_rejected")
    data = payload.get("data", {})
    if data.get("bvid") not in {None, bvid_match.group(1)}:
        raise ContentAccessError("video_identity_mismatch")
    published = _date(data.get("pubdate"))
    if published:
        item["published_at"] = published
    if data.get("title"):
        item["title"] = str(data["title"])[:1000]
    if data.get("owner", {}).get("name"):
        item["author"] = str(data["owner"]["name"])[:300]
    if data.get("desc"):
        item["summary"] = str(data["desc"])[:20000]
    aid, cid = data.get("aid"), data.get("cid")
    if not isinstance(aid, int) or not isinstance(cid, int):
        raise ContentAccessError("invalid_provider_response")
    response = _read(fetch, "https://api.bilibili.com/x/player/v2?" + urlencode({"aid": aid, "cid": cid}), cancelled)
    payload = json.loads(response.text)
    if payload.get("code") != 0:
        raise ContentAccessError("permission_required")
    player_data = payload.get("data", {})
    subtitles = player_data.get("subtitle", {}).get("subtitles", [])
    if not subtitles:
        if player_data.get("need_login_subtitle") is True:
            raise ContentAccessError("subtitle_login_required")
        return
    url = subtitles[0].get("subtitle_url")
    if not isinstance(url, str) or not url:
        return
    response = _read(fetch, urljoin("https://www.bilibili.com", url), cancelled)
    segments = parse_bilibili_captions(response.text)
    _attach_transcript(item, segments)
    if len(data.get("pages", [])) > 1 and segments:
        item["completeness"] = "partial"
        item["quality_issues"].append("multipart_video_first_part_only")


def parse_bilibili_captions(text: str) -> list[dict[str, Any]]:
    try:
        caption = json.loads(text)
        raw_segments = caption.get("body", [])
        if not isinstance(raw_segments, list) or len(raw_segments) > 5000:
            raise ContentAccessError("transcript_segment_limit")
        segments = []
        for cue in raw_segments:
            if not isinstance(cue, dict) or "from" not in cue or "to" not in cue:
                raise ContentAccessError("caption_timestamp_unknown")
            if isinstance(cue["from"], bool) or isinstance(cue["to"], bool):
                raise ContentAccessError("invalid_caption_timestamp")
            start, end = float(cue["from"]), float(cue["to"])
            if not 0 <= start <= end <= 86400:
                raise ContentAccessError("invalid_caption_timestamp")
            body = str(cue.get("content", "")).strip()
            if body:
                segments.append({"start_seconds": start, "end_seconds": end, "text": body, "speaker": ""})
        return segments
    except ContentAccessError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise ContentAccessError("invalid_caption_response") from None


def discover(
    subscription: dict[str, Any], *, fetch: Callable = safe_fetch,
    cancelled: Callable[[], bool] | None = None,
    browser_enabled: bool = False,
    media_handler: Any = None,
) -> dict[str, Any]:
    """Discover a bounded batch; content failure preserves usable metadata."""
    items: list[dict] = []
    try:
        source = normalize_subscription(subscription)
        if type(browser_enabled) is not bool:
            raise ContentAccessError("invalid_browser_setting")
        if not source["enabled"]:
            return {"status": "disabled", "detail": "subscription_paused", "items": []}
        check_cancelled(cancelled)
        platform = source["platform"]
        if platform in {"douyin", "xiaohongshu"} and not source["feed_url"]:
            if not browser_enabled:
                return {"status": "needs_configuration", "detail": "authorized_feed_or_content_import_required", "items": []}
            from .browser_social import discover_social
            extra = {"media_handler": media_handler} if media_handler is not None else {}
            items = discover_social(source, enabled=True, cancelled=cancelled, **extra)
        elif platform == "bilibili" and not source["feed_url"]:
            if browser_enabled:
                from .browser_video import discover_bilibili
                items = discover_bilibili(source, enabled=True, cancelled=cancelled)
            else:
                items = _bilibili_discover(source, fetch, cancelled)
        else:
            try:
                feed_url = source["feed_url"] or (_youtube_feed(source["url"], fetch, cancelled)
                                                    if platform == "youtube" else source["url"])
                response = _read(fetch, feed_url, cancelled)
                # Authorized generic feeds may provide real article bodies.
                # Native video descriptions always remain metadata.
                feed_platform = "rss" if source["feed_url"] else platform
                items = parse_feed(response.text, base_url=response.url, platform=feed_platform)
            except Exception:
                check_cancelled(cancelled)
                if platform != "youtube" or not browser_enabled or source["feed_url"]:
                    raise
                from .browser_video import discover_youtube
                items = discover_youtube(source, enabled=True, cancelled=cancelled)
            for item in items:
                item["platform"] = platform
                if platform == "youtube":
                    from .browser_video import _video_id
                    item["external_id"] = _video_id(item["source_url"])
                    item["source_url"] = "https://www.youtube.com/watch?v=" + item["external_id"]
                if not item["author"]:
                    item["author"] = source["label"]
        failures = 0
        for item in items[:MAX_ENRICH]:
            check_cancelled(cancelled)
            if item["completeness"] == "full":
                continue
            if media_handler is not None and platform in {"youtube", "bilibili"} and media_handler.reuse(item):
                continue
            try:
                if platform == "youtube":
                    try:
                        _youtube_content(item, fetch, cancelled)
                    except Exception:
                        if not browser_enabled:
                            raise
                        check_cancelled(cancelled)
                    if browser_enabled and item["completeness"] != "full":
                        from .browser_video import youtube_transcript
                        extra = {"media_handler": media_handler} if media_handler is not None else {}
                        youtube_transcript(item, enabled=True, cancelled=cancelled, **extra)
                elif platform == "bilibili":
                    try:
                        _bilibili_content(item, fetch, cancelled)
                    except Exception:
                        if not browser_enabled:
                            raise
                        check_cancelled(cancelled)
                    if browser_enabled and item["completeness"] != "full":
                        from .browser_video import bilibili_transcript
                        extra = {"media_handler": media_handler} if media_handler is not None else {}
                        bilibili_transcript(item, enabled=True, cancelled=cancelled, **extra)
                elif platform == "rss":
                    outcome = fetch_article(item["source_url"], fetch=fetch, cancelled=cancelled)
                    body = outcome.get("item", {})
                    if body.get("content_text"):
                        item.update({key: body[key] for key in ("content_text", "content_kind", "completeness")})
                    elif outcome["status"] == "failed":
                        failures += 1
                        item["quality_issues"].append(outcome["detail"])
            except Exception as exc:
                check_cancelled(cancelled)
                failures += 1
                item["quality_issues"].append(str(exc) if isinstance(exc, ContentAccessError) else "content_fetch_failed")
        incomplete = any(item["completeness"] != "full" for item in items)
        return {"status": "partial" if failures or incomplete else "success",
                "detail": "waiting_for_content" if incomplete else "collection_complete", "items": items}
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ContentAccessError) else "invalid_source_or_response"
        status = {"permission_required": "permission_required", "channel_id_unavailable": "needs_configuration",
                  "author_id_unavailable": "needs_configuration", "browser_unavailable": "needs_configuration",
                  "browser_disabled": "needs_configuration", "browser_login_required": "permission_required",
                  "browser_verification_required": "permission_required"}.get(detail, "failed")
        return {"status": status, "detail": detail, "items": items}
