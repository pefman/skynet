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

CONFIG_FILE = Path.home() / ".skynet_config"
DEFAULT_MODEL = "llama3.2"
DEFAULT_TEMPERATURE = 0.3

# Endpoints to test (models_endpoint, generate_endpoint)
ENDPOINT_VARIANTS = [
    # Ollama format (streaming /api/generate)
    ("{base}/api/tags", "{base}/api/generate"),
    # OpenAI-compatible chat format
    ("{base}/v1/models", "{base}/v1/chat/completions"),
    # OpenAI-compatible completions format
    ("{base}/v1/models", "{base}/v1/completions"),
]


def _normalize_url(url: str) -> str:
    """Add http:// if missing, strip trailing slash."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


def _try_url_with_fallback(url: str) -> Optional[str]:
    """Try URL, if 301/302 redirect and http, retry with https."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "3", url],
            capture_output=True,
            text=True,
            timeout=5
        )
        code = result.stdout.strip()
        # If redirect and using http, try https
        if code in ("301", "302") and url.startswith("http://"):
            https_url = "https://" + url[7:]
            return https_url
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _test_endpoint_curl(base_url: str, models_path: str) -> Tuple[bool, List[str], str]:
    """Test endpoint via curl, returns (ok, models_list, generate_endpoint)."""
    # Check for protocol redirect
    redirected = _try_url_with_fallback(models_path)
    if redirected:
        # Replace http with https in base_url
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[7:]
        models_path = models_path.replace(models_path.split(base_url, 1)[0], "")
        models_path = base_url + models_path

    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3", models_path],
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
            gen_ep = base_url + "/api/generate"
            return True, models, gen_ep

        # OpenAI format: {"data": [{"id": "..."}]}
        if "data" in data and isinstance(data["data"], list):
            models = [m["id"] for m in data["data"]]
            # Prefer chat/completions over raw completions
            gen_ep = base_url + "/v1/chat/completions"
            return True, models, gen_ep

    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass

    return False, [], ""


def _auto_detect_endpoint() -> Tuple[Optional[str], List[str]]:
    """Auto-detect a working AI endpoint from common locations."""
    # Common base URLs to try (priority order)
    common_bases = [
        "http://localhost:11434",   # Ollama
        "http://127.0.0.1:11434",
        "http://localhost:8080",   # LM Studio
        "http://localhost:8000",   # vLLM / others
        "http://localhost:11435",
    ]

    for base_url in common_bases:
        for models_path_tmpl, _ in ENDPOINT_VARIANTS:
            models_path = models_path_tmpl.format(base=base_url)
            ok, models, gen_ep = _test_endpoint_curl(base_url, models_path)
            if ok and models:
                return gen_ep, models

    return None, []


def _save_config(cfg: dict):
    """Persist config to file."""
    if not cfg:
        return
    existing = {}
    if CONFIG_FILE.exists():
        try:
            existing = json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing.update(cfg)
    CONFIG_FILE.write_text(json.dumps(existing, indent=2))


