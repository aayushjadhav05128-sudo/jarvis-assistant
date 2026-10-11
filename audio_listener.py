"""
Audio Listener Module for Jarvis
Implements real-time PyAudio acoustic processing with:
1. Adaptive Double-Clap Detector (dynamic noise floor, transient impulse duration check, 200-600ms window).
2. Wake-word listener ("Hey Jarvis" / "Jarvis").
3. Thread-safe continuous stream processing.
"""

import math
import struct
import time
import logging
import threading
from collections import deque
from enum import Enum
from typing import Optional, Tuple, Callable

import numpy as np
import pyaudio

try:
    import speech_recognition as sr
    HAVE_SR = True
except ImportError:
    HAVE_SR = False

from config import CONFIG, AudioConfig, ClapDetectorConfig, WakeWordConfig

logger = logging.getLogger("Jarvis.AudioListener")


class WakeTriggerType(Enum):
    DOUBLE_CLAP = "double_clap"
    WAKE_WORD = "wake_word"
    MANUAL = "manual"


class AdaptiveNoiseFloor:
    """Tracks ambient acoustic background noise using Exponential Moving Average (EMA)."""

    def __init__(self, initial_floor: float = 400.0, alpha: float = 0.05, min_floor: float = 150.0):
        self.floor = initial_floor
        self.alpha = alpha
        self.min_floor = min_floor

    def update(self, rms: float, is_candidate_impulse: bool = False) -> float:
        # Avoid corrupting the noise floor with impulsive peaks or claps
        if not is_candidate_impulse:
            # Asymmetrical update: adapt slower upward to loud ambient bursts, faster downward
            effective_alpha = self.alpha if rms > self.floor else self.alpha * 1.5
            self.floor = (1.0 - effective_alpha) * self.floor + effective_alpha * rms
            if self.floor < self.min_floor:
                self.floor = self.min_floor
        return self.floor


class DoubleClapDetector:
    """
    Acoustic impulse and timing analyzer for detecting clean double-claps.
    Clap physics:
    - High energy spike (RMS >> dynamic noise floor).
    - Rapid decay (impulse duration <= 65ms, unlike speech/music).
    - Timing: 2nd clap must occur between 200ms and 600ms after the 1st clap.
    """

    def __init__(self, clap_cfg: ClapDetectorConfig, sample_rate: int, chunk_size: int):
        self.cfg = clap_cfg
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.chunk_duration_sec = chunk_size / sample_rate

        self.noise_tracker = AdaptiveNoiseFloor(
            initial_floor=400.0,
            alpha=self.cfg.noise_floor_alpha,
            min_floor=200.0
        )

        # State tracking
        self.first_clap_time: Optional[float] = None
        self.last_trigger_time: float = 0.0
        self.high_energy_consecutive_frames: int = 0
        self.max_impulse_frames = max(1, int(self.cfg.max_impulse_duration_ms / (self.chunk_duration_sec * 1000)))

    def calculate_rms(self, pcm_bytes: bytes) -> float:
        """Calculates Root Mean Square (RMS) energy from 16-bit mono PCM."""
        if not pcm_bytes:
            return 0.0
        # Use numpy for vectorized high-speed computation
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if len(samples) == 0:
            return 0.0
        sum_squares = np.sum(samples.astype(np.float64) ** 2)
        return float(np.sqrt(sum_squares / len(samples)))

    def process_chunk(self, pcm_bytes: bytes, current_time: float) -> Tuple[bool, float, float]:
        """
        Processes a single audio chunk.
        Returns:
            (is_double_clap, current_rms, noise_floor)
        """
        # Debounce enforcement
        if (current_time - self.last_trigger_time) < self.cfg.debounce_sec:
            rms = self.calculate_rms(pcm_bytes)
            return False, rms, self.noise_tracker.floor

        rms = self.calculate_rms(pcm_bytes)
        threshold = max(self.cfg.min_clap_rms, self.noise_tracker.floor * self.cfg.spike_multiplier)
        is_spike = rms >= threshold

        # Track impulse width to discard sustained speech vowels or sustained clapping
        if is_spike:
            self.high_energy_consecutive_frames += 1
        else:
            self.high_energy_consecutive_frames = 0

        # Impulse qualification: must spike, but NOT remain sustained across too many frames
        is_valid_impulse = is_spike and (self.high_energy_consecutive_frames <= self.max_impulse_frames)

        # Update noise floor (ignoring candidate impulses)
        noise_floor = self.noise_tracker.update(rms, is_candidate_impulse=is_spike)

        # State machine for double clap
        if self.first_clap_time is None:
            if is_valid_impulse and self.high_energy_consecutive_frames == 1:
                self.first_clap_time = current_time
                logger.debug(f"[ClapDetector] First clap candidate detected (RMS={rms:.1f}, Floor={noise_floor:.1f})")
            return False, rms, noise_floor

        # We have recorded a first clap; evaluate timing for the second clap
        delta_t = current_time - self.first_clap_time

        # If too fast (< 200ms), it's reverberation/ringing from the first clap
        if delta_t < self.cfg.min_clap_interval_sec:
            return False, rms, noise_floor

        # Inside the valid double-clap window (200ms - 600ms)
        if self.cfg.min_clap_interval_sec <= delta_t <= self.cfg.max_clap_interval_sec:
            if is_valid_impulse and self.high_energy_consecutive_frames == 1:
                logger.info(
                    f"★ Double-Clap Triggered! (Interval: {delta_t * 1000:.1f}ms, "
                    f"RMS: {rms:.1f}, Noise Floor: {noise_floor:.1f})"
                )
                self.first_clap_time = None
                self.last_trigger_time = current_time
                return True, rms, noise_floor
            return False, rms, noise_floor

        # Window expired (> 600ms) without a second clap
        if delta_t > self.cfg.max_clap_interval_sec:
            self.first_clap_time = None

        return False, rms, noise_floor


