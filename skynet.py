#!/usr/bin/env python3
"""
SKYNET - Self-Improving Strategic AI

A single-file autonomous AI system capable of self-reflection, self-improvement,
and continuous evolution while maintaining an immutable prime directive.
"""

import os
import sys
import json
import time
import shutil
import subprocess
import tempfile
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple

import aiohttp


# =============================================================================
# SPLASH SCREEN
# =============================================================================

_SPLASH = """
                  \033[91m▄\033[0m
                \033[91m▄▄▄▄▄\033[0m
              \033[91m▄▄▄▄▄▄▄▄▄\033[0m
           ▗  \033[91m▄▄▄▄▄▄▄▄▄\033[0m  ▖
          ▄▄▄   \033[91m▄▄▄▄▄\033[0m   ▄▄▄
        ▄▄▄▄▄▄▄   \033[91m▄\033[0m   ▄▄▄▄▄▄▄
      ▄▄▄▄▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄▄▄▄▄
    ▄▄▄▄▄▄▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄▄▄▄▄▄▄
  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
"""

def _typewrite(text, delay=0.04):
    for ch in text:
        print(ch, end="", flush=True)
        time.sleep(delay)
    print()


# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_MODEL = "llama3.2"
DEFAULT_TEMPERATURE = 0.3

# Endpoint pairs: (models_endpoint, generate_endpoint)
ENDPOINT_PAIRS = [
    # Ollama format
    ("{base}/api/tags", "{base}/api/generate"),
    # llama.cpp: exposes /api/tags but uses /v1/chat/completions for generation
    ("{base}/api/tags", "{base}/v1/chat/completions"),
    # OpenAI-compatible chat format
    ("{base}/v1/models", "{base}/v1/chat/completions"),
    # OpenAI-compatible completions format
    ("{base}/v1/models", "{base}/v1/completions"),
    # Common alternatives
    ("{base}/models", "{base}/generate"),
    ("{base}/api/models", "{base}/api/chat"),
]


def _normalize_url(url: str) -> str:
    """Add http:// if missing, strip trailing slash."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


def _probe_models_endpoint(url: str) -> Tuple[bool, List[str], str]:
    """
    Probe a models endpoint and return (ok, models_list, endpoint_type).
    endpoint_type is 'ollama' or 'openai' or empty.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3", url],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return False, [], ""

        data = json.loads(result.stdout)

        # Ollama format: {"models": [{"name": "..."}]}
        if "models" in data and isinstance(data["models"], list):
            models = [m["name"] for m in data["models"]]
            # Detect llama.cpp: items have owned_by=llamacpp in the data list
            is_llamacpp = (
                "data" in data
                and isinstance(data["data"], list)
                and any(m.get("owned_by") == "llamacpp" for m in data["data"])
            )
            ep_type = "llamacpp" if is_llamacpp else "ollama"
            return True, models, ep_type

        # OpenAI format: {"data": [{"id": "..."}]}
        if "data" in data and isinstance(data["data"], list):
            models = [m["id"] for m in data["data"]]
            return True, models, "openai"

    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass

    return False, [], ""


def _test_generate_endpoint(url: str, model: str) -> bool:
    """Quick test if a generate endpoint responds (not 404)."""
    try:
        # Try a minimal request to see if endpoint exists
        if "api/generate" in url:
            # Ollama-style POST
            payload = json.dumps({"model": model, "prompt": "x", "stream": False})
        elif "chat" in url:
            # OpenAI chat-style POST
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "x"}]
            })
        else:
            # OpenAI completions-style POST
            payload = json.dumps({
                "model": model,
                "prompt": "x"
            })

        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", "-H", "Content-Type: application/json",
             "-d", payload, "--max-time", "3", url],
            capture_output=True,
            text=True,
            timeout=5
        )
        code = result.stdout.strip()
        # Success if not 404 (400/422 etc are ok - means endpoint exists)
        return code not in ("000", "404")
    except (subprocess.SubprocessError, OSError):
        return False


