"""
Speech-to-Text (STT) Module for Jarvis
Primary engine: faster-whisper with in-memory float32 audio tensors and INT8 quantization.
Secondary fallback: SpeechRecognition for resilient execution if Windows Application
Control or DLL policies restrict PyAV/torch/ctranslate2.
"""

import io
import math
import time
import logging
import winsound
import wave
from typing import Optional, Tuple, List

import numpy as np

from config import CONFIG, STTConfig

logger = logging.getLogger("Jarvis.STTEngine")


class ChimePlayer:
    """Zero-latency acoustic cues using native Windows winsound."""

    @staticmethod
    def play_wake_chime():
        """Crisp two-tone upward chime indicating Jarvis is listening."""
        try:
            winsound.Beep(880, 80)    # A5 note
            winsound.Beep(1320, 100)  # E6 note
        except Exception as e:
            logger.debug(f"Chime error: {e}")

    @staticmethod
    def play_done_chime():
        """Subtle acknowledgment chime when speech recording concludes."""
        try:
            winsound.Beep(1046, 70)   # C6 note
        except Exception:
            pass

    @staticmethod
    def play_cancel_chime():
        """Soft downward tone indicating listening timed out without speech."""
        try:
            winsound.Beep(659, 80)
            winsound.Beep(440, 120)
        except Exception:
            pass


def _check_cuda() -> bool:
    """Safely checks CUDA availability without crashing on Windows DLL blocks."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


class FasterWhisperTranscriber:
    """
    STT Engine leveraging faster-whisper when possible, with automated fallback
    to SpeechRecognition if local DLL or Application Control policies restrict PyAV.
    """

    def __init__(self, cfg: Optional[STTConfig] = None):
        self.cfg = cfg or CONFIG.stt
        self.model = None
        self.sr_recognizer = None
        self._init_speech_recognition()
        self._init_faster_whisper()

    def _init_speech_recognition(self):
        try:
            import speech_recognition as sr
            self.sr_recognizer = sr.Recognizer()
        except Exception as e:
            logger.debug(f"SpeechRecognition not available: {e}")

    def _init_faster_whisper(self):
        """Initializes WhisperModel with optimal device and quantization."""
        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            logger.info(f"faster-whisper unavailable ({e}). Using SpeechRecognition backend.")
            return

        device = self.cfg.device
        compute_type = self.cfg.compute_type

        if device == "auto":
            device = "cuda" if _check_cuda() else "cpu"

        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"

        try:
            logger.info(
                f"Loading faster-whisper model '{self.cfg.model_size}' on device={device} (compute_type={compute_type})..."
            )
            start_t = time.perf_counter()
            self.model = WhisperModel(
                self.cfg.model_size,
                device=device,
                compute_type=compute_type,
                cpu_threads=4,
                num_workers=1
            )
            elapsed = time.perf_counter() - start_t
            logger.info(f"WhisperModel loaded in {elapsed:.2f}s.")
        except Exception as e:
            logger.warning(f"Could not load faster-whisper ({e}). Reverting to SpeechRecognition backend.")
            self.model = None

    def transcribe_audio_buffer(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """
        Transcribes raw 16-bit mono PCM bytes in-memory without disk I/O.
        """
        if not pcm_bytes or len(pcm_bytes) < (sample_rate * 2 * 0.3):  # Less than 0.3s
            return ""

        # Primary: Faster-Whisper
        if self.model is not None:
            try:
                audio_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0

                start_t = time.perf_counter()
                segments, info = self.model.transcribe(
                    audio_float32,
                    beam_size=self.cfg.beam_size,
                    language="en",
                    vad_filter=self.cfg.vad_filter,
                    vad_parameters=dict(min_silence_duration_ms=400)
                )

                transcription = " ".join(seg.text.strip() for seg in segments).strip()
                elapsed = time.perf_counter() - start_t
                logger.info(f"Faster-Whisper ({elapsed:.2f}s): \"{transcription}\"")
                return transcription
            except Exception as e:
                logger.warning(f"Faster-Whisper runtime error: {e}")

        # Secondary Fallback: SpeechRecognition
        if self.sr_recognizer is not None:
            try:
                import speech_recognition as sr
                audio_data = sr.AudioData(pcm_bytes, sample_rate, 2)
                start_t = time.perf_counter()
                text = self.sr_recognizer.recognize_google(audio_data, language="en-US")
                elapsed = time.perf_counter() - start_t
                logger.info(f"SpeechRecognition ({elapsed:.2f}s): \"{text}\"")
                return text.strip()
            except Exception as e:
                logger.debug(f"SpeechRecognition result: {e}")
                return ""

        return ""


class SpeechRecorder:
    """
    Captures live microphone audio after a wake trigger, detects speech onset,
    and terminates recording once a ~1.2s silence pause is detected.
    """

    def __init__(self, audio_listener, cfg: Optional[STTConfig] = None):
        self.listener = audio_listener
        self.cfg = cfg or CONFIG.stt

    def record_until_silence(self) -> Tuple[bytes, bool]:
        """
        Listens to user speech.
        Returns:
            (pcm_audio_bytes, has_speech_occurred)
        """
        ChimePlayer.play_wake_chime()
        logger.info("Listening for user speech...")

        chunk_size = self.listener.audio_cfg.chunk_size
        sample_rate = self.listener.audio_cfg.sample_rate
        chunk_duration_sec = chunk_size / sample_rate

        frames: List[bytes] = []
        speech_detected = False
        consecutive_silence_time = 0.0
        start_time = time.time()
        max_duration = self.cfg.max_speech_duration_sec
        silence_timeout = self.cfg.silence_timeout_sec
        energy_threshold = self.cfg.speech_energy_threshold

        while True:
            chunk = self.listener.read_chunk()
            if not chunk:
                time.sleep(0.01)
                continue

            frames.append(chunk)
            rms = self.listener.clap_detector.calculate_rms(chunk)
            elapsed_total = time.time() - start_time

            if rms >= energy_threshold:
                if not speech_detected:
                    logger.debug(f"Speech onset detected (RMS={rms:.1f})")
                speech_detected = True
                consecutive_silence_time = 0.0
            else:
                if speech_detected:
                    consecutive_silence_time += chunk_duration_sec

            # Condition 1: Speech finished (1.2s silence detected after speech onset)
            if speech_detected and consecutive_silence_time >= silence_timeout:
                logger.info(f"Speech completed. Silence duration: {consecutive_silence_time:.2f}s")
                ChimePlayer.play_done_chime()
                break

            # Condition 2: No speech ever started within 4.5 seconds
            if not speech_detected and elapsed_total >= 4.5:
                logger.info("Listening timed out with no speech detected.")
                ChimePlayer.play_cancel_chime()
                return b"", False

            # Condition 3: Exceeded safety maximum duration
            if elapsed_total >= max_duration:
                logger.warning("Reached maximum recording limit.")
                ChimePlayer.play_done_chime()
                break

        full_pcm = b"".join(frames)
        return full_pcm, speech_detected


# Singleton STT engine instance
_transcriber_instance: Optional[FasterWhisperTranscriber] = None


def get_transcriber() -> FasterWhisperTranscriber:
    global _transcriber_instance
    if _transcriber_instance is None:
        _transcriber_instance = FasterWhisperTranscriber()
    return _transcriber_instance
