#!/usr/bin/env python3
"""
Nexus AI — First-run setup
Configures all API keys via OS keychain.
Usage: python scripts/setup.py [--auto]
"""
import asyncio, getpass, os, platform, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IS_AUTO = "--auto" in sys.argv
IS_WIN  = platform.system() == "Windows"

def ok(m):   print(f"  ✓  {m}")
def warn(m): print(f"  ⚠  {m}")
def ask(prompt, default=""):
    if IS_AUTO: return default
    val = input(f"  {prompt}{f' [{default}]' if default else ''}: ").strip()
    return val or default
def ask_pass(prompt):
    if IS_AUTO: return ""
    return getpass.getpass(f"  {prompt}: ")

BANNER = """
  ┌──────────────────────────────────────────────────────┐
  │           NEXUS AI — API Key Setup                   │
  │  Keys stored in OS keychain — never on disk          │
  └──────────────────────────────────────────────────────┘
"""

async def main():
    print(BANNER)
    try:
        from config.settings import get_settings
        from src.security.keychain import secrets_manager
        from src.utils.db import init_databases, create_user, get_user_by_username
        from src.security.auth import hash_password
    except ImportError as e:
        print(f"  ✗  Import error: {e}")
        print("     Make sure packages are installed: pip install -e .")
        sys.exit(1)

    s = get_settings()

    # Dirs
    for d in [s.data_dir, s.logs_dir, s.screenshots_dir, s.cache_dir]:
        d.mkdir(parents=True, exist_ok=True)
    ok("Data directories created")

    # JWT
    secrets_manager.ensure_jwt_secret(s.jwt_keychain_username)
    ok("JWT secret ready")

    # DB
    await init_databases()
    ok("Database initialized")

    # Admin user
    if not await get_user_by_username("admin"):
        if IS_AUTO:
            pw = "nexus-admin-2024!"
            warn(f"Auto mode: default admin password = {pw}")
            warn("Change this with: python scripts/setup.py → re-run and set new password")
        else:
            print()
            while True:
                pw = ask_pass("Create admin password (min 12 chars)")
                if len(pw) < 12: warn("Too short"); continue
                if pw != ask_pass("Confirm password"): warn("No match"); continue
                break
        await create_user("admin", hash_password(pw))
        ok("Admin user created")
    else:
        ok("Admin user exists")

    if not IS_AUTO:
        print()
        print("  ── API Keys (all free, press Enter to skip) ──────────────────")
        print()
        print("  REQUIRED for AI responses:")
        groq = ask_pass("  Groq API key (free at console.groq.com)")
        if groq:
            secrets_manager.set(s.groq_keychain_key, groq)
            ok("Groq key saved")
        else:
            warn("Groq key not set — LLM will be limited")

        print()
        print("  OPTIONAL — Music:")
        sp_id = ask_pass("  Spotify Client ID (developer.spotify.com → create app)")
        sp_sec = ask_pass("  Spotify Client Secret") if sp_id else ""
        if sp_id and sp_sec:
            secrets_manager.set(s.spotify_client_id_key, sp_id)
            secrets_manager.set(s.spotify_client_secret_key, sp_sec)
            ok("Spotify credentials saved")

        print()
        print("  OPTIONAL — Notifications:")
        tg = ask_pass("  Telegram bot token (@BotFather → /newbot)")
        if tg:
            secrets_manager.set(s.telegram_keychain_key, tg)
            tg_chat = ask("  Telegram chat ID (message @userinfobot to get yours)")
            if tg_chat: secrets_manager.set(s.telegram_chat_id_keychain_key, tg_chat)
            ok("Telegram configured")

        print()
        print("  OPTIONAL — Email:")
        print("  Gmail setup: console.cloud.google.com → Gmail API → OAuth credentials")
        print("  Then run: python scripts/gmail_auth.py to authorize")

    print()
    print("  ══════════════════════════════════════════════════")
    print("  Setup complete!")
    print()
    if IS_WIN:
        print("  Start:  double-click start.bat")
    else:
        print("  Start:  nexus start")
    print("  Open:   http://127.0.0.1:8000")
    print("  ══════════════════════════════════════════════════")
    print()

if __name__ == "__main__":
    asyncio.run(main())
