# Nexus AI — Windows PowerShell starter
Write-Host "Starting Nexus AI..." -ForegroundColor Cyan
Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep 2
Start-Process "http://127.0.0.1:8000"
uvicorn src.api.main:app --host 127.0.0.1 --port 8000
