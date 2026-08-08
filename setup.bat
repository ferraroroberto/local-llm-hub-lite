@echo off
REM ==========================================================
REM  local-llm-hub-lite - one-shot environment setup
REM    1. create .venv (if missing)
REM    2. install Python requirements
REM    3. run the installer checks (vendored binaries + weights
REM       are downloaded by `python -m src.install --fix`)
REM ==========================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    python -m venv .venv || exit /b 1
)

echo Installing requirements ...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1

echo.
echo Running install checks (add --fix to download binaries + weights):
.venv\Scripts\python.exe -m src.install

echo.
echo Next steps:
echo   .venv\Scripts\python.exe -m src.install --fix   (download llama.cpp/whisper.cpp + models)
echo   tray.bat                                        (start the tray + hub)
pause