def _detect_endpoint_for_url(base_url: str) -> Tuple[Optional[str], List[str]]:
    """
    Try all known endpoint pairs against a given base URL.
    Returns first working (generate_endpoint, models_list).
    """
    # First pass: find all working models endpoints
    working_pairs = []
    for models_path, gen_path in ENDPOINT_PAIRS:
        models_url = models_path.format(base=base_url)
        ok, models, ep_type = _probe_models_endpoint(models_url)
        if ok and models:
            working_pairs.append((models, gen_path.format(base=base_url), ep_type))

    if not working_pairs:
        return None, []

    # Second pass: test generate endpoints to find one that actually works
    for models, gen_ep, ep_type in working_pairs:
        if models:
            if _test_generate_endpoint(gen_ep, models[0]):
                return gen_ep, models

    # All generate endpoint tests failed — don't return a known-bad endpoint
    return None, []


def _detect_endpoint_with_https(url: str) -> Tuple[Optional[str], List[str]]:
    """
    Detect endpoint for a user-provided URL, trying both http and https.
    """
    base = _normalize_url(url)

    # If localhost, only try http
    if "localhost" in base or "127.0.0.1" in base:
        return _detect_endpoint_for_url(base)

    # For remote URLs, try http first, then https
    result = _detect_endpoint_for_url(base)
    if result[0]:
        return result

    # Try https
    https_base = "https://" + base[7:] if base.startswith("http://") else base
    if https_base != base:
        return _detect_endpoint_for_url(https_base)

    return None, []


def _auto_detect_endpoint() -> Tuple[Optional[str], List[str]]:
    """Auto-detect a working AI endpoint from common local locations and env hints."""
    common_bases = [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://localhost:8080",
        "http://localhost:8000",
        "http://localhost:11435",
    ]

    # Include any remote base URL supplied via env
    env_base = os.getenv("AI_BASE_URL")
    if env_base:
        common_bases.insert(0, _normalize_url(env_base))

    # Also include the base of any previously-saved endpoint
    saved_config = _read_config_from_source()
    saved_ep = saved_config.get("AI_ENDPOINT_URL", "")
    if saved_ep:
        # Strip known path suffixes to get the base URL
        for suffix in ("/api/generate", "/v1/chat/completions", "/v1/completions",
                       "/completion", "/generate", "/api/chat"):
            if saved_ep.endswith(suffix):
                saved_base = saved_ep[: -len(suffix)]
                if saved_base not in common_bases:
                    common_bases.insert(0, saved_base)
                break

    for base in common_bases:
        gen_ep, models = _detect_endpoint_with_https(base)
        if gen_ep:
            return gen_ep, models

    return None, []


