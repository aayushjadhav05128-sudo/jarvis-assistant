"""
System Tools and Execution Engine for Jarvis on Windows
Implements concrete Windows controls, application launching, web interactions,
safe file operations, and security guardrails for LLM Function Calling.
"""

import os
import sys
import ctypes
import subprocess
import webbrowser
import urllib.parse
import re
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import psutil
import requests

from config import CONFIG

logger = logging.getLogger("Jarvis.Tools")

# Windows Virtual Key Codes
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


def _send_virtual_key(vk_code: int):
    """Simulates a Windows multimedia keypress."""
    user32 = ctypes.windll.user32
    user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


# ============================================================================
# 1. Safety Guardrail
# ============================================================================

class SafetyGuardrail:
    """Intercepts potentially destructive actions and mandates explicit confirmation."""

    @staticmethod
    def requires_confirmation(action: str) -> bool:
        return action.lower() in [act.lower() for act in CONFIG.safety.destructive_actions]

    @staticmethod
    def request_confirmation(action_name: str, details: str) -> bool:
        """
        Prompts via console for user confirmation.
        Can be extended to voice confirmation.
        """
        print(f"\n[SECURITY GUARDRAIL] Destructive action requested: '{action_name}'")
        print(f"[SECURITY GUARDRAIL] Details: {details}")
        sys.stdout.write("Jarvis requires your authorization to proceed. Confirm [y/N]: ")
        sys.stdout.flush()

        # Check user terminal input
        try:
            choice = input().strip().lower()
            confirmed = choice in ["y", "yes"]
            if confirmed:
                logger.info(f"Action '{action_name}' authorized by user.")
            else:
                logger.warning(f"Action '{action_name}' rejected by user.")
            return confirmed
        except Exception:
            return False


# ============================================================================
# 2. Concrete Tool Implementations
# ============================================================================

