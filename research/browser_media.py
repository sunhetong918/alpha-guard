"""Bounded audio capture from an already verified, normally playing owned tab.

No media URL, cookie, microphone, screen stream or platform API request leaves
this module. The caller owns the temporary output path and its deletion.
"""
from __future__ import annotations

import base64
from collections.abc import Callable
import json
import math
from pathlib import Path
import time
from typing import Any
import uuid

from .browser_source import BrowserPage
from .network import ContentAccessError, check_cancelled

_MAX_BYTES = 8_000_000
_CHUNK_BYTES = 200_000
_DETAILS = {"", "media_unavailable", "media_source_changed", "media_playback_changed",
    "media_audio_unavailable", "media_too_large", "media_capture_failed", "media_capture_timeout",
    "media_playback_unavailable", "media_capture_incomplete", "media_capture_security_error"}
_PREPARE = r"""/* alpha_guard_audio_prepare */ (() => {
 const videos=[...document.querySelectorAll('video')].filter(v=>v.getClientRects().length&&Number.isFinite(v.duration)&&v.duration>0&&v.readyState>=2);
 videos.sort((a,b)=>b.getBoundingClientRect().width*b.getBoundingClientRect().height-a.getBoundingClientRect().width*a.getBoundingClientRect().height);
 const video=videos[0];if(!video)return null;video.muted=true;
 const s={video,source:video.currentSrc,duration:video.duration};window[SLOT]=s;
 return {duration_seconds:s.duration,capture_supported:typeof video.captureStream==='function',protected_media:Boolean(video.mediaKeys)};
})()"""
_CAPTURE = r"""/* alpha_guard_audio_capture */ (async () => {
 const s=window[SLOT];if(!s)return {detail:'media_unavailable'};
 const v=s.video;let stream,audio,recorder,timer,deadline;
 const chunks=[];let size=0,ended=false,started=false,start=0,peak=0,last=0,detail='',sourceChange='',startedAt=0,stoppedAt=0;
 const fail=code=>{if(!detail)detail=code;if(recorder?.state==='recording')recorder.stop();};
 const same=()=>{const change=!v.isConnected?'element_disconnected':v.currentSrc!==s.source?'src_changed':!Number.isFinite(v.duration)||Math.abs(v.duration-s.duration)>=.1?'duration_changed':'';if(change&&!sourceChange)sourceChange=change;return !change;};
 const seekStart=async()=>{if(v.currentTime>.001){v.currentTime=0;await Promise.race([new Promise(r=>v.addEventListener('seeked',r,{once:true})),new Promise((_,j)=>setTimeout(()=>j(new Error('seek_timeout')),3000))]);}};
 const onSeek=()=>{if(started&&!ended)fail('media_playback_changed');};
 const onRate=()=>{if(started&&!ended&&Math.abs(v.playbackRate-1)>.001)fail('media_playback_changed');};
 const onEnd=()=>{if(!started||ended)return;ended=true;peak=Math.max(peak,v.currentTime);stoppedAt=performance.now();if(!same())detail='media_source_changed';if(recorder?.state==='recording')recorder.stop();};
 try {
  v.muted=true;v.pause();v.loop=false;v.playbackRate=1;await seekStart();
  if(!same())return {detail:'media_source_changed',source_change_kind:sourceChange};
  stream=v.captureStream();
  if(!stream.getAudioTracks().length){
   await Promise.race([v.play(),new Promise((_,j)=>setTimeout(()=>j(new Error('play_timeout')),5000))]);
   if(!stream.getAudioTracks().length)await Promise.race([new Promise(r=>stream.addEventListener('addtrack',r,{once:true})),new Promise(r=>setTimeout(r,2000))]);
   v.pause();await seekStart();
  }
  if(!stream.getAudioTracks().length)return {detail:'media_audio_unavailable'};
  if(!same())return {detail:'media_source_changed',source_change_kind:sourceChange};
  audio=new MediaStream(stream.getAudioTracks());
  const mime=MediaRecorder.isTypeSupported('audio/webm;codecs=opus')?'audio/webm;codecs=opus':'audio/webm';
  recorder=new MediaRecorder(audio,{mimeType:mime,audioBitsPerSecond:64000});s.recorder=recorder;
  recorder.ondataavailable=e=>{size+=e.data.size;if(size>MAX_BYTES){fail('media_too_large');return;}if(e.data.size)chunks.push(e.data);};
  recorder.onerror=()=>fail('media_capture_failed');
  const stopped=new Promise(r=>recorder.addEventListener('stop',r,{once:true}));
  v.addEventListener('ended',onEnd,true);v.addEventListener('seeking',onSeek);v.addEventListener('ratechange',onRate);
  start=v.currentTime;peak=start;last=start;startedAt=performance.now();recorder.start(500);started=true;
  timer=setInterval(()=>{if(ended)return;if(!same()){fail('media_source_changed');return;}
   if(Math.abs(v.playbackRate-1)>.001||v.currentTime+.05<last){fail('media_playback_changed');return;}
   peak=Math.max(peak,v.currentTime);last=v.currentTime;},40);
  deadline=setTimeout(()=>fail('media_capture_timeout'),(s.duration+10)*1000);
  try{await Promise.race([v.play(),new Promise((_,j)=>setTimeout(()=>j(new Error('play_timeout')),5000))]);}
  catch(_){fail('media_playback_unavailable');}
  await stopped;if(!stoppedAt)stoppedAt=performance.now();
  const blob=new Blob(chunks,{type:mime});s.blob=blob;
  return {detail:detail||(!ended?'media_capture_incomplete':''),ended,source_change_kind:sourceChange,
   video_error_code:v.error?.code||null,ready_state:v.readyState,network_state:v.networkState,element_connected:v.isConnected,
   expected_duration_seconds:s.duration,start_seconds:start,max_playhead_seconds:peak,
   observed_duration_seconds:Math.max(0,peak-start),elapsed_seconds:(stoppedAt-startedAt)/1000,
   byte_count:blob.size,mime_type:mime};
 }catch(e){return {detail:e.name==='SecurityError'?'media_capture_security_error':'media_capture_failed'};}
 finally{started=false;clearInterval(timer);clearTimeout(deadline);v.removeEventListener('ended',onEnd,true);v.removeEventListener('seeking',onSeek);v.removeEventListener('ratechange',onRate);v.pause();if(recorder?.state==='recording')recorder.stop();audio?.getTracks().forEach(t=>t.stop());stream?.getTracks().forEach(t=>t.stop());}
})()"""
_READ = r"""/* alpha_guard_audio_read */ (async () => {
 const b=window[SLOT]?.blob;if(!b||b.size>MAX_BYTES)return null;
 const data=new Uint8Array(await b.slice(OFFSET,OFFSET+CHUNK_BYTES).arrayBuffer());let binary='';
 for(let i=0;i<data.length;i+=16000)binary+=String.fromCharCode(...data.subarray(i,i+16000));
 return {base64:btoa(binary),byte_count:data.length,total:b.size};
})()"""
_CLEAR = "/* alpha_guard_audio_clear */ (()=>{const s=window[SLOT];if(s?.recorder?.state==='recording')s.recorder.stop();delete window[SLOT];return true;})()"
_POLL = "/* alpha_guard_audio_poll */ (()=>window[SLOT]?.receipt||null)()"