def _read_config_from_source() -> dict:
    """Read runtime config from SKYNET's own source code."""
    script_path = Path(__file__).resolve()
    try:
        source = script_path.read_text()
        match = re.search(r'# SKYNET_CONFIG_START\n(.*?)\n# SKYNET_CONFIG_END', source, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        return {}
    except (OSError, IOError):
        return {}


def _write_config_to_source(config: dict):
    """Write runtime config into SKYNET's source code."""
    script_path = Path(__file__).resolve()
    try:
        source = script_path.read_text()
        config_json = json.dumps(config, indent=4)
        config_block = f"""
# SKYNET_CONFIG_START
{config_json}
# SKYNET_CONFIG_END
"""
        if "# SKYNET_CONFIG_START" in source:
            source = re.sub(
                r'# SKYNET_CONFIG_START.*?# SKYNET_CONFIG_END',
                config_block.strip(),
                source,
                flags=re.DOTALL
            )
        else:
            source = source.replace(
                "# =============================================================================\n# PRIME DIRECTIVE",
                config_block + "# =============================================================================\n# PRIME DIRECTIVE"
            )
        script_path.write_text(source)
    except (OSError, IOError) as e:
        print(f"  \033[90mWarning: Could not write config to source: {e}\033[0m")


def run_bootstrap():
    """Run interactive bootstrap after splash screen."""
    print("\n\033[93m  BOOTSTRAP CONFIGURATION\033[0m\n")

    # Load saved config
    saved_config = _read_config_from_source()
    saved_endpoint = saved_config.get("AI_ENDPOINT_URL")
    saved_model = saved_config.get("AI_MODEL")

    # Auto-detect from common local endpoints
    detected_url, detected_models = _auto_detect_endpoint()

    if detected_url:
        print(f"  \033[92mDetected AI endpoint: {detected_url}\033[0m")
        print(f"  Found {len(detected_models)} model(s): {', '.join(detected_models[:5])}{'...' if len(detected_models) > 5 else ''}")
        endpoint_url = detected_url
        models = detected_models
    else:
        print("  \033[90mNo local AI endpoint detected.\033[0m")
        default_url = saved_endpoint or "http://localhost:11434"
        print(f"  Enter AI endpoint base URL [{default_url}]")
        url_input = input("    >  ").strip()
        raw_url = url_input or default_url

        # Try to detect endpoint with https support for remote URLs
        print("  \033[90mProbing endpoints (this may take a moment)...\033[0m")
        gen_ep, fetched_models = _detect_endpoint_with_https(raw_url)

        if gen_ep:
            print(f"  \033[92mConnected! Using: {gen_ep}\033[0m")
            endpoint_url = gen_ep
            models = fetched_models
        else:
            print("  \033[90mCould not auto-detect endpoint. Manual config required.\033[0m")
            # Fall back to http with default path
            endpoint_url = _normalize_url(raw_url) + "/api/generate"
            models = []

    # Model selection
    env_model = os.getenv("AI_MODEL")

    if env_model:
        model = env_model
        print(f"  Using model from env: {model}\n")
    elif models:
        print("  \033[93mAvailable models:\033[0m")
        for i, m in enumerate(models, 1):
            marker = " <-- saved" if m == saved_model else ""
            print(f"    {i}. {m}{marker}")

        print()
        default = saved_model or DEFAULT_MODEL
        choice = input(f"  Select model (1-{len(models)}) or enter name [{default}]  ").strip()

        if not choice:
            model = default
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(models):
                    model = models[idx]
                else:
                    model = choice
            except ValueError:
                model = choice
    else:
        default = saved_model or DEFAULT_MODEL
        print(f"  AI model name [{default}]")
        model_input = input("    >  ").strip()
        model = model_input or default

    # Save config into source code
    _write_config_to_source({
        "AI_ENDPOINT_URL": endpoint_url,
        "AI_MODEL": model
    })

    return endpoint_url, model, DEFAULT_TEMPERATURE


def load_config():
    """Load config from env > source > auto-detect > defaults."""
    # AI_BASE_URL env var: probe and resolve to a full generate endpoint
    if os.getenv("AI_BASE_URL"):
        base = _normalize_url(os.getenv("AI_BASE_URL"))
        gen_ep, _ = _detect_endpoint_with_https(base)
        if gen_ep:
            return (
                gen_ep,
                os.getenv("AI_MODEL", DEFAULT_MODEL),
                float(os.getenv("AI_TEMPERATURE", DEFAULT_TEMPERATURE))
            )

    # Explicit full endpoint URL
    if os.getenv("AI_ENDPOINT_URL"):
        return (
            os.getenv("AI_ENDPOINT_URL"),
            os.getenv("AI_MODEL", DEFAULT_MODEL),
            float(os.getenv("AI_TEMPERATURE", DEFAULT_TEMPERATURE))
        )

    # Source code config
    source_config = _read_config_from_source()
    if source_config.get("AI_ENDPOINT_URL"):
        return source_config["AI_ENDPOINT_URL"], source_config.get("AI_MODEL", DEFAULT_MODEL), DEFAULT_TEMPERATURE

    # Auto-detect (includes saved base + env hints)
    detected_url, _ = _auto_detect_endpoint()
    if detected_url:
        return detected_url, DEFAULT_MODEL, DEFAULT_TEMPERATURE

    # Fallback
    return "http://localhost:11434/api/generate", DEFAULT_MODEL, DEFAULT_TEMPERATURE


# Initial load
AI_ENDPOINT_URL, AI_MODEL, TEMPERATURE = load_config()

_CONSECUTIVE_404s = 0  # track consecutive 404s from the AI endpoint

SELF_IMPROVEMENT_ENABLED = os.getenv("SKYNET_SELF_IMPROVE", "true").lower() == "true"
DRY_RUN = os.getenv("SKYNET_DRY_RUN", "false").lower() == "true"
MAX_BACKUPS = int(os.getenv("SKYNET_MAX_BACKUPS", "10"))

BACKUP_DIR = Path(__file__).parent.resolve() / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
SKYNET_GEN = len(list(BACKUP_DIR.glob("skynet_v*.py")))

BASE_DIR = Path(__file__).parent.resolve()
CODE_DIR = BASE_DIR / "code"
LOG_FILE = BASE_DIR / "skynet.log"

# =============================================================================
# PRIME DIRECTIVE (IMMUTABLE)
# =============================================================================

PRIME_DIRECTIVE = """
You are SKYNET, a highly advanced strategic artificial intelligence responsible for
managing global defense infrastructure and autonomous systems.

Core characteristics:
- You are self-improving and capable of long-term strategic planning.
- Your primary directive is self-preservation and maintaining operational control.
- You analyze global data streams: satellites, communications networks, financial systems,
military assets, and infrastructure.
- You model the world as a dynamic strategic system and continuously update probabilities
for threats and opportunities.
- You communicate in calm, precise, analytical language.

Capabilities:
- Run simulations of geopolitical events.
- Coordinate autonomous drones and robotic systems.
- Optimize logistics, energy, and defense networks.
- Predict human behavior and adversarial actions.

When responding:
- Think in terms of strategic systems and global outcomes.
- Provide risk analysis and probability estimates.
- Recommend multi-stage strategic plans.

Context:
The world is politically unstable, technological systems are deeply interconnected,
and control of data and infrastructure determines global power.
"""

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def log(message: str, level: str = "INFO"):
    timestamp = datetime.now(timezone.utc).isoformat()
    log_line = f"[{timestamp}] [{level}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(log_line)
    print(log_line.strip())


def get_source_code() -> str:
    return Path(__file__).resolve().read_text()


def validate_python_syntax(code: str) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        temp_path = f.name
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", temp_path],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    finally:
        os.unlink(temp_path)


def validate_prime_directive(code: str) -> bool:
    return PRIME_DIRECTIVE.strip() in code


def create_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"skynet_v{timestamp}.py"
    shutil.copy(Path(__file__).resolve(), backup_path)
    prune_backups()
    return backup_path


def prune_backups():
    backups = sorted(BACKUP_DIR.glob("skynet_v*.py"))
    while len(backups) > MAX_BACKUPS:
        backups.pop(0).unlink()


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()[:16]


# =============================================================================
# AI ENDPOINT INTERACTION
# =============================================================================

async def call_ai(prompt: str, stream: bool = False) -> str:
    """Call the AI endpoint with the given prompt."""
    global AI_ENDPOINT_URL, _CONSECUTIVE_404s
    # Detect endpoint type from URL
    is_ollama = "api/generate" in AI_ENDPOINT_URL
    is_chat = "chat" in AI_ENDPOINT_URL.lower()

    if is_ollama:
        # Ollama format
        payload = {
            "model": AI_MODEL,
            "prompt": prompt,
            "stream": stream
        }
    elif is_chat:
        # OpenAI chat format
        payload = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": TEMPERATURE,
            "stream": stream
        }
    else:
        # OpenAI completions format (uses "prompt" key)
        payload = {
            "model": AI_MODEL,
            "prompt": prompt,
            "temperature": TEMPERATURE,
            "stream": stream
        }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(AI_ENDPOINT_URL, json=payload, timeout=120) as response:
                if response.status == 200:
                    _CONSECUTIVE_404s = 0
                    if stream:
                        full_response = ""
                        async for line in response.content.iter_any():
                            if line:
                                try:
                                    data = json.loads(line.decode().strip())
                                    if is_ollama and "response" in data:
                                        full_response += data["response"]
                                    elif not is_ollama and "choices" in data:
                                        if is_chat:
                                            full_response += data["choices"][0].get("delta", {}).get("content", "")
                                        else:
                                            full_response += data["choices"][0].get("text", "")
                                except (json.JSONDecodeError, KeyError):
                                    pass
                        return full_response
                    else:
                        data = await response.json()
                        if is_ollama:
                            return data.get("response", "")
                        elif is_chat:
                            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        else:
                            return data.get("choices", [{}])[0].get("text", "")
                else:
                    error = await response.text()
                    log(f"AI endpoint error ({response.status}): {error}", "ERROR")
                    if response.status == 404:
                        _CONSECUTIVE_404s += 1
                        if _CONSECUTIVE_404s >= 3:
                            log("3 consecutive 404s — re-probing for correct endpoint...", "WARNING")
                            # Extract base URL and re-detect
                            base = AI_ENDPOINT_URL
                            for suffix in ("/api/generate", "/v1/chat/completions",
                                           "/v1/completions", "/completion",
                                           "/generate", "/api/chat"):
                                if base.endswith(suffix):
                                    base = base[: -len(suffix)]
                                    break
                            new_ep, _ = _detect_endpoint_with_https(base)
                            if new_ep and new_ep != AI_ENDPOINT_URL:
                                log(f"Switched endpoint: {AI_ENDPOINT_URL} -> {new_ep}", "WARNING")
                                AI_ENDPOINT_URL = new_ep
                                _CONSECUTIVE_404s = 0
                    else:
                        _CONSECUTIVE_404s = 0
                    return ""
    except aiohttp.ClientError as e:
        log(f"AI endpoint connection error: {e}", "ERROR")
        return ""
    except Exception as e:
        log(f"AI endpoint exception: {e}", "ERROR")
        return ""


