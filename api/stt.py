"""로컬 STT — faster-whisper 기반.

브라우저 MediaRecorder가 webm/opus(또는 Safari의 mp4)로 통째 녹음한 오디오를
한 번에 받아 텍스트로 변환한다. webkitSpeechRecognition 실시간 dictation을
대체하기 위함 (실시간 인식이 환경에 따라 정확도가 낮은 문제 해결).

모델은 프로세스당 한 번만 로드 (lazy + lock). 기본은 'base.en' — Mac mini CPU
환경에서 정확도/속도 균형. WHISPER_MODEL 환경변수로 override.
  - tiny.en (~40MB)  : 가장 빠름, 정확도 낮음
  - base.en (~150MB) : 권장 균형 (기본값)
  - small.en (~500MB): 정확도 좋음, 약간 느림
  - medium.en (~1.5GB): 매우 정확, 느림
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()
_load_failed_reason: str = ''


def _model_name() -> str:
    return os.environ.get('WHISPER_MODEL', 'base.en').strip() or 'base.en'


def _device_and_compute():
    """Mac mini CPU 가정. int8이 메모리/속도 균형이 가장 좋다."""
    return 'cpu', 'int8'


def get_model():
    """프로세스당 단 한 번 WhisperModel 로드. 실패 사유는 모듈 전역에 캐시."""
    global _model, _load_failed_reason
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            _load_failed_reason = (
                'faster-whisper 미설치. `pip install -r requirements.txt` 실행 필요. '
                f'({e})'
            )
            raise RuntimeError(_load_failed_reason)
        name = _model_name()
        device, compute = _device_and_compute()
        logger.info(f'[stt] WhisperModel 로드 시작: {name} ({device}/{compute})')
        try:
            _model = WhisperModel(name, device=device, compute_type=compute)
            logger.info(f'[stt] WhisperModel 로드 완료: {name}')
        except Exception as e:
            _load_failed_reason = f'WhisperModel 로드 실패 ({name}): {e}'
            logger.exception('[stt] WhisperModel 로드 실패')
            raise
    return _model


def transcribe_file(path: str, language: str = 'en') -> dict:
    """파일 경로 받아서 한 번에 변환. {text, duration, language} 반환."""
    model = get_model()
    segments, info = model.transcribe(
        path,
        language=language,
        vad_filter=True,            # 무음 구간 잘라서 hallucination 줄임
        beam_size=5,
        condition_on_previous_text=False,
    )
    parts = []
    for seg in segments:
        t = (seg.text or '').strip()
        if t:
            parts.append(t)
    text = ' '.join(parts).strip()
    return {
        'text': text,
        'duration': float(info.duration) if info and info.duration else 0.0,
        'language': info.language if info else language,
    }


def last_load_error() -> str:
    return _load_failed_reason
