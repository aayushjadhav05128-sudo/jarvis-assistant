"""
Low-Latency Text-to-Speech (TTS) Module for Jarvis on Windows
Features:
1. Native Windows SAPI5 via pyttsx3 (zero-network, sub-10ms start latency).
2. Non-blocking asynchronous worker thread with speech queue.
3. Spoken Text Sanitizer to strip markdown, code blocks, and URLs for natural voice delivery.
4. Thread-safe COM initialization for Windows multithreading.
"""

import re
import queue
import threading
import logging
from typing import Optional

import pyttsx3

try:
    import pythoncom
    HAVE_PYTHONCOM = True
except ImportError:
    HAVE_PYTHONCOM = False

from config import CONFIG, TTSConfig

logger = logging.getLogger("Jarvis.TTS")


class SpokenTextSanitizer:
    """Prepares raw LLM text generation for speech synthesis."""

    @staticmethod
    def sanitize(text: str) -> str:
        if not text:
            return ""

        # Remove code blocks
        clean = re.sub(r"```[\s\S]*?```", " Code snippet omitted. ", text)
        clean = re.sub(r"`([^`]+)`", r"\1", clean)

        # Remove markdown headers (#, ##, ###)
        clean = re.sub(r"^#{1,6}\s+", "", clean, flags=re.MULTILINE)

        # Remove markdown bold/italic (**, *, __, _)
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
        clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
        clean = re.sub(r"__([^_]+)__", r"\1", clean)
        clean = re.sub(r"_([^_]+)_", r"\1", clean)

        # Remove markdown links [text](url) -> text
        clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)

        # Remove raw URLs
        clean = re.sub(r"https?://\S+", "web link", clean)

        # Clean bullet lists (- Item, * Item, 1. Item)
        clean = re.sub(r"^\s*[-*•]\s+", "", clean, flags=re.MULTILINE)
        clean = re.sub(r"^\s*\d+\.\s+", "", clean, flags=re.MULTILINE)

        # Remove quote markers (>)
        clean = re.sub(r"^\s*>\s*", "", clean, flags=re.MULTILINE)

        # Collapse whitespace and newlines
        clean = re.sub(r"\s+", " ", clean).strip()

        return clean


class TTSEngine:
    """
    Non-blocking TTS Engine managing Windows SAPI5 voice output
    via dedicated background thread.
    """

    def __init__(self, cfg: Optional[TTSConfig] = None):
        self.cfg = cfg or CONFIG.tts
        self._speech_queue: queue.Queue = queue.Queue()
        self._is_running = True
        self._is_speaking = False
        self._worker_thread = threading.Thread(target=self._speech_worker, daemon=True)
        self._worker_thread.start()

    def _speech_worker(self):
        """Worker loop initializing SAPI5 inside its dedicated COM apartment."""
        if HAVE_PYTHONCOM:
            pythoncom.CoInitialize()

        engine = None
        try:
            engine = pyttsx3.init(driverName="sapi5")
            engine.setProperty("rate", self.cfg.rate)
            engine.setProperty("volume", self.cfg.volume)

            # Match voice preference (e.g. David, Zira, Mark)
            voices = engine.getProperty("voices")
            preferred_voice_id = None
            for v in voices:
                if self.cfg.voice_preference.lower() in v.name.lower():
                    preferred_voice_id = v.id
                    break

            if preferred_voice_id:
                engine.setProperty("voice", preferred_voice_id)
                logger.info(f"Selected TTS voice: {preferred_voice_id}")
            elif voices:
                engine.setProperty("voice", voices[0].id)
                logger.info(f"Defaulted to TTS voice: {voices[0].name}")

        except Exception as e:
            logger.error(f"Failed to initialize pyttsx3 engine: {e}")

        while self._is_running:
            try:
                item = self._speech_queue.get(timeout=0.2)
                if item is None:  # Shutdown signal
                    break

                text, done_event = item
                if engine and text:
                    self._is_speaking = True
                    logger.info(f"Jarvis speaking: \"{text}\"")
                    try:
                        engine.say(text)
                        engine.runAndWait()
                    except Exception as e:
                        logger.error(f"Error during SAPI5 playback: {e}")
                    finally:
                        self._is_speaking = False

                if done_event:
                    done_event.set()
                self._speech_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"TTS Worker error: {e}")

        if HAVE_PYTHONCOM:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def speak(self, text: str, wait: bool = False):
        """
        Submits text to be spoken asynchronously or synchronously.
        Automatically sanitizes markdown and technical noise.
        """
        sanitized = SpokenTextSanitizer.sanitize(text)
        if not sanitized:
            return

        done_event = threading.Event() if wait else None
        self._speech_queue.put((sanitized, done_event))

        if wait and done_event:
            done_event.wait()

    def is_speaking(self) -> bool:
        """Returns True if voice synthesis is actively playing audio."""
        return self._is_speaking

    def stop(self):
        """Stops the TTS worker thread."""
        self._is_running = False
        self._speech_queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)


# Global singleton instance
_tts_instance: Optional[TTSEngine] = None


def get_tts() -> TTSEngine:
    global _tts_instance
    if _tts_instance is None:
        _tts_instance = TTSEngine()
    return _tts_instance


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tts = get_tts()
    print("Testing Jarvis TTS voice output...")
    tts.speak("Good afternoon. Systems are operational and ready for your command.", wait=True)
