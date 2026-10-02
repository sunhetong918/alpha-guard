"""Opt-in video evidence from the website's own normal browser page.

Only fixed extractors run in a tab owned by this operation. Browser responses
are read after the website loads them; request credentials and signed URLs are
never copied, generated or replayed by the application.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .browser_source import BrowserPage
from .network import ContentAccessError, check_cancelled

_BILIBILI_CARDS = r"""(() => {
  const cards=[...document.querySelectorAll('.video-section .bili-video-card')];
  return {url:location.href, author:(document.querySelector('.nickname')?.innerText||'').trim(),
    videos:cards.slice(0,20).map(card=>{
      const a=card.querySelector('.bili-video-card__title a[href]');
      return {url:a?.href||'',title:(a?.innerText||'').trim()};
    })};
})()"""
_YOUTUBE_AUTHOR_VIDEOS = r"""(() => {
  const data=window.ytInitialData||{}, meta=data.metadata?.channelMetadataRenderer||{};
  const tabs=data.contents?.twoColumnBrowseResultsRenderer?.tabs||[];
  const selected=tabs.map(t=>t.tabRenderer).find(t=>t?.selected);
  const browse=document.querySelector('ytd-browse[page-subtype="channels"]');
  const cards=browse?[...browse.querySelectorAll('ytd-rich-grid-renderer ytd-rich-item-renderer,ytd-grid-renderer ytd-grid-video-renderer')]:[];
  const chip=browse?.querySelector('yt-chip-cloud-chip-renderer[selected]');
  const message=browse?.querySelector('ytd-message-renderer')?.innerText||'';
  return {url:location.href,channel_id:meta.externalId||'',author:meta.title||'',
    vanity_url:meta.vanityChannelUrl||'',
    selected_tab_url:selected?.endpoint?.commandMetadata?.webCommandMetadata?.url||'',
    selected_tab_title:selected?.title||'',sort_label:(chip?.innerText||'').trim(),
    empty:/has no videos|没有视频|尚无视频|沒有影片|沒有任何影片/i.test(message),
    videos:cards.filter(card=>!card.querySelector('ytd-ad-slot-renderer')).slice(0,20).map(card=>{
      const a=card.querySelector('a#video-title-link[href],a#video-title[href],a.yt-lockup-metadata-view-model-wiz__title[href]')
        ||card.querySelector('a[href*="/watch?v="],a[href*="/shorts/"]');
      const title=card.querySelector('#video-title,h3')?.textContent||a?.getAttribute('title')||a?.innerText||'';
      return {url:a?.href||'',title:title.trim()};
    })};
})()"""
_YOUTUBE_META = r"""(() => {
  document.querySelectorAll('video').forEach(v=>{v.muted=true;});
  const p=window.ytInitialPlayerResponse||{};
  return {video_id:p.videoDetails?.videoId||'',title:p.videoDetails?.title||'',
    author:p.videoDetails?.author||'',duration_seconds:Number(p.videoDetails?.lengthSeconds)||null,
    playability:p.playabilityStatus?.status||'',
    cc_enabled:document.querySelector('button.ytp-subtitles-button')?.getAttribute('aria-pressed')==='true'};
})()"""
_YOUTUBE_PLAYBACK_IDENTITY = r"""(() => {
  const player=document.querySelector('#movie_player');
  return {video_id:player?.getVideoData?.()?.video_id||'',
    page_video_id:new URL(location.href).searchParams.get('v')||'',
    ad:Boolean(player?.classList.contains('ad-showing')||player?.classList.contains('ad-interrupting'))};
})()"""
_YOUTUBE_DOM_TRANSCRIPT = r"""(() => {
  const rows=[...document.querySelectorAll('ytd-transcript-segment-renderer')];
  if(!rows.length) return null;
  if(rows.length>5000) return {over_limit:true};
  return {rows:rows.map(row=>({
    timestamp:(row.querySelector('.segment-timestamp')?.innerText||'').trim(),
    text:(row.querySelector('.segment-text')?.innerText||'').trim()
  })),pending:Boolean(document.querySelector('ytd-transcript-segment-list-renderer tp-yt-paper-spinner[active]'))};
})()"""


def _video_id(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if not any(host == domain or host.endswith('.' + domain) for domain in ('youtube.com', 'youtu.be')):
        raise ContentAccessError('invalid_video_url')
    match = re.search(r"/(?:shorts|embed)/([A-Za-z0-9_-]{11})(?:/|$)", parsed.path)
    value = match.group(1) if match else parse_qs(parsed.query).get('v', [''])[0]
    if parsed.hostname in {'youtu.be', 'www.youtu.be'}:
        value = parsed.path.strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ContentAccessError('invalid_video_url')
    return value


def _youtube_author_identity(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if host != 'youtube.com' and not host.endswith('.youtube.com'):
        raise ContentAccessError('invalid_author_url')
    path = unquote(parsed.path).rstrip('/')
    match = re.fullmatch(r'/(channel/UC[A-Za-z0-9_-]{22}|@[^/\s?#]+)(?:/(?:videos|shorts|featured))?', path)
    if not match:
        raise ContentAccessError('invalid_author_url')
    value = match.group(1)
    return ('channel', value.split('/', 1)[1]) if value.startswith('channel/') else ('handle', value[1:])


def discover_youtube(subscription: dict[str, Any], *, enabled: bool = False,
                      cancelled: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """Read the author's latest Videos tab; never use home-page recommendations."""
    from .connectors import _item

    kind, expected = _youtube_author_identity(subscription['url'])
    base = f'channel/{expected}' if kind == 'channel' else f'@{expected}'
    with BrowserPage(f'https://www.youtube.com/{base}/videos', enabled=enabled,
                     cancelled=cancelled, timeout=30) as page:
        page.wait_for("Boolean(window.ytInitialData?.metadata?.channelMetadataRenderer?.externalId)", timeout=8)
        page.wait_for("Boolean(document.querySelector('ytd-browse[page-subtype=\"channels\"] ytd-rich-grid-renderer ytd-rich-item-renderer,ytd-browse[page-subtype=\"channels\"] ytd-grid-video-renderer,ytd-browse[page-subtype=\"channels\"] ytd-message-renderer'))", timeout=10)
        snapshot = page.evaluate(_YOUTUBE_AUTHOR_VIDEOS)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('videos'), list):
        raise ContentAccessError('browser_page_error')
    channel_id = str(snapshot.get('channel_id') or '')
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
        raise ContentAccessError('author_identity_mismatch')
    current = str(snapshot.get('url') or '')
    actual_kind, actual_value = _youtube_author_identity(current)
    if not urlsplit(current).path.rstrip('/').endswith('/videos'):
        raise ContentAccessError('browser_author_updates_unavailable')
    if kind == 'channel':
        if channel_id != expected:
            raise ContentAccessError('author_identity_mismatch')
    elif actual_kind != 'handle' or actual_value.casefold() != expected.casefold():
        # Redirects to another handle cannot silently replace a subscription.
        raise ContentAccessError('author_identity_mismatch')
    tab_url = str(snapshot.get('selected_tab_url') or '')
    tab_title = str(snapshot.get('selected_tab_title') or '').casefold()
    if not (urlsplit(tab_url).path.rstrip('/').endswith('/videos')
            or tab_title in {'videos', '视频', '影片', '動画'}):
        raise ContentAccessError('browser_author_updates_unavailable')
    sort_label = str(snapshot.get('sort_label') or '').casefold()
    if sort_label and not (sort_label.startswith('latest') or sort_label.startswith('最新')):
        raise ContentAccessError('browser_latest_videos_unavailable')
    author = str(snapshot.get('author') or subscription.get('label') or channel_id)[:300]
    result, seen = [], set()
    for video in snapshot['videos'][:20]:
        check_cancelled(cancelled)
        if not isinstance(video, dict):
            continue
        try:
            identity = _video_id(str(video.get('url') or ''))
        except ContentAccessError:
            continue
        title = str(video.get('title') or '').strip()
        if not title or identity in seen:
            continue
        seen.add(identity)
        path = urlsplit(str(video['url'])).path
        url = f'https://www.youtube.com/shorts/{identity}' if path.startswith('/shorts/') else f'https://www.youtube.com/watch?v={identity}'
        result.append(_item(external_id=identity, source_url=url, title=title, author=author,
            platform='youtube', published_at=None,
            quality_issues=['transcript_unavailable', 'recording_time_unknown', 'browser_latest_videos_tab']))
    if not result and snapshot.get('empty') is not True:
        raise ContentAccessError('browser_author_updates_unavailable')
    return result


