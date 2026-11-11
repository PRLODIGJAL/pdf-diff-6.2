@echo off
rem ============================================================
rem  Start the tool - double-click for daily use
rem
rem  WHAT IT DOES
rem    Quick health check on the embedded runtime, then opens
rem    the GUI.  If something is broken it tries to fix it
rem    automatically before launching.
rem
rem  WHY THIS FILE IS PURE ASCII
rem    cmd.exe decodes .bat with the console codepage, which
rem    differs between machines.  All Chinese text is printed by
rem    Python (setup_env.py), never from here.
rem
rem  No parenthesised blocks on purpose: "if errorlevel" inside
rem    a block is a classic source of surprises.
rem ============================================================
setlocal
pushd "%~dp0"

set "PYEXE=%~dp0runtime\python.exe"

if not exist "%PYEXE%" goto NOPY

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PYEXE%" -B "%~dp0app\shell\setup_env.py" %*
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" goto DONE
echo.
echo   [!] Stopped with exit code %RC%. See the messages above.
echo.
pause
goto DONE

:NOPY
echo.
echo   [!] runtime\python.exe was not found.
echo.
echo       This package looks incomplete.  Copy the whole folder
echo       again - runtime must sit right next to this .bat.
echo.
pause

:DONE
popd
exit /b %RC%
