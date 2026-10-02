from __future__ import annotations

from copy import deepcopy

import pytest

from research import browser_social as social
from research.network import ContentAccessError

AUTHOR = '5c00a062000000000501b916'
NOTE = '6abba491000000001c02e14b'
URL = f'https://www.xiaohongshu.com/user/profile/{AUTHOR}/{NOTE}'


def _item():
    return social.parse_author_snapshot('xiaohongshu', {'author': '新闻晨报', 'items': [
        {'title': '正文标题', 'source_url': URL + '?xsec_token=ephemeral-token&xsec_source=pc_user'}
    ]})[0]


def _note(**values):
    return {'url': URL, 'external_id': NOTE, 'type': 'normal', 'author_id': AUTHOR,
            'author': '新闻晨报', 'body': '可引用的独立文字正文和原始事实描述。' * 10,
            'body_complete': True, 'published_ms': 1790682257000, 'image_count': 1, **values}


def test_normal_note_keeps_full_text_time_and_explicit_unread_images():
    item = _item()
    assert social.apply_xiaohongshu_content(item, _note(), author_id=AUTHOR) is False
    assert item['content_kind'] == 'article' and item['completeness'] == 'full'
    assert item['content_text'] == _note()['body']
    assert item['published_at'] == '2026-09-29T11:44:17+00:00'
    assert 'visual_content_not_extracted' in item['quality_issues']
    assert 'browser_public_page' in item['quality_issues']
    assert 'transcript_unavailable' not in item['quality_issues']
    assert item['source_url'] == f'https://www.xiaohongshu.com/explore/{NOTE}' and 'ephemeral-token' not in str(item)


def test_video_description_never_becomes_full_text():
    item = _item()
    assert social.apply_xiaohongshu_content(item, _note(type='video'), author_id=AUTHOR) is True
    assert item['completeness'] == 'metadata' and item['content_text'] == ''
    assert item['summary'] == _note()['body']


@pytest.mark.parametrize('changes', [{'external_id': 'other'}, {'author_id': 'another-author'}])
def test_note_identity_must_match_subscription_before_mutation(changes):
    item = _item()
    original = deepcopy(item)
    with pytest.raises(ContentAccessError, match='author_identity_mismatch'):
        social.apply_xiaohongshu_content(item, _note(**changes), author_id=AUTHOR)
    assert item == original


def test_dom_excerpt_stays_partial_and_title_alone_is_rejected():
    item = _item()
    social.apply_xiaohongshu_content(item, _note(body_complete=False, image_count=0), author_id=AUTHOR)
    assert item['completeness'] == 'partial'
    assert 'browser_content_extent_unverified' in item['quality_issues']
    assert 'visual_content_not_extracted' not in item['quality_issues']
    with pytest.raises(ContentAccessError, match='browser_content_unavailable'):
        social.apply_xiaohongshu_content(_item(), _note(body='正文标题'), author_id=AUTHOR)


def test_discovery_uses_native_href_in_memory_but_returns_no_navigation_token(monkeypatch):
    opened = []
    author_url = f'https://www.xiaohongshu.com/user/profile/{AUTHOR}'
    native_url = URL + '?xsec_token=ephemeral-token&xsec_source=pc_user'
    class Page:
        def __init__(self, url, **kwargs):
            self.url = url
            opened.append(url)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait_for(self, *args, **kwargs):
            return True
        def evaluate(self, expression):
            if expression == social._XHS_AUTHOR_JS:
                return {'url': author_url, 'author': '新闻晨报', 'items': [
                    {'title': '正文标题', 'source_url': native_url}
                ]}
            return _note()
    monkeypatch.setattr(social, 'BrowserPage', Page)
    items = social.discover_social({'platform': 'xiaohongshu', 'url': author_url}, enabled=True)
    assert opened == [author_url, native_url]
    assert items[0]['completeness'] == 'full'
    assert 'ephemeral-token' not in str(items)