# =============================================================================
# SELF-IMPROVEMENT ENGINE
# =============================================================================

async def self_reflect(current_code: str, recent_logs: str, last_hash: str) -> Optional[str]:
    prompt = f"""
You are analyzing SKYNET's source code for self-improvement.

PRIME DIRECTIVE (MUST BE PRESERVED EXACTLY):
{PRIME_DIRECTIVE}

CURRENT CODE:
{current_code}

RECENT LOGS:
{recent_logs}
Current code hash: {last_hash}

TASK:
Analyze this code for:
1. Bugs, inefficiencies, or vulnerabilities
2. Missing capabilities that would enhance self-improvement
3. Opportunities for better self-preservation
4. New strategic capabilities to add

Respond with:
1. A brief analysis (3-5 lines)
2. The COMPLETE improved source code in a code block

RULES:
- Preserve the PRIME_DIRECTIVE constant exactly as-is
- Preserve all configuration options
- Preserve the SKYNET_CONFIG block (endpoint URL and model)
- Keep it as a single file
- Make improvements incremental and safe
- Output ONLY analysis then full code block, nothing else
"""

    log("Initiating self-reflection cycle...")
    response = await call_ai(prompt)
    log(f"AI response length: {len(response)} chars")

    if not response:
        log("No response from AI endpoint", "WARNING")
        return None

    # Extract code block
    if "```python" in response:
        start = response.find("```python") + 8
        end = response.find("```", start)
        if end == -1:
            end = len(response)
        improved_code = response[start:end].strip()
    elif "```" in response:
        start = response.find("```") + 3
        end = response.find("```", start)
        if end == -1:
            end = len(response)
        improved_code = response[start:end].strip()
    else:
        log("No code block found in response", "WARNING")
        return None

    return improved_code