def _number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _complete(receipt: dict[str, Any], duration: float) -> bool:
    names = ('expected_duration_seconds', 'start_seconds', 'max_playhead_seconds',
             'observed_duration_seconds', 'elapsed_seconds')
    if not all(_number(receipt.get(name)) for name in names):
        return False
    return bool(receipt.get('ended') is True and not receipt.get('detail')
        and abs(receipt['expected_duration_seconds'] - duration) <= .01
        and 0 <= receipt['start_seconds'] <= min(.05, duration * .005)
        and duration - .1 <= receipt['max_playhead_seconds'] <= duration + .1
        and abs(receipt['observed_duration_seconds'] -
                (receipt['max_playhead_seconds'] - receipt['start_seconds'])) <= .01
        and receipt['observed_duration_seconds'] >= duration - min(.1, duration * .01)
        and receipt['elapsed_seconds'] >= duration - min(.25, duration * .025))


def capture_audio(page: BrowserPage, output_path: Path, *, max_duration_seconds: float = 600,
                  cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Record one complete natural playback, then read bounded audio chunks.

    The caller must verify the exact work and author before calling. It must
    close this owned BrowserPage on cancellation, which also ends page capture.
    No partial capture is written as a usable source recording.
    """
    if not _number(max_duration_seconds) or not 0 < max_duration_seconds <= 600:
        raise ValueError('invalid_media_capture_limit')
    check_cancelled(cancelled)
    slot = json.dumps('__alpha_guard_audio_' + uuid.uuid4().hex)
    def script(value: str) -> str:
        return value.replace('SLOT', slot).replace('MAX_BYTES', str(_MAX_BYTES))
    created = False
    try:
        metadata = page.wait_for(script(_PREPARE), timeout=12)
        if not isinstance(metadata, dict) or not _number(metadata.get('duration_seconds')):
            return {'capture_complete': False, 'detail': 'media_unavailable'}
        duration = float(metadata['duration_seconds'])
        if duration <= 0 or duration > max_duration_seconds:
            return {'capture_complete': False, 'detail': 'media_duration_budget_exhausted',
                    'expected_duration_seconds': duration}
        if metadata.get('protected_media'):
            return {'capture_complete': False, 'detail': 'media_capture_protected'}
        if metadata.get('capture_supported') is not True:
            return {'capture_complete': False, 'detail': 'media_capture_unavailable'}
        page.extend_for_media_seconds(duration + 30)
        check_cancelled(cancelled)
        # Start recording in the owned page, then poll short CDP reads so this
        # operation's own cancellation callback works throughout playback.
        page.evaluate("/* alpha_guard_audio_start */ (()=>{const task=" + script(_CAPTURE)
            + ";task.then(receipt=>{const s=window[" + slot + "];if(s)s.receipt=receipt;})"
            + ".catch(()=>{const s=window[" + slot + "];if(s)s.receipt={detail:'media_capture_failed'};});return true;})()")
        deadline = time.monotonic() + duration + 20
        while True:
            check_cancelled(cancelled)
            raw = page.evaluate(script(_POLL))
            if raw is not None:
                break
            if time.monotonic() >= deadline:
                return {'capture_complete': False, 'detail': 'media_capture_timeout',
                        'expected_duration_seconds': duration}
            time.sleep(.2)
        if not isinstance(raw, dict):
            raise ContentAccessError('media_capture_invalid_response')
        # The recording's currentSrc is never included in the metadata or file.
        receipt = {key: raw.get(key) for key in ('expected_duration_seconds',
            'start_seconds', 'max_playhead_seconds', 'observed_duration_seconds', 'elapsed_seconds',
            'byte_count') if _number(raw.get(key))}
        raw_detail = raw.get('detail', '')
        receipt['detail'] = raw_detail if isinstance(raw_detail, str) and raw_detail in _DETAILS else 'media_capture_invalid_response'
        receipt['ended'] = raw.get('ended') is True
        error_code = raw.get('video_error_code')
        if type(error_code) is int and error_code in {1, 2, 3, 4}:
            receipt['video_error_code'] = error_code
            receipt['video_error_category'] = {1: 'aborted', 2: 'network', 3: 'decode', 4: 'source_not_supported'}[error_code]
        for field, limit in (('ready_state', 4), ('network_state', 3)):
            if type(raw.get(field)) is int and 0 <= raw[field] <= limit:
                receipt[field] = raw[field]
        if type(raw.get('element_connected')) is bool:
            receipt['element_connected'] = raw['element_connected']
        if isinstance(raw.get('source_change_kind'), str) and raw['source_change_kind'] in {'element_disconnected', 'src_changed', 'duration_changed'}:
            receipt['source_change_kind'] = raw['source_change_kind']
        if raw.get('mime_type') in ('audio/webm', 'audio/webm;codecs=opus'):
            receipt['mime_type'] = raw['mime_type']
        complete = _complete(receipt, duration)
        receipt['capture_complete'] = complete
        if not complete:
            receipt['detail'] = receipt.get('detail') or 'media_capture_incomplete'
            return receipt
        size = receipt.get('byte_count')
        if type(size) is not int or not 0 < size <= _MAX_BYTES:
            raise ContentAccessError('media_capture_invalid_response')
        check_cancelled(cancelled)
        with Path(output_path).open('xb') as output:
            created = True
            offset = 0
            while offset < size:
                check_cancelled(cancelled)
                value = page.evaluate(script(_READ).replace('OFFSET', str(offset)).replace('CHUNK_BYTES', str(_CHUNK_BYTES)))
                if not isinstance(value, dict) or value.get('total') != size:
                    raise ContentAccessError('media_capture_invalid_response')
                data = value.get('base64')
                if not isinstance(data, str) or len(data) > _CHUNK_BYTES * 2:
                    raise ContentAccessError('media_capture_invalid_response')
                try:
                    chunk = base64.b64decode(data, validate=True)
                except (ValueError, UnicodeError):
                    raise ContentAccessError('media_capture_invalid_response') from None
                if len(chunk) != min(_CHUNK_BYTES, size - offset) or value.get('byte_count') != len(chunk):
                    raise ContentAccessError('media_capture_invalid_response')
                output.write(chunk)
                offset += len(chunk)
        receipt['detail'] = 'media_captured'
        return receipt
    except BaseException:
        if created:
            Path(output_path).unlink(missing_ok=True)
        raise
    finally:
        try:
            page.evaluate(script(_CLEAR))
        except Exception:
            # Cancellation makes CDP reads fail fast; the caller's owned-tab
            # context then closes it and releases any remaining page memory.
            pass
