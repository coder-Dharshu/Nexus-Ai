#!/usr/bin/env python3
"""Nexus AI CLI — nexus [start|setup|stop|status|logs|pull]"""
import argparse, asyncio, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

def main():
    parser = argparse.ArgumentParser(prog="nexus", description="Nexus AI v2.0")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("start", help="Start Nexus AI server")
    setup_p = sub.add_parser("setup", help="First-time setup")
    setup_p.add_argument("--auto", action="store_true")
    sub.add_parser("stop", help="Stop server")
    sub.add_parser("status", help="Check if running")
    sub.add_parser("logs", help="Tail logs")
    pull_p = sub.add_parser("pull", help="Pull Ollama model")
    pull_p.add_argument("model", nargs="?", default="qwen2.5:7b")
    args = parser.parse_args()

    if args.cmd == "start" or args.cmd is None:
        host = os.getenv("HOST", "127.0.0.1")
        port = os.getenv("PORT", "8000")
        workers = os.getenv("WORKERS", "1")
        print(f"Starting Nexus AI on {host}:{port} ...")
        os.execvp("uvicorn", [
            "uvicorn", "src.api.main:app",
            "--host", host, "--port", port,
            "--workers", workers,
            "--log-level", "info",
        ])
    elif args.cmd == "setup":
        sys.argv = ["setup.py"] + (["--auto"] if getattr(args,"auto",False) else [])
        sys.path.insert(0, str(ROOT))
        exec(open(ROOT / "scripts" / "setup.py").read())
    elif args.cmd == "stop":
        subprocess.run(["pkill", "-f", "uvicorn src.api"], check=False)
        print("Stopped.")
    elif args.cmd == "status":
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/health/ping", timeout=3)
            print("Running at http://127.0.0.1:8000")
        except Exception:
            print("Not running.")
    elif args.cmd == "logs":
        log_file = ROOT / "data" / "logs" / "nexus.log"
        os.execvp("tail", ["tail", "-f", str(log_file)])
    elif args.cmd == "pull":
        os.execvp("ollama", ["ollama", "pull", args.model])

if __name__ == "__main__":
    main()