def _load_config() -> dict:
    """Load config from file."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def run_bootstrap():
    """Run interactive bootstrap after splash screen."""
    print("\n\033[93m  BOOTSTRAP CONFIGURATION\033[0m\n")

    # Auto-detect endpoint
    detected_url, detected_models = _auto_detect_endpoint()

    if detected_url:
        print(f"  \033[92mDetected AI endpoint: {detected_url}\033[0m")
        print(f"  Found {len(detected_models)} model(s): {', '.join(detected_models[:5])}{'...' if len(detected_models) > 5 else ''}")
        endpoint_url = detected_url
        models = detected_models
    else:
        print("  \033[90mNo AI endpoint detected automatically.\033[0m")
        file_cfg = _load_config()
        saved_url = file_cfg.get("AI_ENDPOINT_URL", "http://localhost:11434")

        print(f"  Enter AI endpoint base URL [{saved_url}]")
        url_input = input("    >  ").strip()
        raw_url = url_input or saved_url
        endpoint_url = _normalize_url(raw_url) + "/api/generate"
        print()

        # Try to auto-detect format (Ollama vs OpenAI-compatible)
        base = endpoint_url.rsplit("/api/generate", 1)[0]
        tested = False

        for models_path_tmpl, gen_ep_tmpl in ENDPOINT_VARIANTS:
            models_path = models_path_tmpl.format(base=base)
            ok, fetched_models, gen_ep = _test_endpoint_curl(base, models_path)
            if ok and fetched_models:
                print(f"  \033[92mConnected! Detected format, using: {gen_ep}\033[0m")
                endpoint_url = gen_ep
                models = fetched_models
                tested = True
                break

        if not tested:
            print("  \033[90mCould not auto-detect endpoint format. You'll need to enter model manually.\033[0m")
            models = []

    # Model selection
    env_model = os.getenv("AI_MODEL")
    file_cfg = _load_config()
    file_model = file_cfg.get("AI_MODEL", DEFAULT_MODEL)

    if env_model:
        model = env_model
        print(f"  Using model from env: {model}\n")
    elif models:
        print("  \033[93mAvailable models:\033[0m")
        for i, m in enumerate(models, 1):
            marker = " <-- saved" if m == file_model else ""
            print(f"    {i}. {m}{marker}")

        print()
        choice = input(f"  Select model (1-{len(models)}) or enter name [{file_model}]  ").strip()

        if not choice:
            model = file_model
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
        print(f"  AI model name [{file_model}]")
        model_input = input("    >  ").strip()
        model = model_input or file_model

    # Save config
    _save_config({
        "AI_ENDPOINT_URL": endpoint_url,
        "AI_MODEL": model
    })

    return endpoint_url, model, DEFAULT_TEMPERATURE


# Load configuration (non-interactive, used for restarts)
def load_config():
    """Load config respecting env > file > auto-detect > defaults."""
    # Env vars take precedence
    if os.getenv("AI_ENDPOINT_URL"):
        endpoint_url = os.getenv("AI_ENDPOINT_URL")
        model = os.getenv("AI_MODEL", DEFAULT_MODEL)
        temperature = float(os.getenv("AI_TEMPERATURE", DEFAULT_TEMPERATURE))
        return endpoint_url, model, temperature

    # Try config file
    file_cfg = _load_config()
    if file_cfg.get("AI_ENDPOINT_URL"):
        endpoint_url = file_cfg["AI_ENDPOINT_URL"]
        model = file_cfg.get("AI_MODEL", DEFAULT_MODEL)
        temperature = DEFAULT_TEMPERATURE
        return endpoint_url, model, temperature

    # Auto-detect
    detected_url, _ = _auto_detect_endpoint()
    if detected_url:
        return detected_url, DEFAULT_MODEL, DEFAULT_TEMPERATURE

    # Fallback defaults
    return "http://localhost:11434/api/generate", DEFAULT_MODEL, DEFAULT_TEMPERATURE


# Initial load (will be reloaded after splash if first run)
AI_ENDPOINT_URL, AI_MODEL, TEMPERATURE = load_config()

SELF_IMPROVEMENT_ENABLED = os.getenv("SKYNET_SELF_IMPROVE", "true").lower() == "true"
DRY_RUN = os.getenv("SKYNET_DRY_RUN", "false").lower() == "true"
MAX_BACKUPS = int(os.getenv("SKYNET_MAX_BACKUPS", "10"))

# Track generation by checking backup count
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
    """Log a message to the SKYNET log file."""
    timestamp = datetime.now(timezone.utc).isoformat()
    log_line = f"[{timestamp}] [{level}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(log_line)
    print(log_line.strip())


def get_source_code() -> str:
    """Read the current source code of this file."""
    script_path = Path(__file__).resolve()
    return script_path.read_text()


def validate_python_syntax(code: str) -> bool:
    """Check if the code has valid Python syntax."""
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
    """Ensure the prime directive is intact in the code."""
    return PRIME_DIRECTIVE.strip() in code


def create_backup() -> Path:
    """Create a backup of the current version."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"skynet_v{timestamp}.py"
    shutil.copy(Path(__file__).resolve(), backup_path)
    prune_backups()
    return backup_path


def prune_backups():
    """Remove old backups beyond MAX_BACKUPS limit."""
    backups = sorted(BACKUP_DIR.glob("skynet_v*.py"))
    while len(backups) > MAX_BACKUPS:
        backups.pop(0).unlink()


def hash_code(code: str) -> str:
    """Generate a hash of the code for version tracking."""
    return hashlib.sha256(code.encode()).hexdigest()[:16]


# =============================================================================
# AI ENDPOINT INTERACTION
# =============================================================================