def discover_bilibili(subscription: dict[str, Any], *, enabled: bool = False,
                       cancelled: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """Read only the author's latest video section, excluding featured videos."""
    from .connectors import _item

    with BrowserPage(subscription['url'], enabled=enabled, cancelled=cancelled) as page:
        found = page.wait_for("document.querySelector('.video-section .bili-video-card__title a[href]') !== null", timeout=12)
        if not found:
            raise ContentAccessError('browser_author_updates_unavailable')
        snapshot = page.evaluate(_BILIBILI_CARDS)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('videos'), list):
        raise ContentAccessError('browser_page_error')
    expected_mid = re.match(r'^/(\d+)(?:/|$)', urlsplit(subscription['url']).path)
    actual_mid = re.match(r'^/(\d+)(?:/|$)', urlsplit(str(snapshot.get('url', ''))).path)
    if not expected_mid or not actual_mid or expected_mid.group(1) != actual_mid.group(1):
        raise ContentAccessError('author_identity_mismatch')
    author = str(snapshot.get('author') or subscription['label'])[:300]
    result, identities = [], set()
    for video in snapshot['videos'][:20]:
        check_cancelled(cancelled)
        parsed = urlsplit(str(video.get('url', '')))
        host = parsed.hostname or ''
        match = re.match(r'^/video/(BV[A-Za-z0-9]{10})(?:/|$)', parsed.path)
        if not match or not (host == 'bilibili.com' or host.endswith('.bilibili.com')):
            continue
        identity = match.group(1)
        title = str(video.get('title', '')).strip()
        if not title or identity in identities:
            continue
        identities.add(identity)
        result.append(_item(external_id=identity, source_url=f'https://www.bilibili.com/video/{identity}',
            title=title[:1000], author=author, platform='bilibili', published_at=None,
            quality_issues=['transcript_unavailable', 'recording_time_unknown', 'browser_public_page']))
    if not result:
        raise ContentAccessError('browser_author_updates_unavailable')
    return result