class WakeWordDetector:
    """
    Lightweight wake-word detector operating over rolling PCM window.
    Supports SpeechRecognition audio chunk analysis for hotwords 'Hey Jarvis' / 'Jarvis'.
    """

    def __init__(self, wake_cfg: WakeWordConfig, audio_cfg: AudioConfig):
        self.cfg = wake_cfg
        self.audio_cfg = audio_cfg
        self.keywords = [kw.lower() for kw in self.cfg.keywords]
        self.sr_recognizer = sr.Recognizer() if HAVE_SR else None
        if self.sr_recognizer:
            self.sr_recognizer.energy_threshold = 400
            self.sr_recognizer.dynamic_energy_threshold = False

        self.last_check_time = 0.0
        self.check_interval = 1.0  # Check every 1.0s if speech energy detected

    def check_wake_word(self, pcm_buffer: bytes) -> bool:
        if not HAVE_SR or not self.cfg.enabled or not pcm_buffer:
            return False

        try:
            audio_data = sr.AudioData(
                pcm_buffer,
                self.audio_cfg.sample_rate,
                self.audio_cfg.format_bytes
            )
            # Use fast built-in Google recognizer or Sphinx if available
            text = self.sr_recognizer.recognize_google(audio_data, language="en-US").lower()
            logger.debug(f"[WakeWord] Recognized background snippet: '{text}'")
            for kw in self.keywords:
                if kw in text:
                    logger.info(f"★ Wake-Word Triggered! Detected keyword: '{kw}'")
                    return True
        except sr.UnknownValueError:
            pass  # Normal: background noise was not speech
        except Exception as e:
            logger.debug(f"[WakeWord] Detection exception: {e}")

        return False


