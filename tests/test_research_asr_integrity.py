from __future__ import annotations

import json
import sys

import pytest

from research.ingest import ingest_text, transcribe_media
from research.media_transcription import CapturedMedia, transcribe_captured_media
from research.store import ResearchStore


@pytest.mark.parametrize('field', ['text', 'segments'])
def test_local_asr_rejects_replacement_characters_in_root_or_segment_text(tmp_path, field):
    media = tmp_path / 'speech.wav'
    media.write_bytes(b'input read only by configured fixture CLI')
    output = {'text': 'Clean speech.', 'segments': [{'start': 0, 'end': 1, 'text': 'Clean speech.'}]}
    if field == 'text':
        output['text'] = 'Corrupt \ufffd speech.'
    else:
        output['segments'][0]['text'] = 'Corrupt \ufffd speech.'
    script = ('import pathlib,sys; pathlib.Path(sys.argv[2],"transcript.json").write_text('
              + repr(json.dumps(output)) + ',encoding="utf-8")')
    result = transcribe_media(media, command=[sys.executable, '-c', script, '{input}', '{output_dir}'])
    assert result == {'status': 'failed', 'detail': 'invalid_transcription_output'}


def source():
    return {'platform': 'douyin', 'external_id': '7686727666610883903',
            'source_url': 'https://www.douyin.com/video/7686727666610883903',
            'title': 'Source title', 'author': 'Source author', 'published_at': None,
            'content_kind': 'metadata', 'completeness': 'metadata'}


def result(text='Original complete speech requiring human verification.'):
    return {'status': 'success', 'item': {'content_text': text, 'segments': [
        {'start_seconds': 0, 'end_seconds': 1, 'text': text, 'speaker': ''}]}}


@pytest.mark.parametrize('cached', [False, True])
def test_captured_asr_and_raw_media_cache_reject_corrupt_decoding(tmp_path, cached):
    media = tmp_path / 'audio.webm'
    media.write_bytes(b'local media fixture')
    item = source()
    capture = CapturedMedia(media, item['source_url'], item['external_id'], True, 1)
    command = ['asr', '{input}', '{output_dir}']
    cache = {}
    if cached:
        initial = transcribe_captured_media(capture, item, command=command, cache=cache,
            probe=lambda *a, **k: 1, transcriber=lambda *a, **k: result())
        assert initial['status'] == 'success'
        entry = next(iter(cache.values()))
        entry['content_text'] = entry['segments'][0]['text'] = 'Corrupt \ufffd speech.'
    checked = transcribe_captured_media(capture, item, command=command, cache=cache,
        probe=lambda *a, **k: 1, transcriber=lambda *a, **k: result('Corrupt \ufffd speech.'))
    assert checked == {'status': 'failed', 'detail': 'invalid_transcription_output'}


def test_old_corrupt_local_transcript_is_not_reused_from_sqlite(tmp_path):
    item = {**source(), **result('Corrupt \ufffd speech.')['item'],
            'content_kind': 'transcript', 'completeness': 'full',
            'quality_issues': ['machine_transcription_unverified', 'local_media_transcription']}
    with ResearchStore(tmp_path / 'state.db') as store:
        saved = store.upsert_evidence(item)
        assert '\ufffd' in saved.content_text
        assert store.latest_reusable_transcript(source()) is None
        assert store.get_evidence(saved.id).content_text == saved.content_text


def test_explicit_human_text_and_subtitle_imports_preserve_replacement_characters():
    original = 'The source intentionally discusses the Unicode character \ufffd.'
    assert ingest_text(original)['content_text'] == original
    subtitles = '1\n00:00:00,000 --> 00:00:01,000\n' + original
    assert ingest_text(subtitles, 'srt')['content_text'] == original