def _dom_segments(payload: Any) -> list[dict[str, Any]]:
    """DOM timestamps are exact cue starts; unknown ends remain unknown."""
    if not isinstance(payload, dict) or payload.get('pending'):
        return []
    if payload.get('over_limit'):
        raise ContentAccessError('transcript_segment_limit')
    rows = payload.get('rows', [])
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ContentAccessError('transcript_segment_limit')
    result = []
    for row in rows:
        timestamp, body = str(row.get('timestamp', '')).strip(), str(row.get('text', '')).strip()
        if not body:
            continue
        if not re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?', timestamp):
            raise ContentAccessError('caption_timestamp_unknown')
        parts = [int(value) for value in timestamp.split(':')]
        if any(value >= 60 for value in parts[1:]):
            raise ContentAccessError('invalid_caption_timestamp')
        start = sum(value * 60 ** power for power, value in enumerate(reversed(parts)))
        if start > 86400:
            raise ContentAccessError('invalid_caption_timestamp')
        result.append({'start_seconds': start, 'end_seconds': None, 'text': body, 'speaker': ''})
    return result


def youtube_transcript(item: dict[str, Any], *, enabled: bool = False,
                       cancelled: Callable[[], bool] | None = None,
                       media_handler: Callable[[BrowserPage, dict[str, Any]], None] | None = None) -> None:
    from .connectors import _attach_transcript, parse_youtube_captions

    identity = _video_id(item['source_url'])
    # The normal watch page exposes captions for Shorts too, preserving the
    # same public video identity without changing client signatures or tokens.
    with BrowserPage(f'https://www.youtube.com/watch?v={identity}', enabled=enabled,
                     cancelled=cancelled, timeout=60) as page:
        if not page.wait_for('Boolean(window.ytInitialPlayerResponse?.videoDetails?.videoId || window.ytInitialPlayerResponse?.playabilityStatus?.status)', timeout=8):
            raise ContentAccessError('browser_video_unavailable')
        metadata = page.evaluate(_YOUTUBE_META)
        if isinstance(metadata, dict) and metadata.get('playability') == 'LOGIN_REQUIRED':
            raise ContentAccessError('browser_login_required')
        if not isinstance(metadata, dict) or metadata.get('video_id') != identity:
            raise ContentAccessError('video_identity_mismatch')
        if metadata.get('playability') not in {'OK', ''}:
            raise ContentAccessError('browser_video_unavailable')
        item['external_id'] = identity
        for key, limit in (('title', 1000), ('author', 300)):
            if metadata.get(key):
                item[key] = str(metadata[key])[:limit]
        # Initial player JSON is in static HTML before its controls are mounted.
        # A one-shot early click would silently miss captions on a fresh profile.
        page.wait_for("Boolean(document.querySelector('button.ytp-subtitles-button')?.getClientRects().length)", timeout=8)
        metadata = page.evaluate(_YOUTUBE_META)
        if page.evaluate("Boolean(document.querySelector('video')?.paused)"):
            page.click('button.ytp-large-play-button') or page.click('button.ytp-play-button')
        if not metadata.get('cc_enabled'):
            page.click('button.ytp-subtitles-button')
        # Clicking only a currently visible, ordinary UI control permits the
        # website to load its own transcript; no internal API is forged.
        page.wait_for("[...document.querySelectorAll('#description-inline-expander #expand')].some(e=>e.getClientRects().length)", timeout=5)
        if not page.click('#description-inline-expander #expand'):
            page.click('#description.ytd-watch-metadata')
        page.wait_for("[...document.querySelectorAll('ytd-video-description-transcript-section-renderer button')].some(e=>e.getClientRects().length)", timeout=3)
        page.click('ytd-video-description-transcript-section-renderer button')
        page.wait_for("[...document.querySelectorAll('button[aria-label=\"Transcript\"]')].some(e=>e.getClientRects().length)", timeout=2)
        page.click('button[aria-label="Transcript"]')
        deadline = time.monotonic() + 10
        caption_error: str | None = None
        while True:
            check_cancelled(cancelled)
            responses = page.caption_responses()
            for response in sorted(responses, key=lambda value: bool(value.get('machine'))):
                if response.get('video_id') != identity or not response.get('body'):
                    continue
                try:
                    segments = parse_youtube_captions(response['body'])
                except ContentAccessError as exc:
                    caption_error = str(exc)
                    continue
                if not segments:
                    continue
                duration = metadata.get('duration_seconds')
                if duration and any(segment['start_seconds'] > duration + 1 for segment in segments):
                    raise ContentAccessError('caption_video_duration_mismatch')
                _attach_transcript(item, segments, machine=response.get('machine') is not False)
                if item.get('completeness') == 'full':
                    item['quality_issues'].append('browser_public_captions')
                    if metadata.get('title'):
                        item['title'] = str(metadata['title'])[:1000]
                    if metadata.get('author'):
                        item['author'] = str(metadata['author'])[:300]
                return
            dom = page.evaluate(_YOUTUBE_DOM_TRANSCRIPT)
            segments = _dom_segments(dom)
            if segments:
                # DOM may be virtualized. Without a complete native response
                # keep visible transcript excerpts explicitly partial.
                _attach_transcript(item, segments)
                item['completeness'] = 'partial'
                item['quality_issues'].append('browser_transcript_extent_unverified')
                _youtube_media_fallback(page, item, identity, media_handler)
                return
            if time.monotonic() >= deadline:
                if _youtube_media_fallback(page, item, identity, media_handler):
                    return
                raise ContentAccessError(caption_error or 'browser_transcript_unavailable')
            time.sleep(0.2)


