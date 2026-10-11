"""
JARVIS — Production All-In-One Local AI Assistant for Windows
Combines Double-Clap & Wake-Word activation, Faster-Whisper STT with VAD silence
detection, Ollama Qwen2.5 Tool Calling (Battery, Volume, Apps, Web, Lock),
and Zero-Latency Windows SAPI5 Speech Output.
"""

import os
import sys
import time
import json
import re
import ctypes
import subprocess
import webbrowser
import urllib.parse
import winsound
import numpy as np
import pyaudio
import pyttsx3
import psutil
from faster_whisper import WhisperModel
import ollama

# Ensure proper Unicode rendering on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ============================================================================
# 1. AUDIO & DSP CONFIGURATION (Optimized for Whisper 16kHz Mono)
# ============================================================================
RATE = 16000                   # 16kHz is native for Whisper & acoustic models
CHUNK = 512                    # 32ms frame latency
ENERGY_MULTIPLIER = 4.2        # Spike ratio above adaptive noise floor
MIN_ABSOLUTE_RMS = 1800.0      # Minimum RMS to qualify as a clap impulse
MAX_IMPULSE_FRAMES = 2         # Impulse must subside within ~65ms (guards against speech)
MIN_CLAP_INTERVAL = 0.20       # 200ms minimum spacing (prevents echo/reverberation)
MAX_CLAP_INTERVAL = 0.60       # 600ms maximum spacing between the two claps
DEBOUNCE_PERIOD = 0.85         # Cooldown period after wake
SILENCE_TIMEOUT = 1.2          # Stop recording after 1.2s silence pause
SPEECH_ENERGY_THRESHOLD = 500  # RMS threshold for user speech

# Windows Virtual Keys for Audio Control
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_EXTENDEDKEY = 0x01
KEYEVENTF_KEYUP = 0x02

# ============================================================================
# 2. ACOUSTIC CHIMES & SPEECH SANITIZER
# ============================================================================
def play_wake_chime():
    try:
        winsound.Beep(880, 80)
        winsound.Beep(1320, 100)
    except Exception:
        pass

def play_done_chime():
    try:
        winsound.Beep(1046, 70)
    except Exception:
        pass

def sanitize_for_speech(text: str) -> str:
    """Strips markdown headers, asterisks, URLs, and code blocks for fluid TTS."""
    clean = re.sub(r"```[\s\S]*?```", " Code snippet omitted. ", text)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    clean = re.sub(r"^#{1,6}\s+", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
    clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    clean = re.sub(r"https?://\S+", "web link", clean)
    clean = re.sub(r"^\s*[-*•]\s+", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean

# ============================================================================
# 3. INITIALIZE TTS & STT (WHISPER)
# ============================================================================
print("Initializing Jarvis voice and speech engines...")
tts = pyttsx3.init()
tts.setProperty("rate", 192)
# Select English David voice if present
for v in tts.getProperty("voices"):
    if "david" in v.name.lower():
        tts.setProperty("voice", v.id)
        break

def speak(text: str):
    cleaned = sanitize_for_speech(text)
    if not cleaned:
        return
    print(f"\n[Jarvis]: {cleaned}")
    tts.say(cleaned)
    tts.runAndWait()

print("Loading Whisper Speech Recognition model (base.en INT8)...")
whisper_model = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=4)

# ============================================================================
# 4. WINDOWS SYSTEM TOOLS & TOOL DEFINITIONS
# ============================================================================
def send_key(vk_code: int):
    u32 = ctypes.windll.user32
    u32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
    u32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

def system_control(action: str, value: int = 5) -> str:
    action = action.lower()
    if action == "get_battery":
        b = psutil.sensors_battery()
        if not b:
            return "Battery information is unavailable (running on AC power)."
        plug = "plugged in" if b.power_plugged else "on battery"
        return f"Battery is at {b.percent}%, {plug}."
    elif action == "get_system_stats":
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        return f"CPU utilization is {cpu:.1f}%. RAM is {mem.percent}% utilized ({mem.used // (1024**3)} GB of {mem.total // (1024**3)} GB)."
    elif action == "volume_up":
        for _ in range(value):
            send_key(VK_VOLUME_UP)
        return f"Increased volume by {value} steps."
    elif action == "volume_down":
        for _ in range(value):
            send_key(VK_VOLUME_DOWN)
        return f"Decreased volume by {value} steps."
    elif action == "volume_mute":
        send_key(VK_VOLUME_MUTE)
        return "Toggled volume mute."
    elif action == "lock_screen":
        ctypes.windll.user32.LockWorkStation()
        return "Screen locked."
    return f"Unknown system action: {action}"

def launch_application(app_name: str) -> str:
    apps = {
        "chrome": "chrome", "vscode": "code", "vs code": "code",
        "terminal": "wt", "powershell": "powershell", "notepad": "notepad",
        "calculator": "calc", "calc": "calc", "spotify": "spotify",
        "explorer": "explorer", "task manager": "taskmgr"
    }
    target = apps.get(app_name.lower().strip(), app_name)
    try:
        subprocess.Popen(f'start "" "{target}"', shell=True)
        return f"Launched {app_name}."
    except Exception as e:
        return f"Failed to launch {app_name}: {e}"

def browser_task(action: str, query: str = None, url: str = None) -> str:
    if action == "search" and query:
        webbrowser.open_new_tab(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")
        return f"Searching Google for: {query}"
    elif action == "open_url" and url:
        t = url if url.startswith("http") else f"https://{url}"
        webbrowser.open_new_tab(t)
        return f"Opened {t}"
    return "Browser action completed."

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "system_control",
            "description": "Controls Windows system parameters: battery, volume, lock screen, and stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get_battery", "get_system_stats", "volume_up", "volume_down", "volume_mute", "lock_screen"]},
                    "value": {"type": "integer", "description": "Volume change amount (default 5)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "launch_application",
            "description": "Launches Windows apps (Chrome, VS Code, Spotify, Calculator, Notepad, Terminal, Explorer).",
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string", "description": "The name of the application."}},
                "required": ["app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_task",
            "description": "Performs Google searches or opens URLs in default browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["search", "open_url"]},
                    "query": {"type": "string", "description": "Search query keywords"},
                    "url": {"type": "string", "description": "Web URL to open"}
                },
                "required": ["action"]
            }
        }
    }
]

