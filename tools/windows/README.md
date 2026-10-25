# Windows on/off buttons

- `sts-cua-on.cmd`: starts Steam (in WSL), the virtual screen `:99` and Spire 2 on the main menu. Keep its window open while the agent is in use.
- `sts-cua-off.cmd`: stops agent runs, any local model server, Spire 2, Steam and the virtual screen, then runs `wsl --shutdown`, which gives all of WSL's RAM and GPU memory back to Windows.

Install them on the desktop from a WSL terminal:

```
cp ~/sts-cua/tools/windows/sts-cua-*.cmd "$(wslpath "$(powershell.exe -NoProfile -Command '[Environment]::GetFolderPath(\"Desktop\")' | tr -d '\r')")/"
```

`wsl --shutdown` stops every WSL distro and Docker Desktop's WSL backend too, not just this project.
