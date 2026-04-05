@echo off
title Nexus AI
echo Starting Nexus AI...
start /min ollama serve
timeout /t 2 /nobreak >nul
start http://127.0.0.1:8000
uvicorn src.api.main:app --host 127.0.0.1 --port 8000
