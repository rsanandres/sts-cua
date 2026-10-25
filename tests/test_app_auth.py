import os
import stat

import pytest

from sts_cua.app import auth


def test_token_file_created_once_mode_600(tmp_path):
    path = tmp_path / "d" / "app-token"
    token, created = auth.load_or_create(path)
    assert created and len(token) == 64 and int(token, 16) >= 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert auth.load_or_create(path) == (token, False)


def test_loose_token_file_is_tightened_and_short_one_refused(tmp_path):
    path = tmp_path / "app-token"
    path.write_text("a" * 64 + "\n")
    os.chmod(path, 0o644)
    assert auth.load_or_create(path) == ("a" * 64, False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("short")
    with pytest.raises(ValueError):
        auth.load_or_create(path)


def test_matches():
    assert auth.matches("abc", "abc")
    assert not auth.matches("abc", "abd")
    assert not auth.matches("abc", "")
    assert not auth.matches("abc", None)
    assert not auth.matches("abc", "abé")


def test_health_is_open(app_server):
    assert app_server.client.get("/api/health", token=None) == (200, {"ok": True, "version": "0.1.0"})


@pytest.mark.parametrize("path", ["/api/status", "/api/live", "/api/runs", "/api/ledger", "/api/memory/notes",
                                  "/api/screen.png", "/api/logs/app",
                                  "/api/runs/20261025T100000Z/screenshots/step_0001.png"])
def test_no_token_or_wrong_token_is_401(app_server, path):
    c = app_server.client
    assert c.get(path, token=None)[0] == 401
    assert c.get(path, token="0" * 64)[0] == 401
    assert c.get(path, token=None, headers={"Cookie": "sts_cua_token=" + "0" * 64})[0] == 401
    assert c.get(path, token=None, headers={"Authorization": "Basic " + app_server.token})[0] == 401


def test_post_without_token_is_401_and_starts_nothing(app_server):
    status, body = app_server.client.post("/api/runs/start", {"model": "scripted"}, token=None)
    assert status == 401 and "error" in body
    assert app_server.launcher.calls == []


def test_bearer_header_ok(app_server):
    status, body = app_server.client.get("/api/runs")
    assert status == 200 and "runs" in body


def test_login_sets_cookie_that_authenticates(app_server):
    c = app_server.client
    status, _, headers = c.request("/login?token=" + "f" * 64, token=None)
    assert status == 401 and headers.get("Set-Cookie") is None
    assert c.request("/login", token=None)[0] == 401

    status, _, headers = c.request(f"/login?token={app_server.token}", token=None)
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
        assert part in cookie
    pair = cookie.split(";")[0]
    assert pair == f"sts_cua_token={app_server.token}"
    status, body = c.get("/api/status", token=None, headers={"Cookie": f"other=1; {pair}"})
    assert status == 200 and "processes" in body


def test_token_never_logged(app_server, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="sts_cua.app"):
        app_server.client.request(f"/login?token={app_server.token}", token=None)
    assert caplog.records and all(app_server.token not in r.getMessage() for r in caplog.records)


def test_post_needs_json_content_type(app_server):
    c = app_server.client
    status, body = c.post("/api/runs/start", raw_body=b"model=scripted",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415
    status, body = c.post("/api/runs/start", raw_body=b"model=scripted", headers={"Content-Type": "text/plain"})
    assert status == 415
    assert c.post("/api/runs/start", raw_body=b"[1]", headers={"Content-Type": "application/json"})[0] == 400
    assert c.post("/api/runs/start", raw_body=b"{nope", headers={"Content-Type": "application/json"})[0] == 400
    assert c.post("/api/runs/start", raw_body=b'{"daily_budget": NaN, "model": "scripted"}',
                  headers={"Content-Type": "application/json"})[0] == 400
    assert app_server.launcher.calls == []


def test_wrong_method_and_unknown_path(app_server):
    c = app_server.client
    assert c.get("/api/stack/up")[0] == 405
    assert c.get("/api/runs/start")[0] == 404  # GET reads it as run id "start"
    assert c.post("/api/status")[0] == 405
    assert c.get("/api/nope")[0] == 404


def test_static_page_is_served_without_token(app_server):
    status, body, headers = app_server.client.request("/", token=None)
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"<html" in body
    assert app_server.client.request("/index.html", token=None)[0] == 200
    assert app_server.client.request("/../server.py", token=None)[0] == 404
    assert app_server.client.request("/%2e%2e/server.py", token=None)[0] == 404