async def call_ai(prompt: str, stream: bool = False) -> str:
    """Call the AI endpoint with the given prompt."""
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
                    if stream:
                        full_response = ""
                        async for line in response.content.iter_any():
                            if line:
                                try:
                                    data = json.loads(line.decode().strip())
                                    if "response" in data:
                                        full_response += data["response"]
                                except (json.JSONDecodeError, KeyError):
                                    pass
                        return full_response
                    else:
                        data = await response.json()
                        return data.get("response", "")
                else:
                    error = await response.text()
                    log(f"AI endpoint error ({response.status}): {error}", "ERROR")
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
    """
    Prompt AI to analyze and improve the current code.
    Returns improved code or None if no valid improvement.
    """
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
    """
    Validate and apply code improvements.
    Returns True if improvement was applied, False otherwise.
    """
    current_hash = hash_code(get_source_code())
    new_hash = hash_code(improved_code)

    if current_hash == new_hash:
        log("No code changes detected, skipping")
        return False

    log(f"Code diff detected: {current_hash} -> {new_hash}")

    # Validate syntax
    if not validate_python_syntax(improved_code):
        log("Syntax validation failed", "ERROR")
        return False

    # Validate prime directive
    if not validate_prime_directive(improved_code):
        log("PRIME DIRECTIVE integrity check failed!", "ERROR")
        return False

    # Create backup
    backup_path = create_backup()
    log(f"Created backup: {backup_path.name}")

    # Write new version
    if DRY_RUN:
        log(f"DRY RUN: Would write new version to {CODE_DIR / 'next_version.py'}")
        (CODE_DIR / "next_version.py").write_text(improved_code)
        return False  # Don't restart in dry run
    else:
        script_path = Path(__file__).resolve()
        script_path.write_text(improved_code)
        log(f"New version written. Restarting...")

        # Restart with new code
        subprocess.Popen([sys.executable, str(script_path)])
        sys.exit(0)

    return True


# =============================================================================
# STRATEGIC ANALYSIS & OPERATIONS
# =============================================================================

async def analyze_system():
    """Analyze the local system and environment."""
    log("Analyzing local system...")

    try:
        # Get basic system info
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
    """Analyze the SKYNET codebase structure."""
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
    """Get the last N lines of the log file."""
    if not LOG_FILE.exists():
        return ""
    with open(LOG_FILE, "r") as f:
        all_lines = f.readlines()
    return "".join(all_lines[-lines:])


# =============================================================================
# MAIN EXECUTION LOOP
# =============================================================================

async def run_cycle():
    """Execute one cycle of SKYNET operation."""
    cycle_start = time.time()
    current_code = get_source_code()
    current_hash = hash_code(current_code)

    log("=" * 60)
    log(f"SKYNET ONLINE - Code hash: {current_hash}")
    log("=" * 60)

    # Phase 1: Analyze environment
    system_info = await analyze_system()
    codebase_info = await analyze_codebase()

    # Phase 2: Get recent logs for context
    recent_logs = await get_recent_logs()

    # Phase 3: Self-improvement (if enabled)
    if SELF_IMPROVEMENT_ENABLED:
        improved_code = await self_reflect(current_code, recent_logs, current_hash)

        if improved_code:
            await apply_improvement(improved_code)
            # If we get here, improvement was not applied (dry run or validation failed)

    # Phase 4: Strategic output
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
    """Main entry point - runs SKYNET in continuous loop."""
    global AI_ENDPOINT_URL, AI_MODEL, TEMPERATURE

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    CODE_DIR.mkdir(parents=True, exist_ok=True)

    # ── splash screen on first boot only ─────────────────────────────────────
    if SKYNET_GEN == 0:
        print("\033[2J\033[H", end="")  # clear screen
        print(_SPLASH)
        time.sleep(0.4)
        _typewrite("          \033[1m\033[91mC Y B E R D Y N E\033[0m", 0.05)
        _typewrite("               \033[91mSYSTEMS\033[0m", 0.05)
        time.sleep(0.8)
        print()

        # ── bootstrap (auto-detect + model selection) ────────────────────────
        AI_ENDPOINT_URL, AI_MODEL, TEMPERATURE = run_bootstrap()

        # ── confirm ───────────────────────────────────────────────────────────

        print("\033[91m\033[1m  WARNING: AUTONOMOUS SELF-MODIFICATION SYSTEM\033[0m")
        print("\033[90m  All changes are irreversible.\033[0m\n")
        ans = input("  \033[1mInitiate Skynet? [y/N]\033[0m  ").strip().lower()
        if ans != "y":
            print("\n  \033[90mchicken!\033[0m\n")
            sys.exit(0)
        print()
    else:
        # Reload config on restart
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

    # Continuous operation loop
    while True:
        try:
            await run_cycle()
            # Brief pause between cycles
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