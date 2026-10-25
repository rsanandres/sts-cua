# Windows on/off buttons

- `sts-cua-on.cmd`: starts Steam (in WSL), the virtual screen `:99` and Spire 2 on the main menu. Keep its window open while the agent is in use.
- `sts-cua-off.cmd`: stops agent runs, any local model server, Spire 2, Steam and the virtual screen, then runs `wsl --shutdown`, which gives all of WSL's RAM and GPU memory back to Windows.
- `sts-cua-app.cmd`: opens the control app (`python -m sts_cua.app`, see the README's "Control app"). If `http://localhost:8765/api/health` doesn't answer, it first starts the server in WSL in a minimized "STS-CUA App server" window (close that window to stop the server). It reads the token with `wsl.exe -e sh -c "cat ~/.sts-cua/app-token"` and opens `http://localhost:8765/login?token=...` in an Edge app window (`msedge --app=...`), or in the default browser if Edge isn't installed.

Install them on the desktop from a WSL terminal:

```
DESKTOP=$(powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('Desktop')" | tr -d '\r')
cp ~/sts-cua/tools/windows/sts-cua-*.cmd "$(wslpath "$DESKTOP")/"
```

`wsl --shutdown` stops every WSL distro and Docker Desktop's WSL backend too, not just this project.

## Control app from the phone

The server only listens on localhost. The phone reaches it through Tailscale, tailnet only:

1. Install Tailscale on the PC (Windows) and on the phone, and sign both in to the same tailnet.
2. On Windows, in a terminal: `tailscale serve --bg 8765`. Tailscale then serves `https://<pc-name>.<tailnet>.ts.net/` to devices on the tailnet and proxies it to port 8765 on the PC. `tailscale serve status` shows the URL; `tailscale serve reset` turns it off.
3. Get the login URL once from a WSL terminal: `echo "https://<pc-name>.<tailnet>.ts.net/login?token=$(cat ~/.sts-cua/app-token)"`. Open it on the phone; it sets a cookie, so later visits only need `https://<pc-name>.<tailnet>.ts.net/`.

The server must be running (start it with `sts-cua-app.cmd`). Anyone with the token can control the stack; to revoke it, delete `~/.sts-cua/app-token` in WSL and restart the server, which makes a new one.

From Tailscale's [serve docs](https://tailscale.com/kb/1242/tailscale-serve) (read 2026-10-25): a target can be a bare port number, HTTPS is the default with an automatically provisioned certificate, serve shares within the tailnet only (Funnel is the public variant), and `tailscale serve reset` clears the config. Windows `localhost:8765` reaches the server inside WSL: `.wslconfig`'s `localhostForwarding` (default `true`) makes "ports bound to wildcard or localhost in the WSL 2 VM" connectable from Windows via `localhost:port` ([wsl-config](https://learn.microsoft.com/en-us/windows/wsl/wsl-config), read 2026-10-25). In mirrored networking mode the setting is ignored and localhost is shared outright.

Unverified:
- That a bare port proxies to `127.0.0.1:<port>` (the docs only say "only `http://127.0.0.1` is supported for proxies"). If not, use `tailscale serve --bg http://127.0.0.1:8765`.
- Whether HTTPS certificates and MagicDNS must be turned on in the tailnet's admin console first; the CLI says so if they are needed.
- Edge's `--app=` window mode is a Chromium switch, not documented by Microsoft; if it misbehaves, the launcher's default-browser fallback is the same URL.