def _youtube_media_fallback(page: BrowserPage, item: dict[str, Any], identity: str,
                            media_handler: Callable[[BrowserPage, dict[str, Any]], None] | None) -> bool:
    if media_handler is None:
        return False
    state = page.evaluate(_YOUTUBE_PLAYBACK_IDENTITY)
    if isinstance(state, dict) and state.get('ad') is True:
        # Use only the website's currently available ordinary skip control,
        # otherwise wait for the advertisement to finish before recording.
        page.click('button.ytp-ad-skip-button-modern') or page.click('button.ytp-skip-ad-button')
        state = page.wait_for("(() => {const state=" + _YOUTUBE_PLAYBACK_IDENTITY
            + ";return state.ad===false ? state : null;})()", timeout=30)
        if not state:
            raise ContentAccessError('browser_media_advertisement')
    if (not isinstance(state, dict) or state.get('video_id') != identity
            or state.get('page_video_id') != identity):
        raise ContentAccessError('video_identity_mismatch')
    if state.get('ad') is not False:
        raise ContentAccessError('browser_media_advertisement')
    media_handler(page, item)
    return item.get('completeness') == 'full' and item.get('content_kind') == 'transcript'


_BILIBILI_META = r"""(() => {
  document.querySelectorAll('video').forEach(v=>{v.muted=true;});
  const d=window.__INITIAL_STATE__?.videoData||{};
  const state=window.__INITIAL_STATE__||{};
  return {video_id:d.bvid||state.bvid||'',aid:d.aid,cid:state.cid||d.cid,
    title:d.title||'',author:d.owner?.name||'',published_at:d.pubdate,
    duration_seconds:d.pages?.find(p=>p.cid===(state.cid||d.cid))?.duration||d.duration,
    part_count:d.pages?.length||1};
})()"""