def system_control(action: str, value: Optional[int] = None) -> Dict[str, Any]:
    """
    Control Windows system settings (volume, screen lock, power, hardware stats).
    """
    action = action.lower().strip()
    logger.info(f"Executing system_control: action={action}, value={value}")

    try:
        if action == "get_battery":
            battery = psutil.sensors_battery()
            if not battery:
                return {"status": "success", "result": "Battery information is unavailable on this device (Desktop or AC power)."}
            percent = battery.percent
            plugged = "plugged in and charging" if battery.power_plugged else "discharging on battery"
            time_left = ""
            if not battery.power_plugged and battery.secsleft != psutil.POWER_TIME_UNLIMITED:
                hours = battery.secsleft // 3600
                mins = (battery.secsleft % 3600) // 60
                time_left = f", with approximately {hours} hours and {mins} minutes remaining"
            return {
                "status": "success",
                "result": f"Battery is currently at {percent}%, {plugged}{time_left}."
            }

        elif action == "get_system_stats":
            cpu_usage = psutil.cpu_percent(interval=0.5)
            ram = psutil.virtual_memory()
            ram_used_gb = ram.used / (1024 ** 3)
            ram_total_gb = ram.total / (1024 ** 3)
            return {
                "status": "success",
                "result": (
                    f"System telemetry: CPU utilization is at {cpu_usage:.1f}%. "
                    f"RAM usage is {ram_used_gb:.1f} GB out of {ram_total_gb:.1f} GB ({ram.percent}% used)."
                )
            }

        elif action == "volume_up":
            steps = value or 5
            for _ in range(steps):
                _send_virtual_key(VK_VOLUME_UP)
            return {"status": "success", "result": f"Increased system volume by {steps} increments."}

        elif action == "volume_down":
            steps = value or 5
            for _ in range(steps):
                _send_virtual_key(VK_VOLUME_DOWN)
            return {"status": "success", "result": f"Decreased system volume by {steps} increments."}

        elif action == "volume_mute":
            _send_virtual_key(VK_VOLUME_MUTE)
            return {"status": "success", "result": "Toggled master volume mute."}

        elif action == "lock_screen":
            ctypes.windll.user32.LockWorkStation()
            return {"status": "success", "result": "Workstation locked successfully."}

        elif action == "sleep":
            if not SafetyGuardrail.request_confirmation("system_sleep", "Putting computer to sleep"):
                return {"status": "aborted", "result": "Sleep command aborted by security protocol."}
            # Windows sleep command
            subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
            return {"status": "success", "result": "Entering sleep state."}

        else:
            return {"status": "error", "error": f"Unknown action '{action}' for system_control."}

    except Exception as e:
        logger.error(f"system_control failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


def launch_application(app_name: str) -> Dict[str, Any]:
    """
    Launches applications installed on Windows.
    """
    app_key = app_name.lower().strip()
    logger.info(f"Executing launch_application: app_name={app_name}")

    # Aliases to Windows executables / URI protocols
    known_apps = {
        "vscode": "code",
        "vs code": "code",
        "visual studio code": "code",
        "chrome": "chrome",
        "google chrome": "chrome",
        "terminal": "wt",
        "windows terminal": "wt",
        "powershell": "powershell",
        "cmd": "cmd",
        "notepad": "notepad",
        "calculator": "calc",
        "calc": "calc",
        "spotify": "spotify",
        "explorer": "explorer",
        "file explorer": "explorer",
        "task manager": "taskmgr",
        "taskmgr": "taskmgr",
        "edge": "msedge",
        "word": "winword",
        "excel": "excel"
    }

    target = known_apps.get(app_key, app_key)

    try:
        # Use Windows 'start' via subprocess to handle paths, UWP apps, and protocols
        proc = subprocess.Popen(f'start "" "{target}"', shell=True)
        return {
            "status": "success",
            "result": f"Application '{app_name}' launched successfully."
        }
    except Exception as e:
        logger.error(f"launch_application failed for '{app_name}': {e}")
        return {
            "status": "error",
            "error": f"Failed to launch '{app_name}': {str(e)}"
        }


def browser_task(action: str, query: Optional[str] = None, url: Optional[str] = None) -> Dict[str, Any]:
    """
    Conducts web searches, opens specific URLs, or reads web text content.
    """
    action = action.lower().strip()
    logger.info(f"Executing browser_task: action={action}, query={query}, url={url}")

    try:
        if action == "search":
            if not query:
                return {"status": "error", "error": "Query is required for web search."}
            encoded_query = urllib.parse.quote_plus(query)
            target_url = f"https://www.google.com/search?q={encoded_query}"
            webbrowser.open_new_tab(target_url)
            return {"status": "success", "result": f"Conducted Google search for: '{query}'."}

        elif action == "open_url":
            if not url:
                return {"status": "error", "error": "URL is required to open webpage."}
            target_url = url if url.startswith(("http://", "https://")) else f"https://{url}"
            webbrowser.open_new_tab(target_url)
            return {"status": "success", "result": f"Opened webpage: {target_url}."}

        elif action == "fetch_content":
            if not url:
                return {"status": "error", "error": "URL is required to fetch content."}
            target_url = url if url.startswith(("http://", "https://")) else f"https://{url}"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JarvisAssistant/1.0"}
            resp = requests.get(target_url, headers=headers, timeout=6.0)
            resp.raise_for_status()

            # Clean HTML text
            raw_html = resp.text
            # Remove scripts and styles
            cleaned = re.sub(r"<(script|style).*?>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
            # Remove html tags
            cleaned = re.sub(r"<[^>]+>", " ", cleaned)
            # Normalize whitespace
            cleaned = re.sub(r"\s+", " ", cleaned).strip()

            snippet = cleaned[:1800] + ("..." if len(cleaned) > 1800 else "")
            return {
                "status": "success",
                "result": f"Retrieved content from {target_url}:\n{snippet}"
            }

        else:
            return {"status": "error", "error": f"Unknown browser action '{action}'."}

    except Exception as e:
        logger.error(f"browser_task failed: {e}")
        return {"status": "error", "error": str(e)}


def file_manager(action: str, path: Optional[str] = None, query: Optional[str] = None) -> Dict[str, Any]:
    """
    Safely lists directory contents, searches for files, or reads documents.
    """
    action = action.lower().strip()
    target_path = Path(os.path.expandvars(path)) if path else CONFIG.base_user_dir
    logger.info(f"Executing file_manager: action={action}, path={target_path}, query={query}")

    try:
        if action == "list_dir":
            if not target_path.exists():
                return {"status": "error", "error": f"Path '{target_path}' does not exist."}
            if not target_path.is_dir():
                return {"status": "error", "error": f"Path '{target_path}' is not a directory."}

            items = []
            for item in list(target_path.iterdir())[:30]:  # Cap to top 30 items
                item_type = "DIR" if item.is_dir() else "FILE"
                items.append(f"{item_type}: {item.name}")

            return {
                "status": "success",
                "result": f"Directory contents of {target_path}:\n" + "\n".join(items)
            }

        elif action == "search":
            if not query:
                return {"status": "error", "error": "Search query pattern is required."}

            matches = []
            pattern = query.lower()
            # Scan directory up to depth 3
            for root, dirs, files in os.walk(target_path):
                # Calculate relative depth
                rel_depth = len(Path(root).relative_to(target_path).parts)
                if rel_depth > 2:
                    dirs.clear()  # Do not recurse deeper
                    continue

                for f in files:
                    if pattern in f.lower():
                        matches.append(str(Path(root) / f))
                        if len(matches) >= 15:
                            break
                if len(matches) >= 15:
                    break

            if matches:
                return {
                    "status": "success",
                    "result": f"Found {len(matches)} matching files for '{query}':\n" + "\n".join(matches)
                }
            return {"status": "success", "result": f"No files matching '{query}' were found in {target_path}."}

        elif action == "read_file":
            if not target_path.exists() or not target_path.is_file():
                return {"status": "error", "error": f"File '{target_path}' does not exist or is a directory."}

            # Size limit check: max 2MB
            if target_path.stat().st_size > 2 * 1024 * 1024:
                return {"status": "error", "error": "File size exceeds safe 2MB limit for direct reading."}

            try:
                content = target_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = target_path.read_text(encoding="latin-1", errors="replace")

            snippet = content[:3000] + ("\n... [truncated]" if len(content) > 3000 else "")
            return {
                "status": "success",
                "result": f"Content of {target_path.name}:\n{snippet}"
            }

        elif action == "delete_file":
            if SafetyGuardrail.requires_confirmation("delete_file"):
                if not SafetyGuardrail.request_confirmation("delete_file", f"Permanently delete {target_path}"):
                    return {"status": "aborted", "result": "File deletion canceled by user."}

            if target_path.exists() and target_path.is_file():
                target_path.unlink()
                return {"status": "success", "result": f"File {target_path.name} was deleted successfully."}
            return {"status": "error", "error": f"File {target_path} does not exist."}

        else:
            return {"status": "error", "error": f"Unknown file_manager action '{action}'."}

    except Exception as e:
        logger.error(f"file_manager failed: {e}")
        return {"status": "error", "error": str(e)}


# ============================================================================
# 3. Standard OpenAI / Ollama Tool Schemas
# ============================================================================

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "system_control",
            "description": "Control Windows system settings including volume, battery status, telemetry, screen lock, and power.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "get_battery",
                            "get_system_stats",
                            "volume_up",
                            "volume_down",
                            "volume_mute",
                            "lock_screen",
                            "sleep"
                        ],
                        "description": "The system control action to perform."
                    },
                    "value": {
                        "type": "integer",
                        "description": "Optional numeric adjustment value (e.g. number of volume increments)."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "launch_application",
            "description": "Launches an application or utility on Windows (e.g. Chrome, VS Code, Spotify, Calculator, Terminal, Notepad).",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The common name of the software application to launch."
                    }
                },
                "required": ["app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_task",
            "description": "Performs web browsing operations: Google search, opening URLs, or fetching webpage text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "open_url", "fetch_content"],
                        "description": "The browser operation to perform."
                    },
                    "query": {
                        "type": "string",
                        "description": "The search keywords when action is 'search'."
                    },
                    "url": {
                        "type": "string",
                        "description": "The target website URL when action is 'open_url' or 'fetch_content'."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_manager",
            "description": "Safely manages and interacts with local files: searching for files, listing directory contents, or reading text documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_dir", "search", "read_file", "delete_file"],
                        "description": "File management operation."
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file path to inspect or read. Defaults to user home folder if omitted."
                    },
                    "query": {
                        "type": "string",
                        "description": "File name search keyword when action is 'search'."
                    }
                },
                "required": ["action"]
            }
        }
    }
]

# Dispatcher mapping
TOOL_MAP = {
    "system_control": system_control,
    "launch_application": launch_application,
    "browser_task": browser_task,
    "file_manager": file_manager
}


def dispatch_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Invokes a registered tool by name with provided arguments."""
    func = TOOL_MAP.get(name)
    if not func:
        return {"status": "error", "error": f"Tool '{name}' is not registered."}
    try:
        return func(**arguments)
    except TypeError as te:
        return {"status": "error", "error": f"Invalid arguments for '{name}': {str(te)}"}
    except Exception as e:
        logger.error(f"Error executing '{name}': {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
