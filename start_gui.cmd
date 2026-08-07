@echo off
setlocal
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"
set "PYTHONPATH=%PROJECT_ROOT%src;%PYTHONPATH%"

where python.exe >nul 2>&1
if errorlevel 1 (
    echo [MyAgent] Python was not found in PATH.
    echo Install Python 3.11 or newer, then try again.
    pause
    exit /b 1
)

if /i "%~1"=="--check" (
    python -m myagent.desktop_sidecar --check
    if errorlevel 1 exit /b 1
    where node.exe >nul 2>&1
    if errorlevel 1 (
        echo [MyAgent] Node.js was not found in PATH.
        exit /b 1
    )
    node "%PROJECT_ROOT%desktop\check.mjs"
    if errorlevel 1 exit /b 1
    exit /b 0
)

if /i "%~1"=="--tk" goto tk_start
if /i "%~1"=="--tk-check" goto tk_check

where node.exe >nul 2>&1
if errorlevel 1 (
    echo [MyAgent] Node.js was not found in PATH.
    echo Install Node.js 22.12 or newer, then try again.
    pause
    exit /b 1
)

set "ELECTRON_EXE=%PROJECT_ROOT%desktop\node_modules\electron\dist\electron.exe"
if not exist "%ELECTRON_EXE%" (
    echo [MyAgent] Electron is not installed.
    echo Run npm install in "%PROJECT_ROOT%desktop", then try again.
    pause
    exit /b 1
)

set "ELECTRON_RUN_AS_NODE="
start "MyAgent" /D "%PROJECT_ROOT%desktop" "%ELECTRON_EXE%" .
if errorlevel 1 (
    echo [MyAgent] The Electron desktop process could not be started.
    pause
    exit /b 1
)

exit /b 0

:tk_start
where pythonw.exe >nul 2>&1
if errorlevel 1 (
    echo [MyAgent] pythonw.exe was not found in PATH.
    echo Repair the Python installation, then try again.
    pause
    exit /b 1
)

start "MyAgent Tk fallback" /D "%PROJECT_ROOT%" pythonw.exe -m myagent.gui
if errorlevel 1 (
    echo [MyAgent] The Tk fallback process could not be started.
    pause
    exit /b 1
)
exit /b 0

:tk_check
python -c "import customtkinter; import tkinter; import myagent.gui as gui; assert gui._set_windows_app_id(), 'Windows AppUserModelID setup failed'; print('MyAgent Tk fallback check passed (CustomTkinter ' + customtkinter.__version__ + ', Tk ' + str(tkinter.TkVersion) + ', AppID ' + gui._WINDOWS_APP_ID + ')')"
exit /b %errorlevel%

endlocal
