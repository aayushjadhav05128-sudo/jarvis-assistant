"""
Jarvis System Diagnostics and Subsystem Verification Tool
Runs end-to-end unit tests on Audio, TTS, Windows Tools, and Whisper STT.
"""

import sys
import os
import time
import winsound
import logging
import numpy as np

# Prevent Windows console charmap encoding issues
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Jarvis.Diagnostics")


def test_winsound_chimes():
    print("\n[1/5] Testing Windows Acoustic Chimes (winsound)...")
    try:
        from stt_engine import ChimePlayer
        print("  Playing wake chime...")
        ChimePlayer.play_wake_chime()
        time.sleep(0.2)
        print("  Playing done chime...")
        ChimePlayer.play_done_chime()
        print("  [PASS] Chimes functional.")
        return True
    except Exception as e:
        print(f"  [FAIL] Chime test failed: {e}")
        return False


def test_system_tools():
    print("\n[2/5] Testing Windows System Tools & Telemetry...")
    try:
        from tools import system_control, launch_application, dispatch_tool

        batt = system_control("get_battery")
        print(f"  Battery: {batt.get('result')}")

        stats = system_control("get_system_stats")
        print(f"  System stats: {stats.get('result')}")

        print("  [PASS] System tools verified.")
        return True
    except Exception as e:
        print(f"  [FAIL] System tools failed: {e}")
        return False


def test_tts_engine():
    print("\n[3/5] Testing Low-Latency Windows SAPI5 TTS Engine...")
    try:
        from tts_engine import get_tts
        tts = get_tts()
        print("  Speaking test greeting...")
        tts.speak("Jarvis diagnostic test complete. Audio channels verified.", wait=True)
        print("  [PASS] TTS engine verified.")
        return True
    except Exception as e:
        print(f"  [FAIL] TTS engine failed: {e}")
        return False


def test_whisper_engine():
    print("\n[4/5] Testing STT Model Loading & Transcription Pipeline...")
    try:
        from stt_engine import get_transcriber
        transcriber = get_transcriber()
        # Test synthetic silent buffer
        silence = np.zeros(16000, dtype=np.int16).tobytes()
        res = transcriber.transcribe_audio_buffer(silence)
        print(f"  In-memory inference pipeline operational (test output: '{res}')")
        print("  [PASS] STT pipeline verified.")
        return True
    except Exception as e:
        print(f"  [FAIL] STT engine test failed: {e}")
        return False


def test_audio_stream_and_claps():
    print("\n[5/5] Testing PyAudio Microphone Stream & RMS Energy Meter...")
    try:
        from audio_listener import AudioListener
        listener = AudioListener()
        listener.start()
        print("  Streaming live microphone for 3 seconds. Real-time RMS monitor:")

        start = time.time()
        while time.time() - start < 3.0:
            chunk = listener.read_chunk()
            rms = listener.clap_detector.calculate_rms(chunk)
            floor = listener.clap_detector.noise_tracker.floor
            bars = "=" * min(30, int(rms / 120))
            print(f"\r  RMS: {rms:6.1f} | Floor: {floor:5.1f} | [{bars:<30}]", end="", flush=True)
            time.sleep(0.04)

        listener.stop()
        print("\n  [PASS] PyAudio input stream verified.")
        return True
    except Exception as e:
        print(f"\n  [FAIL] Audio stream failed: {e}")
        return False


def main():
    print("=" * 60)
    print("       JARVIS HARDWARE & SUBSYSTEM DIAGNOSTIC SUITE")
    print("=" * 60)

    t1 = test_winsound_chimes()
    t2 = test_system_tools()
    t3 = test_tts_engine()
    t4 = test_whisper_engine()
    t5 = test_audio_stream_and_claps()

    print("\n" + "=" * 60)
    print(f"Summary: Chimes={t1}, Tools={t2}, TTS={t3}, STT={t4}, Audio={t5}")
    if all([t1, t2, t3, t4, t5]):
        print(" [ALL SUBSYSTEMS OPERATIONAL AND PRODUCTION-READY]")
    else:
        print(" [WARNING] Some subsystems reported issues. Review details above.")
    print("=" * 60)


if __name__ == "__main__":
    main()