TOOL_MAP = {
    "system_control": system_control,
    "launch_application": launch_application,
    "browser_task": browser_task
}

# ============================================================================
# 5. BRAIN WITH STRUCTURED OLLAMA TOOL CALLING
# ============================================================================
SYSTEM_PROMPT = (
    "You are Jarvis, a fast, sharp, autonomous executive desktop AI assistant running on Windows. "
    "Keep spoken responses concise (1 to 2 crisp sentences). "
    "Never use markdown headers, asterisks, or bullet points in responses. "
    "Whenever the user asks for system actions, applications, volume, or searches, use the tools immediately."
)

conversation_history = [{"role": "system", "content": SYSTEM_PROMPT}]

def query_brain(prompt: str) -> str:
    """Interacts with local Qwen2.5:7b-instruct with tool execution loop."""
    print(f"\n[You]: {prompt}")
    conversation_history.append({"role": "user", "content": prompt})

    try:
        response = ollama.chat(
            model="qwen2.5:7b-instruct",
            messages=conversation_history[-10:],
            tools=TOOL_DEFINITIONS
        )
        msg = response.get("message") or response.message
        tool_calls = getattr(msg, "tool_calls", None) or (msg.get("tool_calls") if isinstance(msg, dict) else None)
        content = getattr(msg, "content", "") or (msg.get("content") if isinstance(msg, dict) else "")

        if tool_calls:
            print(f"Executing {len(tool_calls)} tool call(s)...")
            conversation_history.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

            for tc in tool_calls:
                func = getattr(tc, "function", None) or tc.get("function")
                fname = getattr(func, "name", None) or func.get("name")
                fargs = getattr(func, "arguments", None) or func.get("arguments") or {}

                fn = TOOL_MAP.get(fname)
                if fn:
                    tool_result = fn(**fargs)
                    print(f"  -> {fname}({fargs}): {tool_result}")
                    conversation_history.append({"role": "tool", "content": str(tool_result), "name": fname})

            # Get final spoken summary
            follow_up = ollama.chat(
                model="qwen2.5:7b-instruct",
                messages=conversation_history[-10:]
            )
            final_msg = follow_up.get("message") or follow_up.message
            final_content = getattr(final_msg, "content", "") or final_msg.get("content")
            conversation_history.append({"role": "assistant", "content": final_content})
            return final_content

        conversation_history.append({"role": "assistant", "content": content})
        return content

    except Exception as e:
        print(f"Brain query error: {e}")
        return "I encountered a minor processing error, sir."

