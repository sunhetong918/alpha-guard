from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import wave

import pytest

from research.local_whisper import transcribe_local


def audio_fixture(path: Path, seconds: int = 1) -> None:
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\x00\x00' * 16000 * seconds)


def test_missing_local_model_fails_before_loading_or_network(tmp_path):
    audio = tmp_path / 'test.wav'
    audio_fixture(audio)
    with pytest.raises(ValueError, match='local_model_missing'):
        transcribe_local(audio, model_file=tmp_path / 'missing.pt', ffmpeg='/usr/bin/false',
                         output_dir=tmp_path / 'out',
                         model_loader=lambda *_: pytest.fail('model must not load'))


def test_real_ffmpeg_decoding_writes_whisper_compatible_json(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('local FFmpeg not installed')
    audio, model = tmp_path / 'test.wav', tmp_path / 'tiny.pt'
    audio_fixture(audio)
    model.write_bytes(b'fixture-model-not-loaded')
    calls = []
    def transcribe(array, **kwargs):
        calls.append((len(array), kwargs))
        return {'text': 'A transcription fixture.', 'segments': [
            {'start': 0.0, 'end': 1.0, 'text': 'A transcription fixture.'}], 'language': 'en'}
    output = transcribe_local(audio, model_file=model, ffmpeg=ffmpeg,
                              output_dir=tmp_path / 'out', language='en',
                              model_loader=lambda *_: SimpleNamespace(transcribe=transcribe))
    assert calls == [(16000, {'language': 'en', 'task': 'transcribe', 'fp16': False,
                             'verbose': False, 'condition_on_previous_text': False,
                             'temperature': 0.0})]
    assert json.loads(output.read_text())['segments'][0]['end'] == 1.0
    assert not output.with_suffix('.json.part').exists()


def test_duration_overflow_is_rejected_instead_of_silently_truncated(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('local FFmpeg not installed')
    audio, model = tmp_path / 'test.wav', tmp_path / 'tiny.pt'
    audio_fixture(audio, seconds=2)
    model.write_bytes(b'fixture')
    with pytest.raises(ValueError, match='audio_duration_limit'):
        transcribe_local(audio, model_file=model, ffmpeg=ffmpeg, output_dir=tmp_path / 'out',
                         max_seconds=1, model_loader=lambda *_: pytest.fail('model must not load'))
