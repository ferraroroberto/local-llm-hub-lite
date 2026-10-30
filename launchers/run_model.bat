@echo off
REM ==========================================================
REM  local-llm-hub - parameterized per-model launcher.
REM  Opens a console for one model's backend: cd / title / echo-banner /
REM  pause around the port declared in config/models.yaml.
REM
REM  Usage: launchers\run_model.bat <model-id>   (id from config/models.yaml)
REM  Title/port/banner are pulled live from the registry via
REM  `run_backend --banner`, so there is exactly one place they
REM  can come from.
REM ==========================================================
if "%~1"=="" (
    echo usage: run_model.bat ^<model-id^>
    exit /b 2
)
cd /d "%~dp0.."

set "_LLH_FIRST=1"
for /f "usebackq delims=" %%L in (`.venv\Scripts\python.exe -m src.run_backend --banner %1`) do (
    if defined _LLH_FIRST (
        title %%L
        set "_LLH_FIRST="
        echo ============================================================
    ) else (
        echo   %%L
    )
)
echo ============================================================
echo.

.venv\Scripts\python.exe -m src.run_backend %1
pause
