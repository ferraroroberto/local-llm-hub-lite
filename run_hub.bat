@echo off
REM ==========================================================
REM  local-llm-hub-lite - models hub (FastAPI)
REM  Exposes /v1/messages + /v1/chat/completions on :8000
REM  Routes by model name to local llama-server / whisper-server
REM ==========================================================
title Local LLM Hub - hub
cd /d "%~dp0"

echo ============================================================
echo   Local LLM Hub - models hub
echo   Local: http://127.0.0.1:8000   (docs at /docs)
echo   LAN:   binds on 0.0.0.0 - reachable from other machines
echo   Ctrl+C to stop
echo ============================================================
echo.

.venv\Scripts\python.exe -m src.run_backend hub
pause
