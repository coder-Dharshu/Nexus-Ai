#!/usr/bin/env python3
"""
Nexus AI — Single-Command Setup & Run
--------------------------------------
Copy this project to any laptop and simply run:

    python setup_and_run.py

What it does:
  1. Checks Python >= 3.11
  2. Creates/activates a virtual environment
  3. Installs all dependencies (pip install -e ".[full]")
  4. Runs first-time API key setup (interactive, skipped on re-runs)
  5. Initialises databases
  6. Starts the FastAPI server (http://localhost:8000)
  7. Starts the Telegram bot polling loop
  8. Sends a Telegram "I'm online" notification
  9. Keeps running until you press Ctrl+C
 10. On exit: clean shutdown + "I'm going offline" Telegram notification
"""
from __future__ import annotations

import asyncio
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
IS_WIN = platform.system() == "Windows"
PY = sys.executable

# ── Colour helpers ─────────────────────────────────────────────────────────────

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"

def ok(m):   print(f"{GREEN}  ✓  {m}{RESET}")
def info(m): print(f"{CYAN}  ▸  {m}{RESET}")
def warn(m): print(f"{YELLOW}  ⚠  {m}{RESET}")
def err(m):  print(f"{RED}  ✗  {m}{RESET}"); sys.exit(1)


BANNER = f"""
{BOLD}{CYAN}
  ╔═══════════════════════════════════════════════════════╗
  ║           NEXUS AI v2.0 — Setup & Run                ║
  ║  Multi-agent AI with live data, email intelligence   ║
  ║  Telegram bot · Gmail integration · Real-time data   ║
  ╚═══════════════════════════════════════════════════════╝
{RESET}"""


# ── Step 1: Python version check ──────────────────────────────────────────────

