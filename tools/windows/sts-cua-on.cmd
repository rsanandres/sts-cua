@echo off
title STS-CUA On
echo Starting the Slay the Spire agent stack in WSL (Steam + Spire 2 on the virtual screen)...
echo Keep this window open while the agent is in use. Close it or run STS-CUA Off to stop.
echo.
wsl.exe -e bash -lc "~/sts-cua/tools/stack_up.sh"
