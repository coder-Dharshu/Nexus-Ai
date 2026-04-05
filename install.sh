#!/usr/bin/env bash
# ┌─────────────────────────────────────────────────────────────────┐
# │  NEXUS AI — One-line terminal installer                         │
# │  curl -fsSL https://raw.githubusercontent.com/nexus-ai/nexus   │
# │       /main/install.sh | bash                                   │
# └─────────────────────────────────────────────────────────────────┘
set -e
R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m' C='\033[0;36m' B='\033[1m' N='\033[0m'
ok()  { printf "  ${G}✓${N}  %s\n" "$1"; }
warn(){ printf "  ${Y}⚠${N}  %s\n" "$1"; }
err() { printf "  ${R}✗${N}  %s\n" "$1"; exit 1; }
inf() { printf "  ${C}→${N}  %s\n" "$1"; }
hr()  { printf "\n  ${B}%s${N}\n" "────────────────────────────────────────────────"; }

clear
printf "\n${B}"
printf "  ╔══════════════════════════════════════════════════════╗\n"
printf "  ║            NEXUS AI  v2.0                            ║\n"
printf "  ║   Real-time data · Email · Spotify · Flights · AI   ║\n"
printf "  ╚══════════════════════════════════════════════════════╝\n"
printf "${N}\n"

OS=$(uname -s); ARCH=$(uname -m)
NEXUS_HOME="${HOME}/.nexus"
BIN="${HOME}/.local/bin"
mkdir -p "$NEXUS_HOME" "$BIN"

# ── Step 1: Python ─────────────────────────────────────────────────
hr; inf "Step 1/6 · Checking Python 3.10+"
if ! command -v python3 &>/dev/null; then
    if [[ "$OS" == "Darwin" ]]; then
        command -v brew &>/dev/null && brew install python3 || err "Install Python from https://python.org"
    elif [[ "$OS" == "Linux" ]]; then
        sudo apt-get install -y python3 python3-pip 2>/dev/null || \
        sudo dnf install -y python3 python3-pip 2>/dev/null || \
        err "Install Python 3.10+ from https://python.org"
    else
        err "Install Python 3.10+ from https://python.org"
    fi
fi
PYV=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
[[ "${PYV%%.*}" -lt 3 ]] && err "Python 3.10+ required. Got $PYV"
MINOR="${PYV#*.}"; [[ "$MINOR" -lt 10 ]] && err "Python 3.10+ required. Got $PYV"
ok "Python $PYV"

# ── Step 2: Pip packages (no Ollama required) ─────────────────────
hr; inf "Step 2/6 · Installing Python packages (this takes ~2 minutes)"
PIP="python3 -m pip install --quiet"

# Core
$PIP --upgrade pip 2>/dev/null || true
$PIP fastapi "uvicorn[standard]" "python-jose[cryptography]" "passlib[bcrypt]" \
     slowapi pydantic "pydantic-settings" python-dotenv aiosqlite structlog \
     httpx aiofiles requests "beautifulsoup4" lxml apscheduler || \
$PIP --break-system-packages fastapi "uvicorn[standard]" "python-jose[cryptography]" \
     "passlib[bcrypt]" slowapi pydantic "pydantic-settings" python-dotenv \
     aiosqlite structlog httpx aiofiles requests beautifulsoup4 lxml apscheduler
ok "Core packages"

# Real-time data
$PIP playwright spotipy \
     "google-auth" "google-auth-oauthlib" "google-api-python-client" \
     "python-telegram-bot" 2>/dev/null || \
$PIP --break-system-packages playwright spotipy \
     "google-auth" "google-auth-oauthlib" "google-api-python-client" \
     "python-telegram-bot" 2>/dev/null
ok "Integration packages (Spotify, Gmail, Telegram)"

# AI/ML
$PIP faiss-cpu sentence-transformers 2>/dev/null || \
$PIP --break-system-packages faiss-cpu sentence-transformers 2>/dev/null || \
warn "faiss-cpu optional — vector memory disabled without it"
ok "AI packages"

# ── Step 3: Playwright browser ────────────────────────────────────
hr; inf "Step 3/6 · Installing Playwright browser (Chromium, ~200MB)"
python3 -m playwright install chromium 2>/dev/null || \
    playwright install chromium 2>/dev/null || \
warn "Playwright install failed — web scraping will use API fallback"
ok "Playwright Chromium"

# ── Step 4: Install Nexus ─────────────────────────────────────────
hr; inf "Step 4/6 · Installing Nexus AI"
if [[ -d "$NEXUS_HOME/app/.git" ]]; then
    inf "Updating existing installation..."
    cd "$NEXUS_HOME/app" && git pull --quiet 2>/dev/null || true
elif command -v git &>/dev/null && [[ -n "${NEXUS_REPO:-}" ]]; then
    git clone --quiet "$NEXUS_REPO" "$NEXUS_HOME/app"
else
    # Download as zip if no git repo set
    inf "Downloading Nexus AI..."
    if command -v curl &>/dev/null; then
        curl -fsSL "${NEXUS_ZIP:-https://github.com/nexus-ai/nexus/archive/main.zip}" \
             -o "/tmp/nexus.zip" 2>/dev/null && \
        (cd /tmp && unzip -q nexus.zip 2>/dev/null && \
         cp -r nexus-main "$NEXUS_HOME/app") 2>/dev/null || \
        (mkdir -p "$NEXUS_HOME/app" && cp -r . "$NEXUS_HOME/app/" 2>/dev/null || true)
    fi
fi
# Fallback: use current directory
[[ -f "$(pwd)/config/settings.py" ]] && NEXUS_HOME="$(pwd)" && \
    mkdir -p "$NEXUS_HOME/app" || true
ok "Nexus AI installed at $NEXUS_HOME"

# ── Step 5: Create directories + databases ────────────────────────
hr; inf "Step 5/6 · Setting up data directories"
APP="${NEXUS_HOME}/app"
[[ -f "$(pwd)/config/settings.py" ]] && APP="$(pwd)"
for d in data/logs data/screenshots data/cache data/models; do
    mkdir -p "$APP/$d"
done
ok "Directories created"

# ── Step 6: CLI command ───────────────────────────────────────────
hr; inf "Step 6/6 · Installing nexus CLI command"
cat > "$BIN/nexus" << NEXUSCLI
#!/usr/bin/env bash
APP="${APP}"
NEXUS_HOME="${NEXUS_HOME}"
case "\${1:-start}" in
  start)
    echo "Starting Nexus AI..."
    # Try Ollama if installed
    command -v ollama &>/dev/null && (ollama serve &>/tmp/ollama.log & sleep 2)
    cd "\$APP"
    python3 scripts/setup.py --auto 2>/dev/null || true
    uvicorn src.api.main:app --host 127.0.0.1 --port 8000 &
    sleep 2
    echo "Nexus AI running at http://127.0.0.1:8000"
    echo "Press Ctrl+C to stop."
    wait
    ;;
  setup)
    cd "\$APP" && python3 scripts/setup.py
    ;;
  stop)
    pkill -f "uvicorn src.api" 2>/dev/null && echo "Stopped Nexus AI"
    pkill -f "ollama serve" 2>/dev/null && echo "Stopped Ollama"
    ;;
  logs)
    tail -f "\$APP/data/logs/nexus.log"
    ;;
  status)
    curl -s http://127.0.0.1:8000/health/ping && echo "Running" || echo "Not running"
    ;;
  update)
    cd "\$APP" && git pull --quiet 2>/dev/null && python3 -m pip install -e "." --quiet && echo "Updated!"
    ;;
  pull)
    command -v ollama &>/dev/null && ollama pull "\${2:-qwen2.5:7b}" || echo "Install Ollama from https://ollama.com"
    ;;
  open)
    command -v xdg-open &>/dev/null && xdg-open http://127.0.0.1:8000 || \
    command -v open &>/dev/null && open http://127.0.0.1:8000 || \
    echo "Open: http://127.0.0.1:8000"
    ;;
  *)
    echo "Usage: nexus [start|setup|stop|status|logs|update|pull <model>|open]"
    ;;