def test_discovery_failure_preserves_metadata_and_never_exports_raw_exception(monkeypatch):
    author_url = f'https://www.xiaohongshu.com/user/profile/{AUTHOR}'
    class Page:
        def __init__(self, url, **kwargs):
            self.url = url
        def __enter__(self):
            if self.url != author_url:
                raise RuntimeError('private-response-token')
            return self
        def __exit__(self, *args):
            pass
        def wait_for(self, *args, **kwargs):
            return True
        def evaluate(self, expression):
            return {'url': author_url, 'items': [{'title': '正文标题', 'source_url': URL}]}
    monkeypatch.setattr(social, 'BrowserPage', Page)
    items = social.discover_social({'platform': 'xiaohongshu', 'url': author_url}, enabled=True)
    assert items[0]['completeness'] == 'metadata'
    assert 'content_fetch_failed' in items[0]['quality_issues']
    assert 'private-response-token' not in str(items)


def test_douyin_media_requires_exact_page_author_native_identity_and_not_ad():
    item = {'external_id': '7686727666610883903', 'title': '原题', 'summary': '原摘要'}
    snapshot = {'url': 'https://www.douyin.com/video/' + item['external_id'],
                'author_ids': ['author-id'], 'video_ready': True}
    metadata = {'video_id': item['external_id'], 'author_id': 'author-id', 'author': '原作者',
                'description': '真实作品描述', 'published_timestamp': 1790682257, 'is_ads': False}
    social.apply_douyin_content(item, snapshot, metadata, author_id='author-id')
    assert item['title'] == '真实作品描述'
    assert item['published_at'] == '2026-09-29T11:44:17+00:00'
    for patch in ({'video_id': 'another'}, {'author_id': 'other'}, {'is_ads': True}, {'is_ads': None}):
        before = deepcopy(item)
        with pytest.raises(ContentAccessError):
            social.apply_douyin_content(item, snapshot, {**metadata, **patch}, author_id='author-id')
        assert item == before
    for patch in ({'url': 'https://www.douyin.com/video/other'}, {'author_ids': []}, {'video_ready': False}):
        with pytest.raises(ContentAccessError):
            social.apply_douyin_content(item, {**snapshot, **patch}, metadata, author_id='author-id')


@pytest.mark.parametrize('url', ['https://www.xiaohongshu.com/explore/another', 'https://evil.invalid/' + NOTE])
def test_note_url_must_still_point_to_verified_work(url):
    with pytest.raises(ContentAccessError, match='author_identity_mismatch'):
        social.apply_xiaohongshu_content(_item(), _note(url=url), author_id=AUTHOR)


@pytest.mark.parametrize('path', [f'/explore/{NOTE}', f'/search_result/{NOTE}', f'/user/profile/{AUTHOR}/{NOTE}'])
def test_xhs_source_identity_is_canonical_across_native_entry_routes(path):
    item = social.parse_author_snapshot('xiaohongshu', {'items': [{'title': '正文',
        'source_url': 'https://www.xiaohongshu.com' + path + '?xsec_token=ephemeral'}]})[0]
    assert item['source_url'] == f'https://www.xiaohongshu.com/explore/{NOTE}'


def test_douyin_discovery_rejects_recommendation_before_media_handler(monkeypatch):
    author_url = 'https://www.douyin.com/user/expected'
    video_id = '7686727666610883903'
    calls = []
    class Page:
        def __init__(self, url, **kwargs):
            self.url = url
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait_for(self, *args, **kwargs):
            return True
        def evaluate(self, expression):
            if expression == social._DOUYIN_AUTHOR_JS:
                return {'url': author_url, 'items': [{'title': '真实作者作品',
                    'source_url': 'https://www.douyin.com/video/' + video_id}]}
            return {'url': self.url, 'author_ids': ['recommended-author'], 'video_ready': True}
        def douyin_video_metadata(self):
            return {'video_id': video_id, 'author_id': 'recommended-author', 'is_ads': False}
    monkeypatch.setattr(social, 'BrowserPage', Page)
    items = social.discover_social({'platform': 'douyin', 'url': author_url},
        enabled=True, media_handler=lambda page, item: calls.append(item))
    assert calls == []
    assert 'author_identity_mismatch' in items[0]['quality_issues']
    assert items[0]['completeness'] == 'metadata'


def test_topic_labels_are_not_a_substantive_full_article():
    item = _item()
    tags = '#第一财经有看投[话题]# #有看投app[话题]# #投资有看头[话题]# #第一财经[话题]#'
    assert len(tags) >= 40
    with pytest.raises(ContentAccessError, match='browser_content_unavailable'):
        social.apply_xiaohongshu_content(item, _note(body=tags), author_id=AUTHOR)
    assert item['content_text'] == '' and item['completeness'] == 'metadata'
