from __future__ import annotations

import base64
import re

import pytest

from research.browser_media import capture_audio
from research.network import ContentAccessError


class Page:
    def __init__(self, *, receipt=None, metadata=None, data=b'audio-data', waiting=False):
        self.data = data
        self.metadata = {'duration_seconds': 31.672, 'capture_supported': True, 'protected_media': False, **(metadata or {})}
        self.receipt = {'detail': '', 'ended': True, 'expected_duration_seconds': 31.672,
            'start_seconds': 0, 'max_playhead_seconds': 31.672, 'observed_duration_seconds': 31.672,
            'elapsed_seconds': 31.7, 'byte_count': len(data), 'mime_type': 'audio/webm;codecs=opus', **(receipt or {})}
        self.waiting = waiting
        self.calls = []
        self.extended = []
        self.cleaned = False
        self.corrupt = False
    def wait_for(self, expression, **kwargs):
        self.calls.append(expression)
        return self.metadata
    def extend_for_media_seconds(self, value):
        self.extended.append(value)
    def evaluate(self, expression):
        self.calls.append(expression)
        if 'alpha_guard_audio_clear' in expression:
            self.cleaned = True
            return True
        if 'alpha_guard_audio_start' in expression:
            return True
        if 'alpha_guard_audio_poll' in expression:
            return None if self.waiting else self.receipt
        if 'alpha_guard_audio_read' in expression:
            offset = int(re.search(r'\.slice\((\d+),', expression).group(1))
            chunk = self.data[offset:offset + 200_000]
            return {'base64': 'not base64!' if self.corrupt else base64.b64encode(chunk).decode(),
                    'byte_count': len(chunk), 'total': len(self.data)}
        raise AssertionError('unexpected browser operation')


def test_complete_capture_reads_bounded_chunks_and_never_uses_other_media_devices(tmp_path):
    page = Page(data=b'captured-audio' * 35_000)
    output = tmp_path / 'voice.webm'
    receipt = capture_audio(page, output)
    assert receipt['capture_complete'] is True and receipt['ended'] is True
    assert output.read_bytes() == page.data and page.cleaned
    assert page.extended == [61.672]
    reads = [call for call in page.calls if 'alpha_guard_audio_read' in call]
    assert len(reads) == 3
    script = '\n'.join(page.calls)
    assert 'captureStream' in script and 'audioBitsPerSecond:64000' in script
    assert 'getUserMedia' not in script and 'getDisplayMedia' not in script and 'fetch(' not in script


@pytest.mark.parametrize('change', [
    {'ended': False}, {'start_seconds': .2}, {'max_playhead_seconds': 25},
    {'elapsed_seconds': 10}, {'detail': 'media_source_changed'}, {'detail': 'media_playback_changed'},
    {'observed_duration_seconds': 1000},
])
def test_incomplete_changed_or_accelerated_playback_never_produces_source_file(tmp_path, change):
    page = Page(receipt=change)
    output = tmp_path / 'voice.webm'
    result = capture_audio(page, output)
    assert result['capture_complete'] is False and not output.exists()
    assert page.cleaned


@pytest.mark.parametrize('metadata,detail', [
    ({'duration_seconds': 601}, 'media_duration_budget_exhausted'),
    ({'capture_supported': False}, 'media_capture_unavailable'),
    ({'protected_media': True}, 'media_capture_protected'),
])
def test_unsupported_or_over_budget_media_stops_before_capture(tmp_path, metadata, detail):
    page = Page(metadata=metadata)
    result = capture_audio(page, tmp_path / 'voice.webm')
    assert result['detail'] == detail and result['capture_complete'] is False
    assert page.extended == []
    assert not any('alpha_guard_audio_start' in value for value in page.calls)


def test_cancellation_is_checked_during_native_playback_and_cleans_page(tmp_path):
    page = Page(waiting=True)
    def cancelled():
        return any('alpha_guard_audio_poll' in value for value in page.calls)
    with pytest.raises(ContentAccessError, match='^cancelled$'):
        capture_audio(page, tmp_path / 'voice.webm', cancelled=cancelled)
    assert page.cleaned and not list(tmp_path.iterdir())


def test_corrupt_audio_chunk_removes_only_its_own_partial_file(tmp_path):
    page = Page()
    page.corrupt = True
    output = tmp_path / 'voice.webm'
    other = tmp_path / 'other.txt'
    other.write_text('retain')
    with pytest.raises(ContentAccessError, match='media_capture_invalid_response'):
        capture_audio(page, output)
    assert not output.exists() and other.read_text() == 'retain' and page.cleaned


def test_existing_output_is_never_overwritten_or_deleted(tmp_path):
    page = Page()
    output = tmp_path / 'voice.webm'
    output.write_text('existing user file')
    with pytest.raises(FileExistsError):
        capture_audio(page, output)
    assert output.read_text() == 'existing user file' and page.cleaned


def test_receipt_never_reflects_raw_page_errors_or_media_urls(tmp_path):
    page = Page(receipt={'detail': 'https://signed.example/private-token',
                        'expected_duration_seconds': 'private-token', 'source_url': 'private-token'})
    result = capture_audio(page, tmp_path / 'voice.webm')
    assert result['capture_complete'] is False
    assert result['detail'] == 'media_capture_invalid_response'
    assert 'private-token' not in str(result)


@pytest.mark.parametrize('kind', ['element_disconnected', 'src_changed', 'duration_changed'])
def test_source_change_diagnostics_are_safe_enums_and_never_allow_full(tmp_path, kind):
    page = Page(receipt={'detail': 'media_source_changed', 'source_change_kind': kind,
                        'video_error_code': 3, 'ready_state': 0, 'network_state': 3,
                        'element_connected': True, 'video_error_message': 'https://signed.invalid/private'})
    result = capture_audio(page, tmp_path / 'voice.webm')
    assert result['capture_complete'] is False and result['source_change_kind'] == kind
    assert result['video_error_category'] == 'decode' and result['video_error_code'] == 3
    assert result['ready_state'] == 0 and result['network_state'] == 3
    assert 'private' not in str(result)


def test_arbitrary_diagnostic_values_are_not_reflected(tmp_path):
    result = capture_audio(Page(receipt={'source_change_kind': 'private-url',
        'ready_state': 'private-url', 'network_state': 20, 'element_connected': 'private-url',
        'video_error_code': True}), tmp_path / 'voice.webm')
    assert 'source_change_kind' not in result and 'video_error_code' not in result
    assert 'ready_state' not in result and 'network_state' not in result and 'element_connected' not in result
