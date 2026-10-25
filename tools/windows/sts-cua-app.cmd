@echo off
setlocal
title STS-CUA App
rem Opens the sts-cua control app. Starts its server in WSL first if /api/health doesn't answer.
rem The server runs in a minimized "STS-CUA App server" window; close that window to stop it.
set "BASE=http://localhost:8765"

curl.exe -s -o NUL -m 2 %BASE%/api/health
if not errorlevel 1 goto open

echo Starting the control app server in WSL...
start "STS-CUA App server" /min wsl.exe -e bash -lc "cd ~/sts-cua && exec .venv/bin/python -m sts_cua.app"
set /a TRIES=0
:wait
timeout /t 1 /nobreak >NUL
curl.exe -s -o NUL -m 2 %BASE%/api/health
if not errorlevel 1 goto open
set /a TRIES+=1
if %TRIES% lss 40 goto wait
echo The server didn't answer on %BASE%.
echo See the "STS-CUA App server" window, or ~/.sts-cua/logs/app.log in WSL.
pause
exit /b 1

:open
set "TOKEN="
for /f "usebackq delims=" %%t in (`wsl.exe -e sh -c "cat ~/.sts-cua/app-token"`) do set "TOKEN=%%t"
if not defined TOKEN (
  echo Couldn't read ~/.sts-cua/app-token from WSL.
  pause
  exit /b 1
)
set "URL=%BASE%/login?token=%TOKEN%"
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE%" (
  start "" "%EDGE%" --app="%URL%"
) else (
  start "" "%URL%"
)
exit /b 0