def check_python() -> None:
    info("Checking Python version…")
    if sys.version_info < (3, 11):
        err(f"Python 3.11+ required. You have {sys.version}. "
            "Download from https://python.org/downloads")
    ok(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


# ── Step 2: Virtual environment ───────────────────────────────────────────────

def ensure_venv() -> None:
    venv_dir = ROOT / ".venv"
    info(f"Checking virtual environment at {venv_dir}…")
    if not venv_dir.exists():
        info("Creating virtual environment…")
        subprocess.run([PY, "-m", "venv", str(venv_dir)], check=True)
        ok("Virtual environment created")
    else:
        ok("Virtual environment exists")


def venv_python() -> str:
    """Return path to the venv Python executable."""
    if IS_WIN:
        return str(ROOT / ".venv" / "Scripts" / "python.exe")
    return str(ROOT / ".venv" / "bin" / "python")


def venv_pip() -> str:
    if IS_WIN:
        return str(ROOT / ".venv" / "Scripts" / "pip.exe")
    return str(ROOT / ".venv" / "bin" / "pip")


def install_deps() -> None:
    vpy = venv_python()
    info("Installing / upgrading dependencies (this may take 2-3 minutes first time)…")
    # Upgrade pip silently
    subprocess.run([vpy, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)
    # Install project in editable mode with full extras
    result = subprocess.run(
        [vpy, "-m", "pip", "install", "--quiet", "-e", '.[full]'],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        # Try without full extras (some may not be available on this platform)
        warn("Full install failed, trying base install…")
        subprocess.run([vpy, "-m", "pip", "install", "--quiet", "-e", "."], cwd=str(ROOT), check=True)
    ok("Dependencies installed")


# ── Step 4: First-time setup (API keys + DB) ─────────────────────────────────

SETUP_DONE_MARKER = ROOT / "data" / ".setup_done"

def run_setup() -> None:
    vpy = venv_python()
    if SETUP_DONE_MARKER.exists():
        ok("Setup already completed (delete data/.setup_done to re-run)")
        return
    info("Running first-time setup (API keys + database)…")
    print()
    subprocess.run([vpy, str(ROOT / "scripts" / "setup.py")], cwd=str(ROOT), check=True)
    # Mark as done
    SETUP_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    SETUP_DONE_MARKER.touch()
    ok("First-time setup complete")


# ── Step 5: Start services ────────────────────────────────────────────────────

async def send_online_notification() -> None:
    """Tell Telegram that Nexus AI is now online."""
    try:
        sys.path.insert(0, str(ROOT))
        from src.interfaces.telegram_bot import nexus_bot
        chat_id = nexus_bot._get_chat_id()
        if chat_id:
            await nexus_bot._send_message(
                chat_id,
                "🟢 *Nexus AI is online*\n\n"
                "All systems ready. Type any query or use:\n"
                "• `/analyze_mail` — AI email analysis\n"
                "• `/important_mail` — Important emails\n"
                "• `/status` — System status",
                parse_mode="Markdown",
            )
            ok("Sent online notification to Telegram")
    except Exception as e:
        warn(f"Could not send Telegram notification: {e}")


async def send_offline_notification() -> None:
    """Tell Telegram that Nexus AI is shutting down."""
    try:
        from src.interfaces.telegram_bot import nexus_bot
        chat_id = nexus_bot._get_chat_id()
        if chat_id:
            await nexus_bot._send_message(
                chat_id,
                "🔴 *Nexus AI is shutting down*\n\nBye! Run `python setup_and_run.py` to restart.",
                parse_mode="Markdown",
            )
    except Exception:
        pass


async def run_async_services() -> None:
    """Start Telegram polling as a background task."""
    sys.path.insert(0, str(ROOT))
    try:
        from src.interfaces.telegram_bot import nexus_bot
        await nexus_bot.start_polling()
        ok("Telegram bot polling started")
    except Exception as e:
        warn(f"Telegram bot could not start: {e}")

    await send_online_notification()


def start_uvicorn() -> subprocess.Popen:
    """Start the FastAPI server as a subprocess."""
    vpy = venv_python()
    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8000")
    info(f"Starting FastAPI server at http://{host}:{port} …")
    proc = subprocess.Popen(
        [vpy, "-m", "uvicorn", "src.api.main:app",
         "--host", host, "--port", port, "--log-level", "warning"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    time.sleep(2)  # Give server a moment to boot
    if proc.poll() is not None:
        warn("Server exited immediately — check logs above for errors")
    else:
        ok(f"Server running at http://{host}:{port}")
    return proc


# ── Main entry point ──────────────────────────────────────────────────────────

def main() -> None:
    print(BANNER)

    # Steps 1-4 (sync)
    check_python()
    ensure_venv()
    install_deps()
    run_setup()

    print()
    info("Starting all services…")
    print()

    # Step 5a: Start FastAPI server (subprocess)
    server_proc = start_uvicorn()

    # Step 5b: Run async services (Telegram polling) in event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_async_services())

    print()
    print(f"{BOLD}{GREEN}  ══════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{GREEN}  Nexus AI is running!{RESET}")
    print(f"{CYAN}  • Web UI:       http://127.0.0.1:8000{RESET}")
    print(f"{CYAN}  • Telegram bot: Send /start to your bot{RESET}")
    print(f"{CYAN}  • Press Ctrl+C to stop{RESET}")
    print(f"{BOLD}{GREEN}  ══════════════════════════════════════════════════════{RESET}")
    print()

    # Keep running until Ctrl+C
    shutdown_requested = False

    def _on_signal(signum, frame):
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            print()
            info("Shutdown requested — stopping gracefully…")

    signal.signal(signal.SIGINT, _on_signal)
    if not IS_WIN:
        signal.signal(signal.SIGTERM, _on_signal)

    try:
        while not shutdown_requested:
            # Check if server crashed
            if server_proc.poll() is not None:
                warn("Server process exited unexpectedly. Restarting…")
                server_proc = start_uvicorn()
            time.sleep(2)
    except KeyboardInterrupt:
        pass

    # ── Shutdown sequence ─────────────────────────────────────────────────────
    info("Sending offline notification…")
    loop.run_until_complete(send_offline_notification())

    info("Stopping Telegram bot…")
    try:
        from src.interfaces.telegram_bot import nexus_bot
        loop.run_until_complete(nexus_bot.stop_polling())
    except Exception:
        pass

    info("Stopping server…")
    server_proc.terminate()
    try:
        server_proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        server_proc.kill()

    loop.close()
    ok("Nexus AI stopped cleanly. Goodbye!")


if __name__ == "__main__":
    main()
