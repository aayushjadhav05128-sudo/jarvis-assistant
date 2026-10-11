"""
Configuration Module for Jarvis System
Defines all operational thresholds, acoustic parameters, model configurations,
and system integration paths.
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from pathlib import Path


@dataclass
class AudioConfig:
    sample_rate: int = 16000          # 16kHz audio standard for speech & acoustic models
    channels: int = 1                 # Mono
    chunk_size: int = 512             # ~32ms latency per frame at 16kHz
    format_bytes: int = 2             # 16-bit PCM = 2 bytes per sample
    device_index: Optional[int] = None  # None selects default input device


@dataclass
class ClapDetectorConfig:
    # Energy thresholds
    min_clap_rms: float = 2200.0       # Minimum absolute RMS to qualify as a candidate clap
    noise_floor_alpha: float = 0.05    # Exponential moving average smoothing for noise floor
    spike_multiplier: float = 4.2      # RMS must exceed (noise_floor * spike_multiplier)
    
    # Impulse duration guard (claps are sharp, speech is sustained)
    max_impulse_duration_ms: float = 65.0  # Clap spike must subside within ~65ms
    
    # Dual-clap interval constraints
    min_clap_interval_sec: float = 0.20    # 200ms minimum gap to prevent reverb echoes
    max_clap_interval_sec: float = 0.60    # 600ms maximum gap between the two claps
    debounce_sec: float = 0.85             # Cooldown period after trigger to prevent double activation


@dataclass
class WakeWordConfig:
    enabled: bool = True
    keywords: List[str] = field(default_factory=lambda: ["hey jarvis", "jarvis"])
    openwakeword_model: str = "hey_jarvis"
    threshold: float = 0.55
    buffer_seconds: float = 2.5       # Rolling audio window for wake-word verification


@dataclass
class STTConfig:
    model_size: str = "base.en"       # "base.en" for ultralow latency, "small.en" for higher accuracy
    device: str = "auto"              # "cuda", "cpu", or "auto"
    compute_type: str = "auto"        # "float16" (cuda), "int8" (cpu), or "auto"
    beam_size: int = 2
    vad_filter: bool = True
    silence_timeout_sec: float = 1.2  # Stop recording after 1.2 seconds of silence
    min_speech_duration_sec: float = 0.4
    max_speech_duration_sec: float = 18.0
    speech_energy_threshold: float = 550.0  # RMS threshold indicating speech activity


@dataclass
class LLMConfig:
    provider: str = "ollama"          # "ollama", "openai", "gemini"
    ollama_base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b-instruct"  # Alternative: "llama3.1:8b"
    temperature: float = 0.2
    max_tokens: int = 400
    
    # API Fallbacks / Alternates
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    gemini_api_key: Optional[str] = os.getenv("GEMINI_API_KEY")
    
    # System Persona
    system_prompt: str = (
        "You are Jarvis, a highly capable, autonomous, and professional AI executive assistant. "
        "You are running directly on the user's Windows laptop. "
        "Your demeanor is concise, razor-sharp, polite, and loyal, reminiscent of Tony Stark's J.A.R.V.I.S. "
        "RULES:\n"
        "1. Never output markdown headers (no #, ##), asterisks (**), or bullet lists in your conversational responses, "
        "because your text will be read aloud by a Text-To-Speech engine. "
        "2. Speak in natural, fluent, professional sentences.\n"
        "3. When the user requests a system action (volume, apps, search, files), use the appropriate tool immediately.\n"
        "4. Summarize tool results concisely in 1 to 2 crisp sentences."
    )


@dataclass
class TTSConfig:
    engine: str = "pyttsx3"            # "pyttsx3" (SAPI5 native zero-latency) or "edge-tts"
    rate: int = 195                    # Words per minute
    volume: float = 1.0
    voice_preference: str = "David"    # Substring to match installed Windows voice (David, Mark, Zira)
    edge_voice: str = "en-GB-RyanNeural"  # British professional tone for edge-tts


@dataclass
class SafetyConfig:
    # Actions that require explicit user confirmation before executing
    destructive_actions: List[str] = field(default_factory=lambda: [
        "delete_file",
        "kill_process",
        "system_shutdown",
        "system_restart",
        "wipe_directory"
    ])


@dataclass
class JarvisConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    clap: ClapDetectorConfig = field(default_factory=ClapDetectorConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    
    # Base workspace or home directory for file searching
    base_user_dir: Path = Path(os.path.expanduser("~"))


# Global singleton configuration
CONFIG = JarvisConfig()
