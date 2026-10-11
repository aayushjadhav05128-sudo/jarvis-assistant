"""
Agent Core Module for Jarvis
Coordinates the event-driven state machine:
[Listener: Double-Clap / Wake-Word] -> [Wake Chime] -> [VAD Audio Capture] ->
[Faster-Whisper STT] -> [Ollama Qwen2.5 Brain / Tool Loop] -> [TTS Voice Response].
"""

import sys
import os
import time
import json
import logging
import threading
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple

import requests

try:
    import ollama
    HAVE_OLLAMA_LIB = True
except ImportError:
    HAVE_OLLAMA_LIB = False

from config import CONFIG
from audio_listener import AudioListener, WakeTriggerType
from stt_engine import get_transcriber, SpeechRecorder, ChimePlayer
from tools import TOOL_DEFINITIONS, dispatch_tool
from tts_engine import get_tts

# Safe Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("Jarvis.Core")


class AgentState(Enum):
    IDLE_LISTENING = "idle_listening"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    REASONING = "reasoning"
    EXECUTING_TOOLS = "executing_tools"
    SPEAKING = "speaking"
    SHUTDOWN = "shutdown"


class LLMBrain:
    """
    Handles conversational reasoning and structured JSON Function Calling.
    Leverages the official Ollama library or HTTP endpoint for Qwen2.5:7b-instruct,
    with automatic tool execution and synthesis loop.
    """

    def __init__(self):
        self.cfg = CONFIG.llm
        self.conversation_history: List[Dict[str, Any]] = [
            {"role": "system", "content": self.cfg.system_prompt}
        ]

    def _call_ollama(self, messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Invokes local Ollama via native Python client or HTTP endpoint with tool definitions."""
        if HAVE_OLLAMA_LIB:
            try:
                response = ollama.chat(
                    model=self.cfg.model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    options={
                        "temperature": self.cfg.temperature,
                        "num_predict": self.cfg.max_tokens
                    }
                )
                msg = response.get("message") or response.message
                # Normalize tool_calls format if needed
                tool_calls = getattr(msg, "tool_calls", None) or (msg.get("tool_calls") if isinstance(msg, dict) else None)
                content = getattr(msg, "content", "") or (msg.get("content") if isinstance(msg, dict) else "")
                
                normalized_calls = []
                if tool_calls:
                    for tc in tool_calls:
                        func = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else {})
                        fname = getattr(func, "name", None) or (func.get("name") if isinstance(func, dict) else "")
                        fargs = getattr(func, "arguments", None) or (func.get("arguments") if isinstance(func, dict) else {})
                        normalized_calls.append({
                            "function": {
                                "name": fname,
                                "arguments": fargs
                            }
                        })

                return {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": normalized_calls if normalized_calls else None
                }
            except Exception as e:
                logger.debug(f"Ollama library call exception: {e}")

        # Fallback to direct HTTP endpoint
        url = f"{self.cfg.ollama_base_url}/api/chat"
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
            "stream": False,
            "options": {
                "temperature": self.cfg.temperature,
                "num_predict": self.cfg.max_tokens
            }
        }

        try:
            resp = requests.post(url, json=payload, timeout=25.0)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("message")
            else:
                logger.warning(f"Ollama HTTP returned status {resp.status_code}")
                return None
        except Exception as e:
            logger.debug(f"Error communicating with Ollama HTTP: {e}")
            return None

    def _offline_fallback(self, prompt: str) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
        """Fast local pattern matcher when LLM server is temporarily offline."""
        lower = prompt.lower().strip()

        if any(k in lower for k in ["battery", "power level", "battery status", "charge"]):
            return None, [{"function": {"name": "system_control", "arguments": {"action": "get_battery"}}}]

        if any(k in lower for k in ["system stats", "cpu", "ram", "memory usage", "telemetry"]):
            return None, [{"function": {"name": "system_control", "arguments": {"action": "get_system_stats"}}}]

        if "volume up" in lower or "louder" in lower or "increase volume" in lower:
            return None, [{"function": {"name": "system_control", "arguments": {"action": "volume_up", "value": 5}}}]
        if "volume down" in lower or "quieter" in lower or "decrease volume" in lower:
            return None, [{"function": {"name": "system_control", "arguments": {"action": "volume_down", "value": 5}}}]
        if "mute" in lower or "unmute" in lower:
            return None, [{"function": {"name": "system_control", "arguments": {"action": "volume_mute"}}}]

        if "lock" in lower and ("screen" in lower or "workstation" in lower or "pc" in lower or "laptop" in lower):
            return None, [{"function": {"name": "system_control", "arguments": {"action": "lock_screen"}}}]

        for app in ["vscode", "chrome", "terminal", "powershell", "notepad", "calculator", "calc", "spotify", "explorer"]:
            if f"open {app}" in lower or f"launch {app}" in lower or f"start {app}" in lower:
                return None, [{"function": {"name": "launch_application", "arguments": {"app_name": app}}}]

        if lower.startswith("search for ") or lower.startswith("google ") or lower.startswith("search "):
            query = lower.replace("search for ", "").replace("google ", "").replace("search ", "").strip()
            return None, [{"function": {"name": "browser_task", "arguments": {"action": "search", "query": query}}}]

        if any(k in lower for k in ["hello", "hey", "hi", "good morning", "good afternoon", "jarvis"]):
            return "Good day, sir. Systems are online and standing by for your instructions.", None

        return "I heard your request, sir, but could not contact the reasoning engine.", None

    def process_prompt(self, user_prompt: str) -> str:
        """Executes reasoning and tool dispatch loop."""
        logger.info(f"User Prompt: \"{user_prompt}\"")
        self.conversation_history.append({"role": "user", "content": user_prompt})

        # Cap conversation context to last 10 messages
        if len(self.conversation_history) > 12:
            self.conversation_history = [self.conversation_history[0]] + self.conversation_history[-10:]

        assistant_message = self._call_ollama(self.conversation_history)

        if not assistant_message:
            fallback_text, fallback_tools = self._offline_fallback(user_prompt)
            if fallback_tools:
                results_text = []
                for tool_call in fallback_tools:
                    fname = tool_call["function"]["name"]
                    fargs = tool_call["function"]["arguments"]
                    res = dispatch_tool(fname, fargs)
                    res_val = res.get("result") or res.get("error") or str(res)
                    results_text.append(res_val)
                final_answer = " ".join(results_text)
                self.conversation_history.append({"role": "assistant", "content": final_answer})
                return final_answer
            elif fallback_text:
                self.conversation_history.append({"role": "assistant", "content": fallback_text})
                return fallback_text
            else:
                return "I am unable to process that at this moment, sir."

        tool_calls = assistant_message.get("tool_calls")
        content = assistant_message.get("content") or ""

        if tool_calls:
            logger.info(f"Qwen generated {len(tool_calls)} tool call(s).")
            self.conversation_history.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls
            })

            # Execute tools
            for tc in tool_calls:
                func_info = tc.get("function", {})
                name = func_info.get("name")
                args_raw = func_info.get("arguments", {})

                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        args = {}
                else:
                    args = args_raw

                logger.info(f"Executing Tool: {name}({args})")
                tool_result = dispatch_tool(name, args)

                self.conversation_history.append({
                    "role": "tool",
                    "content": json.dumps(tool_result),
                    "name": name
                })

            # Ask Qwen to summarize results into a natural spoken reply
            follow_up = self._call_ollama(self.conversation_history)
            if follow_up and follow_up.get("content"):
                final_text = follow_up.get("content")
            else:
                final_text = "Action completed successfully, sir."

            self.conversation_history.append({"role": "assistant", "content": final_text})
            return final_text

        self.conversation_history.append({"role": "assistant", "content": content})
        return content


class JarvisAssistant:
    """Main Assistant Controller."""

    def __init__(self):
        self.state = AgentState.IDLE_LISTENING
        self.listener = AudioListener()
        self.recorder = SpeechRecorder(self.listener)
        self.transcriber = get_transcriber()
        self.brain = LLMBrain()
        self.tts = get_tts()
        self._running = False

    def start(self):
        self._running = True
        self.listener.start()

        print("\n" + "=" * 60)
        print(" ★ J.A.R.V.I.S. Production System Online ★")
        print("  - Wake Triggers: [Double-Clap] or ['Hey Jarvis']")
        print("  - Local STT: faster-whisper (in-memory INT8)")
        print("  - LLM Brain: Ollama (qwen2.5:7b-instruct) with Windows Tools")
        print("  - Local Voice: Windows SAPI5 (zero-latency)")
        print("=" * 60 + "\n")

        self.tts.speak("Jarvis is online and ready for your command, sir.")

        try:
            while self._running:
                self.state = AgentState.IDLE_LISTENING
                logger.info("Standing by... [Double-Clap or say 'Hey Jarvis']")

                wake_trigger = self.listener.wait_for_wake(timeout=1.0)
                if not wake_trigger:
                    continue

                logger.info(f"Wake trigger received: {wake_trigger.value.upper()}")

                self.state = AgentState.RECORDING
                pcm_audio, has_speech = self.recorder.record_until_silence()

                if not has_speech or not pcm_audio:
                    logger.info("No speech captured. Returning to standby.")
                    continue

                self.state = AgentState.TRANSCRIBING
                user_text = self.transcriber.transcribe_audio_buffer(pcm_audio)
                if not user_text:
                    logger.info("Transcription empty. Returning to standby.")
                    continue

                print(f"\n[USER]: {user_text}")

                self.state = AgentState.REASONING
                response_text = self.brain.process_prompt(user_text)

                print(f"[JARVIS]: {response_text}\n")

                self.state = AgentState.SPEAKING
                self.tts.speak(response_text, wait=True)

        except KeyboardInterrupt:
            print("\nShutting down Jarvis gracefully...")
        finally:
            self.stop()

    def stop(self):
        self._running = False
        self.state = AgentState.SHUTDOWN
        self.listener.stop()
        self.tts.stop()
        logger.info("Jarvis subsystems terminated.")


if __name__ == "__main__":
    assistant = JarvisAssistant()
    assistant.start()