esac
NEXUSCLI
chmod +x "$BIN/nexus"

# Add to PATH
for RC in ~/.bashrc ~/.zshrc ~/.profile ~/.bash_profile; do
    [[ -f "$RC" ]] || continue
    grep -q "$BIN" "$RC" 2>/dev/null && continue
    echo "export PATH=\"$BIN:\$PATH\"" >> "$RC"
done
export PATH="$BIN:$PATH"

# ── Done ──────────────────────────────────────────────────────────
printf "\n${G}${B}"
printf "  ══════════════════════════════════════════════════════\n"
printf "  ✓  Nexus AI installed!\n"
printf "${N}${B}\n"
printf "  Quick start:\n"
printf "${N}"
printf "    ${C}nexus setup${N}   ← add your API keys (takes 2 min)\n"
printf "    ${C}nexus start${N}   ← start the server\n"
printf "    ${C}nexus open${N}    ← open in browser\n"
printf "\n"
printf "  Required API key (free, no credit card):\n"
printf "    Groq → ${C}console.groq.com${N} (sign up, copy key)\n"
printf "\n"
printf "  Optional keys for more features:\n"
printf "    Spotify  → ${C}developer.spotify.com${N} (music)\n"
printf "    Telegram → ${C}@BotFather${N} on Telegram (notifications)\n"
printf "    Gmail    → ${C}console.cloud.google.com${N} (email)\n"
printf "\n${G}${B}"
printf "  ══════════════════════════════════════════════════════\n"
printf "${N}\n"

# Offer to run setup now
read -rp "  Run setup now? (recommended) [Y/n]: " REPLY
[[ "${REPLY:-Y}" =~ ^[Yy]?$ ]] && nexus setup || true
