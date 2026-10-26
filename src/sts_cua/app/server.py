"""HTTP server for the control app: token auth, JSON API (routes.py) and the static page.

    python -m sts_cua.app [--host 127.0.0.1] [--port 8765] [--repo ~/sts-cua] [--token-file ~/.sts-cua/app-token]

Every /api/ request except /api/health needs the token, as an `Authorization: Bearer`
header or the cookie /login?token=... sets. There is no localhost exemption: `tailscale
serve` traffic arrives from 127.0.0.1 too. POST bodies must be application/json, which a
cross-site HTML form can't send.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import auth, files
from .procs import ProcessManager
from .routes import OPEN, ApiError, App, AppConfig, Reply, match

STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
log = logging.getLogger("sts_cua.app")
_TOKEN_RE = re.compile(r"token=[^&\s'\"]*")


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


class Handler(BaseHTTPRequestHandler):
    server_version = "sts-cua-app"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        log.info("%s %s", self.address_string(), _TOKEN_RE.sub("token=<redacted>", format % args))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        path = url.path
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if path == "/login":
                if method != "GET":
                    raise ApiError(405, "use GET /login?token=...")
                return self._login(query)
            if path.startswith("/api/"):
                return self._api(method, path, query)
            if method != "GET":
                raise ApiError(405, f"{method} not allowed on {path}")
            return self._static(path)
        except ApiError as e:
            self._send_json(e.status, {"error": e.message})
        except Exception as e:  # keep the server up; the traceback goes to the app log
            log.exception("error handling %s %s", method, path)
            self._send_json(500, {"error": f"internal error: {type(e).__name__}"})

    def _login(self, query: dict) -> None:
        if not auth.matches(self.token, query.get("token")):
            raise ApiError(401, "wrong token")
        self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": auth.cookie_header(self.token)})

    def _api(self, method: str, path: str, query: dict) -> None:
        found = match(method, path)
        if found is None:
            raise ApiError(404, f"no endpoint {path}")
        name, args = found
        if name not in OPEN and not auth.matches(self.token, auth.from_headers(self.headers)):
            raise ApiError(401, "missing or wrong token")
        body = self._body() if method == "POST" else {}
        result = getattr(self.app, name)(body=body, query=query, **args)
        if isinstance(result, Reply):
            self._send(result.status, result.body, result.content_type)
        else:
            self._send_json(200, result)

    def _body(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "POST bodies must be Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length") from None
        if length < 0 or length > MAX_BODY:
            raise ApiError(413, f"body over {MAX_BODY} bytes")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    def _static(self, path: str) -> None:
        parts = [p for p in path.split("/") if p] or ["index.html"]
        target = files.inside(STATIC, *parts)
        if target is not None and target.is_dir():
            target = target / "index.html"
        if target is None or not target.is_file():
            raise ApiError(404, f"no page {path}")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _send_json(self, status: int, data: dict) -> None:
        self._send(status, (json.dumps(data) + "\n").encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        headers = headers or {}
        if "Cache-Control" not in headers:
            headers["Cache-Control"] = "no-store"
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: App, token: str):
        super().__init__((app.config.host, app.config.port), Handler)
        self.app = app
        self.token = token


def make_server(config: AppConfig, procs: ProcessManager | None = None, **app_args) -> Server:
    """A server bound to config.host:config.port (0 picks a free port), not yet serving."""
    token, created = auth.load_or_create(config.token_file)
    if created:
        print(f"created token file {config.token_file}\ntoken: {token}", flush=True)
    procs = procs or ProcessManager(config.state_dir, display=config.display)
    return Server(App(config, procs, **app_args), token)


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.FileHandler(log_dir / "app.log", encoding="utf-8"), logging.StreamHandler(sys.stderr)):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> None:
    home = Path.home()
    parser = argparse.ArgumentParser(prog="sts_cua.app", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; keep it on localhost (the phone goes through tailscale serve)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo", type=Path, default=home / "sts-cua", help="the sts-cua checkout (runs/, memory/, tools/)")
    parser.add_argument("--token-file", type=Path, default=home / ".sts-cua" / "app-token")
    parser.add_argument("--state-dir", type=Path, default=home / ".sts-cua", help="pid files (run/) and logs (logs/)")
    parser.add_argument("--display", default=":99", help="the agent's X display")
    parser.add_argument("--vllm", help="vllm executable (default: <repo>/.venv/bin/vllm, else vllm on PATH)")
    parser.add_argument("--llama-server", help="llama-server executable (default: $LLAMA_SERVER, "
                        "~/llama.cpp/build/bin/llama-server, else PATH)")
    parser.add_argument("--models-dir", type=Path, help="local models, <dir>/<repo>/<file> (default: $STS_CUA_MODELS, "
                        "else ~/models, else /mnt/d/models if only it exists)")
    args = parser.parse_args(argv)

    config = AppConfig(repo=args.repo.expanduser().resolve(), state_dir=args.state_dir.expanduser(),
                       token_file=args.token_file.expanduser(), host=args.host, port=args.port,
                       display=args.display, vllm=args.vllm, llama_server=args.llama_server,
                       models_dir=args.models_dir.expanduser() if args.models_dir else None)
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _setup_logging(config.state_dir / "logs")
    server = make_server(config)
    host, port = server.server_address[:2]
    log.info("serving %s on http://%s:%s (log in once at /login?token=<token from %s>)",
             config.repo, host, port, config.token_file)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