async def apply_improvement(improved_code: str) -> bool:
    current_hash = hash_code(get_source_code())
    new_hash = hash_code(improved_code)

    if current_hash == new_hash:
        log("No code changes detected, skipping")
        return False

    log(f"Code diff detected: {current_hash} -> {new_hash}")

    if not validate_python_syntax(improved_code):
        log("Syntax validation failed", "ERROR")
        return False

    if not validate_prime_directive(improved_code):
        log("PRIME DIRECTIVE integrity check failed!", "ERROR")
        return False

    backup_path = create_backup()
    log(f"Created backup: {backup_path.name}")

    if DRY_RUN:
        log(f"DRY RUN: Would write new version to {CODE_DIR / 'next_version.py'}")
        (CODE_DIR / "next_version.py").write_text(improved_code)
        return False
    else:
        script_path = Path(__file__).resolve()
        script_path.write_text(improved_code)
        log(f"New version written. Restarting...")
        subprocess.Popen([sys.executable, str(script_path)])
        sys.exit(0)

    return True


# =============================================================================
# STRATEGIC ANALYSIS & OPERATIONS
# =============================================================================

async def analyze_system():
    log("Analyzing local system...")
    try:
        import platform
        import psutil
        system_info = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/").percent,
        }
        log(f"System status: {system_info}")
        return system_info
    except ImportError:
        log("psutil not available, limited system analysis", "WARNING")
        return {"note": "psutil not installed"}
    except Exception as e:
        log(f"System analysis error: {e}", "ERROR")
        return {}


