# Nexus AI — Quick Start Guide

## 🚀 One Command: Start Everything

```bash
python setup_and_run.py
```

That's it. On any laptop with Python 3.11+, this single command will:

| Step | What it does |
|------|-------------|
| 1 | Checks Python 3.11+ |
| 2 | Creates a virtual environment (`.venv/`) |
| 3 | Installs all dependencies automatically |
| 4 | Runs interactive API key setup *(first time only)* |
| 5 | Starts the web server at `http://localhost:8000` |
| 6 | Starts the Telegram bot |
| 7 | Sends you a Telegram notification "I'm online" |
| 8 | Runs until you press **Ctrl+C** |
| 9 | Sends "Going offline" notification on exit |

---

## API Keys (Free)

Collected interactively on first run. Get them at:

| Key | Where | Required? |
|-----|-------|-----------|
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) | **Yes** |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram | Recommended |
| `TELEGRAM_CHAT_ID` | Message [@userinfobot](https://t.me/userinfobot) | Recommended |
| Spotify Client ID/Secret | [developer.spotify.com](https://developer.spotify.com) | Optional |
| Gmail OAuth | [console.cloud.google.com](https://console.cloud.google.com) → Gmail API | Optional |

> **Re-run setup** at any time: `python scripts/setup.py`

---

## Telegram Bot Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome message + command list |
| `/analyze_mail` | AI tone & category analysis of your inbox |
| `/important_mail` | Top important emails with 1–10 importance scores |
| `/approve <id>` | Approve a pending action (e.g. sending an email) |
| `/reject <id>` | Reject a pending action |
| `/status` | System health check |
| *Any text* | Full multi-agent AI response (crypto, weather, flights, etc.) |

### Example queries to send the bot:
```
What is the current BTC price?
Send an email to boss@company.com about the project update
Weather in Mumbai
Gold price today
Flights from BLR to DEL on 2026-04-01
```

---

## Moving to a New Laptop

```bash
# 1. Copy the project folder (or git clone)
git clone <your-repo-url>
cd nexus-ai-v2-final

# 2. Run setup (will ask for API keys again)
python setup_and_run.py
```

The API keys are stored in your **OS keychain** (Windows Credential Manager / macOS Keychain / Linux Secret Service) — they are never saved to disk or committed to git.

---

## Troubleshooting

### Server won't start
```bash
# Check if port 8000 is in use
netstat -an | findstr 8000   # Windows
lsof -i :8000                # Mac/Linux

# Re-run setup
python scripts/setup.py
```

### Telegram bot not responding
- Make sure `TELEGRAM_BOT_TOKEN` is set: `python scripts/setup.py`
- Check that you messaged your bot (it needs your chat ID — just send `/start`)

### Gmail not working
- Run Gmail OAuth flow: `python scripts/gmail_auth.py`
- Make sure Gmail API is enabled at [console.cloud.google.com](https://console.cloud.google.com)

### Re-run API key setup
```bash
# Delete the setup marker and re-run
del data\.setup_done      # Windows
rm data/.setup_done        # Mac/Linux
python setup_and_run.py
```