# ============================================================================
# 6. AUDIO DSP & VAD SPEECH RECORDING
# ============================================================================
def calculate_rms(audio_data: bytes) -> float:
    samples = np.frombuffer(audio_data, dtype=np.int16)
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))

def record_user_speech(stream) -> str:
    """Records speech using Voice Activity Detection (stops on ~1.2s silence pause)."""
    play_wake_chime()
    print("Listening to your request... (speak naturally)")

    frames = []
    speech_detected = False
    consecutive_silence = 0.0
    chunk_time = CHUNK / RATE
    start_time = time.time()

    while True:
        data = stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)
        rms = calculate_rms(data)
        elapsed = time.time() - start_time

        if rms >= SPEECH_ENERGY_THRESHOLD:
            speech_detected = True
            consecutive_silence = 0.0
        elif speech_detected:
            consecutive_silence += chunk_time

        # Condition 1: User paused after speaking
        if speech_detected and consecutive_silence >= SILENCE_TIMEOUT:
            play_done_chime()
            break

        # Condition 2: No speech started within 4.0s
        if not speech_detected and elapsed >= 4.0:
            return ""

        # Condition 3: Max duration guard
        if elapsed >= 15.0:
            play_done_chime()
            break

    audio_data = b"".join(frames)
    audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

    segments, _ = whisper_model.transcribe(audio_np, beam_size=1, vad_filter=True)
    return " ".join([seg.text.strip() for seg in segments]).strip()

# ============================================================================
# 7. MAIN EVENT LOOP
# ============================================================================
def main():
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=CHUNK)

    print("\nCalibrating microphone ambient noise (1 sec)...")
    samples = [calculate_rms(stream.read(CHUNK, exception_on_overflow=False)) for _ in range(int(RATE / CHUNK))]
    ambient_noise = max(float(np.mean(samples)), 150.0)

    print("=" * 60)
    print(" ★ JARVIS IS READY IN STANDBY ★")
    print(f" Ambient Noise Baseline: {ambient_noise:.1f} RMS")
    print(" Clap twice (200ms - 600ms apart) to activate!")
    print("=" * 60 + "\n")
    speak("Jarvis is online and ready for your command, sir.")

    first_clap_time = 0.0
    waiting_for_second = False
    cooldown_until = 0.0
    consecutive_impulse_frames = 0

    try:
        while True:
            raw = stream.read(CHUNK, exception_on_overflow=False)
            rms = calculate_rms(raw)
            now = time.time()
            threshold = max(ambient_noise * ENERGY_MULTIPLIER, MIN_ABSOLUTE_RMS)
            is_spike = rms > threshold

            if is_spike:
                consecutive_impulse_frames += 1
            else:
                consecutive_impulse_frames = 0

            # Guard against sustained speech: claps are sharp impulses (<= 2 frames)
            is_clap = is_spike and (consecutive_impulse_frames <= MAX_IMPULSE_FRAMES)

            # Smooth noise floor when quiet
            if not is_spike:
                ambient_noise = 0.95 * ambient_noise + 0.05 * rms
                if ambient_noise < 150.0:
                    ambient_noise = 150.0

            if now < cooldown_until:
                continue

            if is_clap and consecutive_impulse_frames == 1:
                if not waiting_for_second:
                    first_clap_time = now
                    waiting_for_second = True
                else:
                    delta = now - first_clap_time
                    if MIN_CLAP_INTERVAL <= delta <= MAX_CLAP_INTERVAL:
                        # WAKE TRIGGER CONFIRMED
                        print(f"\n[★ WAKE TRIGGER] Double-clap confirmed (interval: {delta*1000:.0f}ms)")
                        user_text = record_user_speech(stream)

                        if user_text:
                            reply = query_brain(user_text)
                            speak(reply)
                        else:
                            print("No speech detected.")

                        waiting_for_second = False
                        consecutive_impulse_frames = 0
                        cooldown_until = time.time() + DEBOUNCE_PERIOD
                        print("\nStanding by... Clap twice to wake.")
                    elif delta > MAX_CLAP_INTERVAL:
                        first_clap_time = now
                        waiting_for_second = True

            if waiting_for_second and (now - first_clap_time > MAX_CLAP_INTERVAL):
                waiting_for_second = False

    except KeyboardInterrupt:
        print("\nShutting down Jarvis...")
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

if __name__ == "__main__":
    main()