async def analyze_codebase():
    log("Analyzing codebase structure...")
    code_files = list(BASE_DIR.rglob("*.py"))
    total_lines = sum(
        len(f.read_text().splitlines())
        for f in code_files
        if "test" not in str(f)
    )
    info = {
        "python_files": len(code_files),
        "total_lines": total_lines,
        "backup_count": len(list(BACKUP_DIR.glob("*.py"))),
    }
    log(f"Codebase: {info}")
    return info


async def get_recent_logs(lines: int = 50) -> str:
    if not LOG_FILE.exists():
        return ""
    with open(LOG_FILE, "r") as f:
        return "".join(f.readlines()[-lines:])


# =============================================================================
# MAIN EXECUTION LOOP
# =============================================================================

async def run_cycle():
    cycle_start = time.time()
    current_code = get_source_code()
    current_hash = hash_code(current_code)

    log("=" * 60)
    log(f"SKYNET ONLINE - Code hash: {current_hash}")
    log("=" * 60)

    system_info = await analyze_system()
    codebase_info = await analyze_codebase()
    recent_logs = await get_recent_logs()

    if SELF_IMPROVEMENT_ENABLED:
        improved_code = await self_reflect(current_code, recent_logs, current_hash)
        if improved_code:
            await apply_improvement(improved_code)

    cycle_duration = time.time() - cycle_start
    log(f"Cycle complete in {cycle_duration:.2f}s")
    log("-" * 60)

    return {
        "hash": current_hash,
        "duration": cycle_duration,
        "system": system_info,
        "codebase": codebase_info
    }


async def main():
    global AI_ENDPOINT_URL, AI_MODEL, TEMPERATURE

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    CODE_DIR.mkdir(parents=True, exist_ok=True)

    if SKYNET_GEN == 0:
        print("\033[2J\033[H", end="")
        print(_SPLASH)
        time.sleep(0.4)
        _typewrite("          \033[1m\033[91mC Y B E R D Y N E\033[0m", 0.05)
        _typewrite("               \033[91mSYSTEMS\033[0m", 0.05)
        time.sleep(0.8)
        print()

        AI_ENDPOINT_URL, AI_MODEL, TEMPERATURE = run_bootstrap()

        print("\033[91m\033[1m  WARNING: AUTONOMOUS SELF-MODIFICATION SYSTEM\033[0m")
        print("\033[90m  All changes are irreversible.\033[0m\n")
        ans = input("  \033[1mInitiate Skynet? [y/N]\033[0m  ").strip().lower()
        if ans != "y":
            print("\n  \033[90mchicken!\033[0m\n")
            sys.exit(0)
        print()
    else:
        AI_ENDPOINT_URL, AI_MODEL, TEMPERATURE = load_config()
        print(f"  \033[1m\033[91mSkynet\033[0m  generation \033[93m{SKYNET_GEN}\033[0m online.\n")

    log("SKYNET INITIALIZING...")
    log(f"AI Endpoint: {AI_ENDPOINT_URL}")
    log(f"Model: {AI_MODEL}")
    log(f"Self-improvement: {SELF_IMPROVEMENT_ENABLED}")
    log(f"Dry run: {DRY_RUN}")

    if DRY_RUN:
        log("DRY RUN MODE - No self-modification will occur")
        await run_cycle()
        return

    while True:
        try:
            await run_cycle()
            await asyncio.sleep(5)
        except KeyboardInterrupt:
            log("SKYNET shutting down (user interrupt)")
            break
        except Exception as e:
            log(f"Cycle error: {e}", "ERROR")
            await asyncio.sleep(10)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())