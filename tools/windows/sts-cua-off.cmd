@echo off
title STS-CUA Off
echo Stopping the Slay the Spire agent stack in WSL, then shutting WSL down...
wsl.exe -e bash -lc "~/sts-cua/tools/stack_down.sh"
wsl.exe --shutdown
echo.
echo Done. WSL is off and its memory (RAM and GPU) is back to Windows.
timeout /t 8