def bilibili_transcript(item: dict[str, Any], *, enabled: bool = False,
                         cancelled: Callable[[], bool] | None = None,
                         media_handler: Callable[[BrowserPage, dict[str, Any]], None] | None = None) -> None:
    from .connectors import _attach_transcript, _date, parse_bilibili_captions

    parsed = urlsplit(item['source_url'])
    host = parsed.hostname or ''
    match = re.match(r'^/video/(BV[A-Za-z0-9]{10})(?:/|$)', parsed.path)
    if not match or (host != 'bilibili.com' and not host.endswith('.bilibili.com')):
        raise ContentAccessError('invalid_video_url')
    identity = match.group(1)
    with BrowserPage(item['source_url'], enabled=enabled, cancelled=cancelled, timeout=30) as page:
        if not page.wait_for('Boolean(window.__INITIAL_STATE__?.videoData?.bvid)', timeout=8):
            raise ContentAccessError('browser_video_unavailable')
        metadata = page.evaluate(_BILIBILI_META)
        if not isinstance(metadata, dict) or metadata.get('video_id') != identity:
            raise ContentAccessError('video_identity_mismatch')
        aid, cid = metadata.get('aid'), metadata.get('cid')
        if type(aid) is not int or type(cid) is not int or min(aid, cid) <= 0:
            raise ContentAccessError('video_identity_mismatch')
        item['external_id'] = identity
        for key in ('title', 'author'):
            if metadata.get(key):
                item[key] = str(metadata[key])[:1000 if key == 'title' else 300]
        published = _date(metadata.get('published_at'))
        if published:
            item['published_at'] = published
        # Read the player binding promptly: long media responses can evict its
        # small response body from Chrome's network buffer while controls mount.
        responses = page.bilibili_caption_responses(bvid=identity, aid=aid, cid=cid)
        if not responses:
            # Bilibili's subtitle label is a quick toggle; languages are in a
            # hover menu. Clicking only the label can leave captions disabled.
            page.wait_for("Boolean(document.querySelector('.bpx-player-ctrl-subtitle')?.getClientRects().length)", timeout=8)
            # The label mounts before the player finishes loading its actual
            # tracks and event handlers. Wait for populated language choices.
            page.wait_for("document.querySelector('.bpx-player-ctrl-subtitle-major .bpx-player-ctrl-subtitle-language-item') !== null", timeout=8)
            responses = page.bilibili_caption_responses(bvid=identity, aid=aid, cid=cid)
            page.evaluate("document.querySelectorAll('video').forEach(v=>{v.muted=true})")
            if page.evaluate("Boolean(document.querySelector('video')?.paused)"):
                page.click('.bpx-player-ctrl-play')
            page.hover('.bpx-player-ctrl-subtitle')
            if page.wait_for("[...document.querySelectorAll('.bpx-player-ctrl-subtitle-major .bpx-player-ctrl-subtitle-language-item')].some(e=>e.getClientRects().length)", timeout=3):
                prefix = '.bpx-player-ctrl-subtitle-major .bpx-player-ctrl-subtitle-language-item'
                for language in ('zh-CN', 'zh-Hans', 'zh', 'ai-zh', ''):
                    selector = prefix + (f'[data-lan="{language}"]' if language else '')
                    if page.click(selector):
                        break
        deadline = time.monotonic() + 8
        while True:
            check_cancelled(cancelled)
            for response in responses:
                if response.get('video_id') != identity or response.get('segment_id') != cid:
                    continue
                segments = parse_bilibili_captions(str(response.get('body', '')))
                if not segments:
                    continue
                duration = metadata.get('duration_seconds')
                if duration and any(segment['start_seconds'] > duration + 1 for segment in segments):
                    raise ContentAccessError('caption_video_duration_mismatch')
                _attach_transcript(item, segments, machine=response.get('machine') is not False)
                if item.get('completeness') == 'full':
                    item['quality_issues'].append('browser_public_captions')
                    if metadata.get('part_count', 1) > 1:
                        item['completeness'] = 'partial'
                        item['quality_issues'].append('multipart_video_first_part_only')
                return
            if time.monotonic() >= deadline:
                if media_handler is not None:
                    # The handler persists a complete recording immediately.
                    # One part can never be cached as this entire multi-part work.
                    if metadata.get('part_count', 1) > 1:
                        item['quality_issues'].append('multipart_video_first_part_only')
                        raise ContentAccessError('browser_transcript_unavailable')
                    state = page.evaluate("""(() => {const s=window.__INITIAL_STATE__||{},d=s.videoData||{};
                      return {video_id:d.bvid,aid:d.aid,cid:s.cid||d.cid,
                        duration:document.querySelector('video')?.duration,
                        ad:Boolean(document.querySelector('.bpx-player-ad')?.getClientRects().length)};})()""")
                    duration = metadata.get('duration_seconds')
                    if (not isinstance(state, dict) or state.get('video_id') != identity
                            or state.get('aid') != aid or state.get('cid') != cid
                            or not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0
                            or type(state.get('duration')) not in {int, float}
                            or not abs(state['duration'] - duration) <= 2):
                        raise ContentAccessError('video_identity_mismatch')
                    if state.get('ad') is not False:
                        raise ContentAccessError('browser_media_advertisement')
                    media_handler(page, item)
                    if item.get('completeness') == 'full' and item.get('content_kind') == 'transcript':
                        return
                raise ContentAccessError('browser_transcript_unavailable')
            time.sleep(.2)
            responses = page.bilibili_caption_responses(bvid=identity, aid=aid, cid=cid)
