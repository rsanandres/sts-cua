"""Browser test of the control app's page against the stub server (tests/app_ui_stub.py).

Uses the pre-installed Playwright Chromium; skipped if Playwright isn't importable or no
browser is installed. Set STS_UI_SHOTS=<dir> to keep screenshots of each screen.
"""

import os
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from app_ui_stub import TOKEN, serve  # noqa: E402

VIEWPORTS = {"phone": (390, 844), "desktop": (1280, 800)}


@pytest.fixture(scope="module")
def stub():
    server, state = serve()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser installed
            pytest.skip(f"Chromium not available: {exc}")
        yield b
        b.close()


def shots_dir(tmp_path) -> Path:
    path = Path(os.environ.get("STS_UI_SHOTS") or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_page(browser, base, size, login=True):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/login?token={TOKEN}" if login else f"{base}/")
    return context, page, errors


def no_horizontal_scroll(page):
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


def test_login_message_without_cookie(stub, browser):
    base, _ = stub
    context, page, _ = open_page(browser, base, VIEWPORTS["phone"], login=False)
    auth = page.locator("#auth")
    auth.wait_for(state="visible")
    assert "login" in auth.inner_text().lower()
    assert page.locator("#main").is_hidden()
    context.close()


@pytest.mark.parametrize("name", list(VIEWPORTS))
def test_screens(stub, browser, tmp_path, name):
    base, state = stub
    size = VIEWPORTS[name]
    out = shots_dir(tmp_path)
    context, page, errors = open_page(browser, base, size)

    # Control
    page.locator("#procs .proc").nth(4).wait_for()
    assert page.locator("#procs .proc").count() == 5
    assert "model" in page.locator("#procs").inner_text()
    assert "GiB" in page.locator("#gpu-bar").inner_text()
    assert "Floor 3" in page.locator("#mod-line").inner_text()
    no_horizontal_scroll(page)
    page.screenshot(path=str(out / f"ui-control-{name}.png"), full_page=True)

    # WSL shutdown sits behind a confirm; cancelling sends nothing.
    page.click("#wsl-shutdown")
    page.locator("#confirm").wait_for(state="visible")
    assert "frees" in page.locator("#confirm-body").inner_text()
    page.click("#confirm-cancel")
    assert not any(path == "/api/wsl/shutdown" for path, _ in state.calls)

    # Run: live panel, model output rendered as text
    page.click("a[data-tab=run]")
    page.wait_for_function("document.getElementById('live-thought').textContent.includes('<script>')")
    assert page.evaluate("window.__xss") is None
    actions = page.locator("#live-actions").inner_text()
    assert "right double click at (640, 600) with shift" in actions
    assert 'type "<b>hi</b>"' in actions
    assert "<i>x</i>" in page.locator("#live-error").inner_text()
    page.locator("#live-notes li .nid").first.wait_for()
    assert "Nibbits hit hard" in page.locator("#live-notes").inner_text()
    assert page.locator("#live-notes b").count() == 0
    page.wait_for_function("document.getElementById('live-shot').naturalWidth > 0")
    assert page.locator("#run-budget").input_value() == "2.00"
    assert "HP 61/80" in page.locator("#live-state").inner_text()
    no_horizontal_scroll(page)
    page.screenshot(path=str(out / f"ui-run-{name}.png"), full_page=True)

    # Results: reset runs hidden by default
    page.click("a[data-tab=results]")
    rows = page.locator("#runs-table tbody tr")
    rows.first.wait_for()
    assert rows.count() == 4
    assert page.locator("#runs-table tbody tr[data-kind=reset]").count() == 0
    page.check("#show-reset")
    assert rows.count() == 5
    page.uncheck("#show-reset")
    assert "$0.42" in page.locator("#res-budget").inner_text()

    # Evals: the list, then one experiment's report
    page.locator("#evals-table tbody tr[data-eval=memory-ab1]").wait_for()
    assert "7/8" in page.locator("#evals-table").inner_text()
    page.click("#evals-table tbody tr[data-eval=memory-ab1]")
    page.wait_for_function("document.getElementById('eval-detail').textContent.includes('Wilcoxon')")
    assert page.locator("#eval-detail tbody tr").count() == 2
    assert "Paired by seed" in page.locator("#eval-detail").inner_text()
    no_horizontal_scroll(page)
    page.screenshot(path=str(out / f"ui-results-{name}.png"), full_page=True)

    # Replay: first run, step through with buttons and arrow keys
    rows.first.click()
    page.wait_for_function("document.getElementById('step-label').textContent.includes('1 of 12')")
    assert "died" in page.locator("#replay-info").inner_text()
    assert "Nibbit" in page.locator("#replay-combats").inner_text()
    page.click("#step-next")
    page.wait_for_function("document.getElementById('step-label').textContent.includes('2 of 12')")
    page.keyboard.press("ArrowRight")
    page.wait_for_function("document.getElementById('step-label').textContent.includes('3 of 12')")
    assert "<script>" in page.locator("#step-thought").inner_text()
    assert page.evaluate("window.__xss") is None
    page.keyboard.press("ArrowRight")
    page.wait_for_function("document.getElementById('step-label').textContent.includes('4 of 12')")
    assert "outside the window" in page.locator("#step-error").inner_text()
    page.keyboard.press("ArrowLeft")
    page.wait_for_function("document.getElementById('step-label').textContent.includes('3 of 12')")
    page.wait_for_function("document.getElementById('step-shot').naturalWidth > 0")
    assert "Nibbit" in page.locator("#step-state").inner_text()
    no_horizontal_scroll(page)
    page.screenshot(path=str(out / f"ui-replay-{name}.png"), full_page=True)

    # Memory: filter, retire behind a confirm, reflect
    page.click("a[data-tab=memory]")
    notes = page.locator("#notes-table tbody tr")
    page.wait_for_function("document.querySelectorAll('#notes-table tbody tr').length >= 3")
    assert "<b>Not</b>" in page.locator("#notes-table").inner_text()
    page.select_option("#notes-status", "probation")
    page.wait_for_function("[...document.querySelectorAll('#notes-table tbody tr')].every(r => r.innerText.includes('probation'))")
    target = notes.first.get_attribute("data-note")
    page.locator(f"#notes-table tr[data-note={target}] button").click()
    page.locator("#confirm").wait_for(state="visible")
    page.click("#confirm-ok")
    page.wait_for_function(f"!document.querySelector('#notes-table tr[data-note={target}]')")
    assert (f"/api/memory/notes/{target}/retire", {}) in state.calls
    page.select_option("#notes-status", "all")
    page.wait_for_function("document.querySelectorAll('#notes-table tbody tr').length === 4")
    page.locator("#reflect-run option").first.wait_for(state="attached")
    page.click("#reflect-go")
    page.wait_for_function("document.getElementById('toast').textContent.includes('Reflecting')")
    assert any(path == "/api/memory/reflect" and body.get("run_id") for path, body in state.calls)
    no_horizontal_scroll(page)
    page.screenshot(path=str(out / f"ui-memory-{name}.png"), full_page=True)

    # Logs drawer
    page.click("#logs-open")
    page.wait_for_function("document.getElementById('log-text').textContent.includes('supervisor line 199 <tag>')")
    page.select_option("#log-name", "model")
    page.wait_for_function("document.getElementById('log-text').textContent.includes('model line 0')")
    page.screenshot(path=str(out / f"ui-logs-{name}.png"))
    page.keyboard.press("Escape")
    assert page.locator("#logs").is_hidden()

    assert errors == []
    context.close()


@pytest.mark.parametrize("name", list(VIEWPORTS))
def test_coop_section(stub, browser, tmp_path, name):
    base, state = stub
    context, page, errors = open_page(browser, base, VIEWPORTS[name])
    page.goto(f"{base}/#run")
    page.locator("#coop-form").wait_for()
    # Fastest settings by default.
    assert page.locator("#coop-model").input_value() == "claude-haiku-5-5"
    assert page.locator("#coop-thinking").input_value() == "off"
    assert page.locator("#coop-rules").is_checked()
    page.fill("#coop-cost", "2.5")
    page.click("#coop-start")
    page.wait_for_function("document.getElementById('toast').textContent.includes('Co-op agent started')")
    path, body = next((p, b) for p, b in reversed(state.calls) if p == "/api/coop/start")
    assert body == {"model": "claude-haiku-5-5", "thinking": "off", "effort": "low", "max_cost": 2.5,
                    "idle_minutes": 30, "rules": True, "game_build": None}
    assert page.locator("#coop-cost").input_value() == "2.50"
    page.click("#coop-stop")
    page.wait_for_function("document.getElementById('coop-status').textContent.includes('Stopping')")
    assert ("/api/coop/stop", {}) in state.calls
    page.select_option("#coop-model", "qwen3-vl-8b")
    assert page.locator("#coop-thinking").is_disabled()
    no_horizontal_scroll(page)
    page.screenshot(path=str(shots_dir(tmp_path) / f"ui-coop-{name}.png"), full_page=True)
    assert errors == []
    context.close()


@pytest.mark.parametrize("name", list(VIEWPORTS))
def test_model_server_card(stub, browser, tmp_path, name):
    base, state = stub
    state.vllm, state.downloading = True, None
    context, page, errors = open_page(browser, base, VIEWPORTS[name])
    page.wait_for_function("document.getElementById('model-served').textContent.includes('Serving holo4-27b')")
    select = page.locator("#model-name")
    page.wait_for_function("document.querySelectorAll('#model-name option').length > 3")
    labels = select.locator("option").all_inner_texts()
    assert labels[0].startswith("Best that fits")
    holo = next(t for t in labels if t.startswith("holo4-27b"))
    assert "rank 90" in holo and "16.6 GiB" in holo and "fits solo" in holo and "co-op" not in holo
    venus = next(t for t in labels if t.startswith("ui-venus-2-9b"))
    assert "fits co-op + solo" in venus
    assert "not downloaded" in next(t for t in labels if t.startswith("qwen3.8-27b-q5"))
    assert "ui-venus-2-9b" in page.locator("#model-info").inner_text()  # best for co-op now
    assert page.locator("#model-start").is_disabled() and page.locator("#model-download").is_disabled()

    page.click("#model-stop")
    page.wait_for_function("document.getElementById('model-served').textContent.includes('No model server')")
    page.select_option("#model-mode", "solo")
    page.wait_for_function("!document.getElementById('model-start').disabled")
    page.click("#model-start")
    page.wait_for_function("document.getElementById('toast').textContent.includes('Starting holo4-27b (solo)')")
    assert ("/api/model/start", {"model": "best", "mode": "solo"}) in state.calls

    page.select_option("#model-name", "qwen3.8-27b-q5")
    assert "<b>x</b>" in page.locator("#model-info").inner_text()  # notes are text, not HTML
    assert not page.locator("#model-download").is_disabled()
    page.click("#model-download")
    page.wait_for_function("document.getElementById('toast').textContent.includes('Downloading qwen3.8-27b-q5')")
    page.locator("#model-progress").wait_for(state="visible")
    assert "Downloading qwen3.8-27b-q5" in page.locator("#model-progress").inner_text()
    assert page.locator("#model-download").is_disabled()
    no_horizontal_scroll(page)
    page.screenshot(path=str(shots_dir(tmp_path) / f"ui-model-{name}.png"), full_page=True)
    state.downloading = None
    assert errors == []
    context.close()