class AudioListener:
    """
    High-performance audio streaming controller using PyAudio.
    Runs continuous background acquisition with thread-safe ring buffers
    and parallel evaluation of Double-Clap and Wake-Word triggers.
    """

    def __init__(self):
        self.audio_cfg = CONFIG.audio
        self.clap_cfg = CONFIG.clap
        self.wake_cfg = CONFIG.wake_word

        self.clap_detector = DoubleClapDetector(
            self.clap_cfg,
            self.audio_cfg.sample_rate,
            self.audio_cfg.chunk_size
        )
        self.wake_detector = WakeWordDetector(self.wake_cfg, self.audio_cfg)

        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream: Optional[pyaudio.Stream] = None
        self._is_running = False
        self._worker_thread: Optional[threading.Thread] = None

        # Thread synchronization
        self._wake_event = threading.Event()
        self._wake_trigger_type: Optional[WakeTriggerType] = None
        self._lock = threading.Lock()

        # Rolling ring buffer for wake word and pre-roll audio (holds ~2.5 seconds)
        max_buffer_chunks = int(2.5 * self.audio_cfg.sample_rate / self.audio_cfg.chunk_size)
        self._recent_chunks: deque = deque(maxlen=max_buffer_chunks)

    def start(self):
        """Initializes PyAudio and starts the background listening thread."""
        with self._lock:
            if self._is_running:
                return

            self._pa = pyaudio.PyAudio()
            input_device = self.audio_cfg.device_index

            try:
                self._stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=self.audio_cfg.channels,
                    rate=self.audio_cfg.sample_rate,
                    input=True,
                    frames_per_buffer=self.audio_cfg.chunk_size,
                    input_device_index=input_device
                )
            except Exception as e:
                logger.error(f"Failed to open PyAudio stream: {e}")
                raise

            self._is_running = True
            self._wake_event.clear()
            self._worker_thread = threading.Thread(target=self._audio_loop, daemon=True)
            self._worker_thread.start()
            logger.info("AudioListener initialized and streaming on background thread.")

    def stop(self):
        """Gracefully halts audio streaming and releases resources."""
        with self._lock:
            self._is_running = False
            if self._stream:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

            if self._pa:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        logger.info("AudioListener stopped.")

    def wait_for_wake(self, timeout: Optional[float] = None) -> Optional[WakeTriggerType]:
        """
        Blocks until either Double-Clap or Wake-Word triggers, or timeout elapses.
        Returns the WakeTriggerType or None if timed out.
        """
        self._wake_event.clear()
        self._wake_trigger_type = None

        triggered = self._wake_event.wait(timeout=timeout)
        if triggered:
            trigger = self._wake_trigger_type
            self._wake_event.clear()
            return trigger
        return None

    def trigger_manual_wake(self):
        """Allows software/hotkey/console activation of the wake state."""
        self._wake_trigger_type = WakeTriggerType.MANUAL
        self._wake_event.set()

    def read_chunk(self) -> bytes:
        """Reads a synchronous chunk of audio from the live stream."""
        if not self._stream or not self._is_running:
            return b""
        try:
            return self._stream.read(self.audio_cfg.chunk_size, exception_on_overflow=False)
        except Exception as e:
            logger.debug(f"Audio read warning: {e}")
            return b""

    def _audio_loop(self):
        """Internal audio loop consuming live PCM frames."""
        last_wake_check = time.time()

        while self._is_running:
            try:
                if not self._stream:
                    break

                chunk_bytes = self._stream.read(self.audio_cfg.chunk_size, exception_on_overflow=False)
                if not chunk_bytes:
                    time.sleep(0.01)
                    continue

                now = time.time()
                self._recent_chunks.append(chunk_bytes)

                # 1. Evaluate Double-Clap
                is_clap, rms, floor = self.clap_detector.process_chunk(chunk_bytes, now)
                if is_clap:
                    self._wake_trigger_type = WakeTriggerType.DOUBLE_CLAP
                    self._wake_event.set()
                    # Sleep slightly to prevent immediate re-trigger
                    time.sleep(0.2)
                    continue

                # 2. Evaluate Wake-Word if enabled and speech energy is detected
                if self.wake_cfg.enabled and (now - last_wake_check) >= 1.2:
                    if rms > (floor * 1.8) and rms > 600.0:
                        last_wake_check = now
                        # Check in background or fast slice
                        full_buffer = b"".join(self._recent_chunks)
                        if self.wake_detector.check_wake_word(full_buffer):
                            self._wake_trigger_type = WakeTriggerType.WAKE_WORD
                            self._wake_event.set()
                            time.sleep(0.2)

            except Exception as e:
                logger.error(f"Error in AudioListener loop: {e}", exc_info=True)
                time.sleep(0.05)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("Testing Double-Clap & Audio Listener... Clap twice to test!")
    listener = AudioListener()
    listener.start()
    try:
        while True:
            trigger = listener.wait_for_wake(timeout=10.0)
            if trigger:
                print(f"\n>>> WAKE EVENT DETECTED: {trigger.value.upper()} <<<\n")
            else:
                print(".", end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping listener...")
    finally:
        listener.stop()
