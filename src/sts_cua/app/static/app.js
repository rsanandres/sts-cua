// sts-cua control app. Plain JS, no build step, no external requests.
// Every string from the API goes into the page through textContent or attributes,
// never innerHTML: thoughts and notes are model output.
"use strict";

(() => {
  const PROCESSES = ["steam", "xvfb", "game", "model", "supervisor"];
  const TABS = ["control", "run", "results", "memory", "bench"];
  const LONG_JOBS = ["bench", "install"]; // busy values that leave Stack up and Stack down usable
  const STEP_PAGE = 50;
  const SHOT_URL = /^\/api\/runs\/[\w.-]+\/screenshots\/[\w.-]+\.png$/;
  const STEP_SHOT = /^screenshots\/([\w.-]+\.png)$/;
  const SHEET_URL = /^\/api\/bench\/review\/sheet-\d+\.png\?v=\d+$/;

  // ---------- DOM helpers ----------

  const $ = (id) => document.getElementById(id);

  function h(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = String(v);
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function setText(id, value) {
    $(id).textContent = value == null ? "" : String(value);
  }

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const money = (v) => (isNum(v) ? "$" + v.toFixed(v !== 0 && Math.abs(v) < 0.1 ? 4 : 2) : "-");
  const dash = (v) => (v == null || v === "" ? "-" : String(v));
  const gib = (mib) => (mib / 1024).toFixed(1);

  function ago(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function localTime(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "-";
    return new Date(t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function meter(container, label, used, total, unitFn, unit) {
    const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
    const fill = h("div", { class: "fill" + (pct >= 100 ? " full" : pct >= 85 ? " high" : "") });
    fill.style.width = pct.toFixed(1) + "%";
    container.replaceChildren(
      h("div", { class: "meter-label" },
        h("span", { text: label }),
        h("span", { text: `${unitFn(used)} / ${unitFn(total)}${unit} (${Math.round(pct)}%)` })),
      h("div", { class: "bar", role: "progressbar", "aria-label": label, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(pct) }, fill),
    );
  }

  function budgetMeter(container, budget) {
    if (!budget) {
      container.replaceChildren(h("p", { class: "muted", text: "Budget: unknown" }));
      return;
    }
    meter(container, `Spend today (${budget.date || "?"}, Pacific)`, budget.spent_usd || 0, budget.cap_usd || 0, (v) => "$" + v.toFixed(2), "");
  }

  let toastTimer = null;
  function toast(message, bad = false) {
    const node = $("toast");
    node.textContent = message;
    node.className = "toast" + (bad ? " bad" : "");
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, bad ? 7000 : 4000);
  }

  function confirmDialog(title, body, okLabel) {
    const dlg = $("confirm");
    setText("confirm-title", title);
    setText("confirm-body", body);
    setText("confirm-ok", okLabel || "OK");
    dlg.returnValue = "";
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.showModal();
      $("confirm-cancel").focus();
    });
  }

  // ---------- API ----------

  class AuthError extends Error {}
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  let authed = true;

  function showAuth() {
    authed = false;
    stopPolling();
    document.body.classList.add("unauthed");
    setText("auth-url", `${location.origin}/login?token=<your token>`);
    $("auth").hidden = false;
    $("logs").hidden = true;
  }

  function setConn(ok) {
    const dot = $("conn");
    dot.className = "dot " + (ok ? "on" : "bad");
    dot.title = ok ? "Connected" : "Can't reach the app server";
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", credentials: "same-origin", headers: { Accept: "application/json" } };
    if (init.method !== "GET") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body || {});
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      setConn(false);
      throw new ApiError("Can't reach the app server", 0);
    }
    setConn(true);
    if (res.status === 401) {
      showAuth();
      throw new AuthError("Not signed in");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new ApiError((data && data.error) || `${res.status} ${res.statusText}`, res.status);
    return data;
  }

  const report = (e) => { if (!(e instanceof AuthError)) toast(e.message, true); };

  async function post(button, path, body, okMessage) {
    if (button) button.disabled = true;
    try {
      const data = await api(path, { method: "POST", body });
      if (okMessage) toast(okMessage);
      return data;
    } catch (e) {
      report(e);
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ---------- Polling ----------
  // One poll loop for the visible tab; nothing polls while the page is hidden.

  let currentTab = "control";
  let pollGen = 0;
  let pollTimer = null;

  const loops = {
    control: { fn: () => Promise.all([refreshStatus(), refreshModels(), refreshLlama()]), ms: () => 3000 },
    run: { fn: () => Promise.all([refreshLive(), refreshRunModels()]), ms: () => (liveRunning ? 2000 : 10000) },
    bench: { fn: () => refreshBench(), ms: () => (benchRunning ? 2000 : 8000) },
  };

  function stopPolling() {
    pollGen++;
    clearTimeout(pollTimer);
    clearTimeout(logTimer);
  }

  function startPolling() {
    stopPolling();
    if (!authed || document.hidden) return;
    const loop = loops[currentTab];
    if (loop) tick(loop, pollGen);
    if (!$("logs").hidden) loadLogs();
  }

  async function tick(loop, gen) {
    try { await loop.fn(); } catch (e) { /* shown by the refresh itself */ }
    if (gen !== pollGen || !authed || document.hidden) return;
    pollTimer = setTimeout(() => tick(loop, gen), loop.ms());
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else startPolling();
  });

  // ---------- Routing ----------

  function route() {
    const [tab, arg] = location.hash.replace(/^#/, "").split("/");
    currentTab = TABS.includes(tab) ? tab : "control";
    for (const name of TABS) $("tab-" + name).hidden = name !== currentTab;
    for (const link of document.querySelectorAll(".tabs a")) {
      if (link.dataset.tab === currentTab) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    if (!authed) return;
    if (currentTab === "results") {
      if (arg) openReplay(decodeURIComponent(arg));
      else closeReplay();
    } else if (currentTab === "memory") {
      loadNotes();
      loadReflectRuns();
    } else if (currentTab === "bench") {
      loadBenchResults();
    } else if (currentTab === "run") {
      refreshRunModels(true).catch(() => {});
    }
    startPolling();
  }

  window.addEventListener("hashchange", route);

  // ---------- Control ----------

  let lastStatus = null;

  async function refreshStatus() {
    try {
      const s = await api("/api/status");
      lastStatus = s;
      renderStatus(s);
      return s;
    } catch (e) {
      if (!(e instanceof AuthError)) setText("busy-line", e.message);
      throw e;
    }
  }

  function renderStatus(s) {
    const procs = s.processes || {};
    $("procs").replaceChildren(...PROCESSES.map((name) => {
      const p = procs[name] || {};
      const detail = p.running
        ? [p.since ? "since " + ago(p.since) : "running", p.pid ? `pid ${p.pid}` : null].filter(Boolean).join(" · ")
        : "stopped";
      return h("li", { class: "proc", "data-name": name },
        h("span", { class: "dot" + (p.running ? " on" : ""), title: p.running ? "running" : "stopped" }),
        h("span", { class: "name", text: name }),
        h("span", { class: "detail", text: detail, title: p.since ? localTime(p.since) : null }));
    }));

    if (s.gpu && s.gpu.total_mib) meter($("gpu-bar"), "GPU memory", s.gpu.used_mib, s.gpu.total_mib, gib, " GiB");
    else $("gpu-bar").replaceChildren(h("p", { class: "muted", text: "GPU: not available" }));
    if (s.ram && s.ram.total_mib) meter($("ram-bar"), "RAM (WSL)", s.ram.used_mib, s.ram.total_mib, gib, " GiB");
    else $("ram-bar").replaceChildren(h("p", { class: "muted", text: "RAM: unknown" }));
    budgetMeter($("ctl-budget"), s.budget);

    setText("mod-line", modLine(s.mod));

    const busy = s.busy || null;
    setText("busy-line", busy ? `Busy: ${busy}` : "Idle.");
    $("busy-line").className = busy ? "" : "muted";
    const badge = $("busy-badge");
    badge.hidden = !busy;
    badge.textContent = busy ? `busy: ${busy}` : "";
    // A bench job or llama.cpp install can run for an hour: Stack down stays usable and stops it.
    for (const id of ["stack-up", "stack-down"]) $(id).disabled = !!busy && !LONG_JOBS.includes(busy);
    renderModelButtons();
  }

  // ---------- Model server ----------

  let lastModels = null;

  async function refreshModels() {
    try {
      const data = await api("/api/models");
      lastModels = data;
      renderModels(data);
      return data;
    } catch (e) {
      if (!(e instanceof AuthError)) setText("model-info", e.message);
      throw e;
    }
  }

  function fitText(m) {
    const fits = ["coop", "solo", "max"].filter((mode) => m.fits && m.fits[mode]).map((mode) => (mode === "coop" ? "co-op" : mode));
    return fits.length ? "fits " + fits.join(" + ") : "fits neither budget";
  }

  function modelLabel(m) {
    return `${m.name} · rank ${m.rank} · ${m.file_gib.toFixed(1)} GiB · ${fitText(m)}${m.downloaded ? "" : " · not downloaded"}`;
  }

  function renderModels(data) {
    const select = $("model-name");
    const keep = select.value || "best";
    const rows = data.models || [];
    const options = [h("option", { value: "best", text: "Best that fits (strongest downloaded model)" }),
      ...rows.map((m) => h("option", { value: m.name, text: modelLabel(m) }))];
    const same = select.options.length === options.length
      && [...select.options].every((o, i) => o.value === options[i].value && o.textContent === options[i].textContent);
    if (!same) {
      select.replaceChildren(...options);
      select.value = rows.some((m) => m.name === keep) ? keep : "best";
    }

    const srv = data.server || {};
    if (srv.running) {
      const served = (srv.served && srv.served.length) ? srv.served.join(", ") : "loading...";
      const which = srv.model && srv.model !== served ? `${srv.model}, ` : "";
      setText("model-served", `Serving ${served} (${which}${dash(srv.mode)}) on port ${srv.port} · pid ${dash(srv.pid)}`);
      $("model-served").className = "";
    } else {
      setText("model-served", "No model server running.");
      $("model-served").className = "muted";
    }
    renderModelInfo();
    renderModelButtons();
    if (lastLlama) renderLlama(lastLlama);
  }

  function selectedModel() {
    const name = $("model-name").value;
    return (lastModels && (lastModels.models || []).find((m) => m.name === name)) || null;
  }

  function renderModelInfo() {
    if (!lastModels) return;
    const mode = $("model-mode").value;
    const m = selectedModel();
    const g = lastModels.gpu;
    const free = g ? `${gib(g.free_mib)} GiB VRAM free` : "free VRAM unknown";
    let info;
    if (!m) {
      const pick = (lastModels.best || {})[mode];
      info = pick ? `Now: ${pick} (the strongest downloaded model within ${free} and the ${mode === "coop" ? "co-op" : mode} budget).`
        : `No downloaded model fits (${free}). Download one first.`;
    } else {
      info = `${m.server === "llama" ? "llama.cpp" : "vLLM"} · needs ${m.need_gib.toFixed(1)} GiB · ${free} · license ${dash(m.license)}. ${m.notes || ""}`;
    }
    if (lastModels.root) info += ` Models folder: ${lastModels.root}.`;
    setText("model-info", info);

    const bar = $("model-progress");
    const d = m && m.download;
    if (d && (d.running || (d.bytes > 0 && d.bytes < d.total))) {
      bar.hidden = false;
      meter(bar, d.running ? `Downloading ${m.name}` : `${m.name}: partly downloaded`, d.bytes, d.total, (b) => (b / 2 ** 30).toFixed(1), " GiB");
    } else {
      bar.hidden = true;
    }
  }

  function renderModelButtons() {
    const busy = !!(lastStatus && lastStatus.busy && lastStatus.busy !== "bench"); // the server says when a bench job conflicts
    const procs = (lastStatus && lastStatus.processes) || {};
    const on = !!(procs.model && procs.model.running);
    $("model-start").disabled = busy || on;
    $("model-stop").disabled = busy || !on;
    const m = selectedModel();
    const downloading = !!(lastModels && lastModels.download && lastModels.download.running);
    $("model-download").disabled = !m || m.downloaded || downloading;
  }

  // llama.cpp: is llama-server installed, and the Install button (tools/install_llama_cpp.sh)

  let lastLlama = null;

  async function refreshLlama() {
    try {
      lastLlama = await api("/api/llama");
      renderLlama(lastLlama);
      return lastLlama;
    } catch (e) {
      if (!(e instanceof AuthError)) setText("llama-line", e.message);
      throw e;
    }
  }

  function renderLlama(d) {
    const inst = d.install || {};
    let line;
    if (inst.running) line = `Installing llama.cpp ${inst.prebuilt ? "(prebuilt release)" : "(building from source)"}${inst.since ? ", started " + ago(inst.since) : ""}. The whole log is under Logs > install.`;
    else if (d.found) line = `llama-server: ${d.path}` + (d.version ? ` · version ${d.version}` : "") + (d.release ? ` · release ${d.release}` : "");
    else line = "llama-server not found. Install it here (into ~/llama.cpp), or run tools/install_llama_cpp.sh.";
    setText("llama-line", line);
    $("llama-line").className = "small" + (d.found || inst.running ? "" : " warn-text");
    const btn = $("llama-install");
    btn.textContent = d.found ? "Update llama.cpp" : "Install llama.cpp";
    const serverOn = !!(lastModels && lastModels.server && lastModels.server.running);
    btn.disabled = !!inst.running || serverOn;
    let hint = "";
    if (serverOn && !inst.running) hint = "Stop the model server first: llama-server can't be replaced while it runs.";
    else if (!inst.running && isNum(inst.exit_code) && inst.exit_code !== 0) {
      hint = inst.exit_code < 0 ? "The last install was stopped." : `The last install failed (exit ${inst.exit_code}).`;
      hint += inst.nvcc_missing
        ? " nvcc (the CUDA toolkit) isn't installed in WSL: tick Prebuilt release, or install cuda-toolkit-12-x as below."
        : " Its last lines are below.";
    } else if (!inst.running && inst.exit_code === 0) hint = "The last install finished.";
    else if (inst.nvcc_missing) hint = "nvcc (the CUDA toolkit) isn't installed in WSL: tick Prebuilt release, or install cuda-toolkit-12-x as below.";
    $("llama-hint").hidden = !hint;
    setText("llama-hint", hint);
    const tail = inst.tail || [];
    $("llama-tail").hidden = !tail.length;
    $("llama-tail").textContent = tail.join("\n");
  }

  $("llama-install").addEventListener("click", async (ev) => {
    const prebuilt = $("llama-prebuilt").checked;
    const ok = await confirmDialog(
      prebuilt ? "Install the prebuilt llama.cpp?" : "Build llama.cpp?",
      prebuilt
        ? "Downloads the newest llama.cpp release's Ubuntu CUDA 12.8 binaries and CUDA runtime into ~/llama.cpp/build/bin. No CUDA toolkit needed; the Windows driver must support CUDA 12.8."
        : "Clones or updates llama.cpp in ~/llama.cpp and builds llama-server with CUDA for the RTX 4090. Needs git, cmake, g++ and the CUDA toolkit (nvcc) inside WSL, and takes several minutes.",
      "Install",
    );
    if (!ok) return;
    const button = ev.currentTarget;
    await post(button, "/api/llama/install", { prebuilt }, "Installing llama.cpp in the background (Logs > install)");
    refreshLlama().catch(() => {});
  });

  function modLine(mod) {
    if (!mod || !mod.reachable) return "Mod: not reachable";
    const parts = [`Screen ${dash(mod.state_type)}`];
    if (mod.act != null) parts.push(`Act ${mod.act}`);
    if (mod.floor != null) parts.push(`Floor ${mod.floor}`);
    if (mod.hp != null) parts.push(`HP ${mod.hp}${mod.max_hp != null ? "/" + mod.max_hp : ""}`);
    return "Mod: " + parts.join(" · ");
  }

  $("stack-up").addEventListener("click", async (ev) => {
    await post(ev.currentTarget, "/api/stack/up", {}, "Starting the stack");
    refreshStatus().catch(() => {});
  });
  $("stack-down").addEventListener("click", async (ev) => {
    await post(ev.currentTarget, "/api/stack/down", {}, "Stopping the stack");
    refreshStatus().catch(() => {});
  });
  $("model-name").addEventListener("change", () => { renderModelInfo(); renderModelButtons(); });
  $("model-mode").addEventListener("change", renderModelInfo);
  $("model-start").addEventListener("click", async (ev) => {
    const body = { model: $("model-name").value, mode: $("model-mode").value };
    const data = await post(ev.currentTarget, "/api/model/start", body);
    if (data) toast(`Starting ${data.model} (${data.mode}); loading takes a minute or two`);
    refreshStatus().then(refreshModels).catch(() => {});
  });
  $("model-stop").addEventListener("click", async (ev) => {
    await post(ev.currentTarget, "/api/model/stop", {}, "Stopping the model server");
    refreshStatus().then(refreshModels).catch(() => {});
  });
  $("model-download").addEventListener("click", async (ev) => {
    const model = $("model-name").value;
    await post(ev.currentTarget, "/api/model/download", { model }, `Downloading ${model} in the background`);
    refreshModels().catch(() => {});
  });
  $("wsl-shutdown").addEventListener("click", async (ev) => {
    const ok = await confirmDialog(
      "Shut down WSL?",
      "This runs wsl --shutdown: it stops any run, the model server, the game, Steam and this app, and frees all of WSL's GPU memory and RAM for gaming on Windows. This page stops working until WSL and the app are started again from Windows.",
      "Shut down WSL",
    );
    if (!ok) return;
    const data = await post(ev.currentTarget, "/api/wsl/shutdown", { confirm: true });
    if (data) {
      stopPolling();
      toast("WSL is shutting down. This page will stop updating.");
      setText("busy-line", "WSL is shutting down.");
    }
  });

  // ---------- Run ----------

  let liveRunning = false;
  let liveShotUrl = null;
  let notesById = null;
  let notesFetching = null;

  const FORM_KEY = "sts-cua.run-form";
  const formFields = ["run-model", "run-thinking", "run-effort", "run-local-thinking", "run-local-effort", "run-budget",
    "run-maxruns", "run-build", "run-memory"];

  function saveForm() {
    try {
      const values = {};
      for (const id of formFields) values[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
      localStorage.setItem(FORM_KEY, JSON.stringify(values));
    } catch (e) { /* storage unavailable */ }
  }

  function restoreForm() {
    try {
      const values = JSON.parse(localStorage.getItem(FORM_KEY) || "{}");
      for (const id of formFields) {
        if (!(id in values)) continue;
        setField(id, values[id]);
      }
    } catch (e) { /* storage unavailable */ }
  }

  // A saved select value whose option isn't there yet (local models come from /api/models) waits in data-want.
  function setField(id, value) {
    const el = $(id);
    if (el.type === "checkbox") { el.checked = !!value; return; }
    if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === value)) { el.dataset.want = value; return; }
    el.value = value;
  }

  // Local models (the registry, from /api/models) and the model server, for the Run tab's options and hints.
  let runModels = null;
  let runModelsAt = 0;

  async function refreshRunModels(force = false) {
    if (!force && runModels && Date.now() - runModelsAt < 10000) return runModels;
    try {
      runModels = await api("/api/models");
      runModelsAt = Date.now();
    } catch (e) {
      if (e instanceof AuthError) throw e;
      return runModels;
    }
    for (const id of ["run-model", "coop-model"]) fillLocalOptions($(id), runModels.models || []);
    updateRunHint();
    updateCoopHint();
    return runModels;
  }

  function fillLocalOptions(select, rows) {
    const group = select.querySelector("optgroup.local-models");
    const label = (m) => m.name + (m.downloaded ? "" : " (not downloaded)");
    const same = group.children.length === rows.length
      && rows.every((m, i) => group.children[i].value === m.name && group.children[i].textContent === label(m));
    if (same) return;
    const keep = select.dataset.want || select.value;
    group.replaceChildren(...rows.map((m) => h("option", { value: m.name, text: label(m) })));
    if ([...select.options].some((o) => o.value === keep)) select.value = keep;
    delete select.dataset.want;
  }

  const localInfo = (name) => (runModels && (runModels.models || []).find((m) => m.name === name)) || null;
  const isLocal = (name) => !!name && !name.startsWith("claude-") && name !== "scripted";

  // Thinking select: "" is the factory's default (co-op: off; else the registry's). Effort needs thinking on.
  function syncLocalControls(thinkId, effortId, model, coop) {
    const m = localInfo(model);
    const think = $(thinkId);
    const effort = $(effortId);
    const hasThinking = !m || m.thinking;
    if (!hasThinking) think.value = "";
    think.disabled = !hasThinking;
    think.options[0].textContent = !hasThinking ? "none (no thinking)"
      : coop ? "default (off in co-op)" : m ? `default (${m.thinking ? "on" : "off"})` : "default";
    const on = think.value === "on" || (think.value === "" && !coop && !!(m && m.thinking));
    effort.disabled = !hasThinking || !on;
    if (effort.disabled) effort.value = "";
  }

  // Whether the model server serves the selected local model: [text, problem].
  function serverHint(model) {
    const srv = runModels && runModels.server;
    if (!srv) return ["Needs the model server (Control tab).", false];
    if (!srv.running) return [`No model server is running: start ${model} in the Control tab first.`, true];
    if (!srv.ready) return [`The model server is still loading ${dash(srv.model)}.`, true];
    if (srv.model && srv.model !== model) return [`The model server is serving ${srv.model}, not ${model}: stop it and start ${model} in the Control tab.`, true];
    return [`The model server is serving it (${dash(srv.mode)} mode). Local runs cost nothing.`, false];
  }

  function localBody(prefix, model) {
    if (!isLocal(model)) return {};
    return { local_thinking: $(prefix + "-local-thinking").value || null, local_effort: $(prefix + "-local-effort").value || null };
  }

  function updateRunHint() {
    const model = $("run-model").value;
    const claude = model.startsWith("claude-");
    const local = isLocal(model);
    $("run-thinking").disabled = !claude;
    $("run-effort").disabled = !claude;
    $("run-claude").hidden = !claude;
    $("run-local").hidden = !local;
    let hint = "";
    let problem = false;
    if (local) {
      syncLocalControls("run-local-thinking", "run-local-effort", model, false);
      [hint, problem] = serverHint(model);
    } else if (model === "scripted") hint = "Scripted runs make no model calls.";
    else hint = "Thinking and effort apply to Claude models only.";
    setText("run-hint", hint);
    $("run-hint").className = "small " + (problem ? "warn-text" : "muted");
  }

  $("run-model").addEventListener("change", updateRunHint);
  $("run-local-thinking").addEventListener("change", updateRunHint);

  $("run-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    let budget = parseFloat($("run-budget").value);
    if (!Number.isFinite(budget) || budget < 0) budget = 2.0;
    budget = Math.min(budget, 5.0);
    $("run-budget").value = budget.toFixed(2);
    const maxRuns = parseInt($("run-maxruns").value, 10);
    const body = {
      model: $("run-model").value,
      thinking: $("run-thinking").value,
      effort: $("run-effort").value,
      daily_budget: budget,
      max_runs: Number.isFinite(maxRuns) && maxRuns > 0 ? maxRuns : null,
      memory: $("run-memory").checked,
      game_build: $("run-build").value.trim() || null,
      ...localBody("run", $("run-model").value),
    };
    saveForm();
    const data = await post($("run-start"), "/api/runs/start", body);
    if (data) {
      toast(`Supervisor started${data.pid ? " (pid " + data.pid + ")" : ""}`);
      liveRunning = true;
      startPolling();
    }
  });

  $("run-stop").addEventListener("click", async (ev) => {
    const data = await post(ev.currentTarget, "/api/runs/stop", {}, "Stopping the supervisor");
    if (data) setTimeout(startPolling, 1000);
  });

  // Co-op: one agent session playing with Raph (python -m sts_cua --coop), not the supervisor.

  const COOP_KEY = "sts-cua.coop-form";
  const coopFields = ["coop-model", "coop-thinking", "coop-effort", "coop-local-thinking", "coop-local-effort", "coop-cost",
    "coop-idle", "coop-build", "coop-rules"];

  function saveCoop() {
    try {
      const values = {};
      for (const id of coopFields) values[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
      localStorage.setItem(COOP_KEY, JSON.stringify(values));
    } catch (e) { /* storage unavailable */ }
  }

  function restoreCoop() {
    try {
      const values = JSON.parse(localStorage.getItem(COOP_KEY) || "{}");
      for (const id of coopFields) {
        if (!(id in values)) continue;
        setField(id, values[id]);
      }
    } catch (e) { /* storage unavailable */ }
  }

  function updateCoopHint() {
    const model = $("coop-model").value;
    const claude = model.startsWith("claude-");
    const local = isLocal(model);
    $("coop-thinking").disabled = !claude;
    $("coop-effort").disabled = !claude;
    for (const el of document.querySelectorAll("#coop-form .claude-only")) el.hidden = !claude;
    for (const el of document.querySelectorAll("#coop-form .local-only")) el.hidden = !local;
    let hint = "";
    let problem = false;
    if (local) {
      syncLocalControls("coop-local-thinking", "coop-local-effort", model, true);
      [hint, problem] = serverHint(model);
    }
    setText("coop-hint", hint);
    $("coop-hint").className = "small " + (problem ? "warn-text" : "muted");
  }

  $("coop-model").addEventListener("change", updateCoopHint);
  $("coop-local-thinking").addEventListener("change", updateCoopHint);

  $("coop-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    let cost = parseFloat($("coop-cost").value);
    if (!Number.isFinite(cost) || cost < 0) cost = 1.0;
    cost = Math.min(cost, 5.0);
    $("coop-cost").value = cost.toFixed(2);
    let idle = parseFloat($("coop-idle").value);
    if (!Number.isFinite(idle)) idle = 30;
    idle = Math.min(Math.max(idle, 5), 120);
    $("coop-idle").value = String(idle);
    const body = {
      model: $("coop-model").value,
      thinking: $("coop-thinking").value,
      effort: $("coop-effort").value,
      max_cost: cost,
      idle_minutes: idle,
      rules: $("coop-rules").checked,
      game_build: $("coop-build").value.trim() || null,
      ...localBody("coop", $("coop-model").value),
    };
    saveCoop();
    const data = await post($("coop-start"), "/api/coop/start", body);
    if (data) {
      toast(`Co-op agent started${data.pid ? " (pid " + data.pid + ")" : ""}`);
      setText("coop-status", "Co-op session running.");
      liveRunning = true;
      startPolling();
    }
  });

  $("coop-stop").addEventListener("click", async (ev) => {
    const data = await post(ev.currentTarget, "/api/coop/stop", {}, "Stopping the co-op agent");
    if (data) {
      setText("coop-status", data.pids && data.pids.length ? "Stopping." : "No co-op session was running.");
      setTimeout(startPolling, 1000);
    }
  });

  $("live-fresh").addEventListener("change", () => {
    liveShotUrl = null;
    refreshLive().catch(() => {});
  });

  async function knownNotes(ids) {
    if (!ids || !ids.length) return notesById || new Map();
    const missing = !notesById || ids.some((id) => !notesById.has(id));
    if (missing && !notesFetching) {
      notesFetching = api("/api/memory/notes?status=all")
        .then((d) => { notesById = new Map((d.notes || []).map((n) => [n.id, n])); })
        .catch(() => { notesById = notesById || new Map(); })
        .finally(() => { notesFetching = null; });
    }
    if (notesFetching) await notesFetching;
    return notesById || new Map();
  }

  async function refreshLive() {
    let live;
    try {
      live = await api("/api/live");
    } catch (e) {
      if (!(e instanceof AuthError)) setText("live-meta", e.message);
      throw e;
    }
    liveRunning = !!live.running;
    const status = $("live-status");
    status.textContent = live.run_id ? (live.running ? "running" : "stopped") : "no runs";
    status.className = "badge" + (live.running ? " ok" : "");

    if (!live.run_id) {
      setText("live-meta", "No runs yet.");
    } else {
      setText("live-meta", [`Run ${live.run_id}`, live.mode === "coop" ? "co-op" : live.kind, live.model].filter(Boolean).join(" · "));
    }
    setText("live-step", live.run_id ? dash(live.step) : "-");
    setText("live-cost", live.run_id ? money(live.cost_usd) : "-");

    const last = live.last;
    setText("live-time", last && last.time ? ago(last.time) : "-");

    // Screenshot: the last step's file, or a fresh capture of the display.
    const img = $("live-shot");
    let url = null;
    if ($("live-fresh").checked) url = "/api/screen.png?t=" + Date.now();
    else if (typeof live.screenshot === "string" && SHOT_URL.test(live.screenshot)) url = live.screenshot;
    if (url && url !== liveShotUrl) {
      liveShotUrl = url;
      img.src = url;
    }
    if (!url) {
      liveShotUrl = null;
      img.hidden = true;
      $("live-noshot").hidden = false;
      setText("live-noshot", "No screenshot yet.");
    }

    renderStep(
      { thought: "live-thought", actions: "live-actions", state: "live-state", error: "live-error", notes: "live-notes" },
      last || {},
    );
    if (!last) setText("live-thought", live.run_id ? "Waiting for the first step." : "");
    return live;
  }

  $("live-shot").addEventListener("load", () => {
    $("live-shot").hidden = false;
    $("live-noshot").hidden = true;
  });
  $("live-shot").addEventListener("error", () => {
    $("live-shot").hidden = true;
    $("live-noshot").hidden = false;
    setText("live-noshot", $("live-fresh").checked ? "No capture: the agent's display (Xvfb) is down." : "Screenshot not available.");
  });

  // Readable actions, mirroring sts_cua.actions.describe.
  function describeAction(a) {
    if (!a || typeof a !== "object") return String(a);
    const pt = (x, y) => `(${x}, ${y})`;
    switch (a.kind) {
      case "click": {
        const name = { 1: "click", 2: "double click", 3: "triple click" }[a.count || 1] || "click";
        const button = a.button && a.button !== "left" ? a.button + " " : "";
        const mods = a.modifiers ? ` with ${a.modifiers}` : "";
        return `${button}${name} at ${pt(a.x, a.y)}${mods}`;
      }
      case "drag": return `drag from ${pt(a.x0, a.y0)} to ${pt(a.x1, a.y1)}`;
      case "mouse_move": return `move mouse to ${pt(a.x, a.y)}`;
      case "scroll": return `scroll ${a.direction} ${a.amount}${a.x != null ? " at " + pt(a.x, a.y) : ""}`;
      case "key": return `press ${a.keys}${a.repeat > 1 ? " x" + a.repeat : ""}`;
      case "type": return `type ${JSON.stringify(a.text)}`;
      case "wait": return `wait ${a.seconds}s`;
      default: return JSON.stringify(a);
    }
  }

  function actionItems(list, emptyText) {
    if (!list || !list.length) return [h("li", { class: "muted", text: emptyText })];
    return list.map((a) => h("li", { text: describeAction(a) }));
  }

  // A summarized mod state (sts_cua.state.summarize) as a few readable lines.
  function stateView(st) {
    if (!st) return [h("p", { class: "muted", text: "No game state (mod not reachable)." })];
    const kv = [];
    const add = (label, value) => { if (value != null && value !== "") kv.push(h("span", null, h("span", { class: "muted", text: label + " " }), String(value))); };
    add("Screen", st.menu_screen ? `${st.state_type} / ${st.menu_screen}` : st.state_type);
    add("Act", st.act);
    add("Floor", st.floor);
    const p = st.player || {};
    if (p.hp != null) add("HP", `${p.hp}${p.max_hp != null ? "/" + p.max_hp : ""}`);
    if (p.block) add("Block", p.block);
    add("Energy", p.energy);
    add("Gold", p.gold);
    add("Round", st.round);
    add("Character", st.character);
    if (st.combat_ended) add("Combat", "ended");
    const out = [h("div", { class: "kv" }, kv)];
    if (st.enemies && st.enemies.length) {
      out.push(h("ul", null, st.enemies.map((e) => {
        const bits = [`${dash(e.name)}: ${dash(e.hp)}/${dash(e.max_hp)} HP`];
        if (e.block) bits.push(`${e.block} block`);
        if (e.intents && e.intents.length) bits.push("intent " + e.intents.map(dash).join(", "));
        return h("li", { text: bits.join(" · ") });
      })));
    }
    if (st.hand && st.hand.length) {
      out.push(h("p", { class: "small" }, h("span", { class: "muted", text: "Hand: " }),
        st.hand.map((c) => `${c.slot}. ${dash(c.name)} (${dash(c.cost)})`).join(", ")));
    }
    if (st.menu_options && st.menu_options.length) {
      out.push(h("p", { class: "small" }, h("span", { class: "muted", text: "Options: " }), st.menu_options.map(dash).join(", ")));
    }
    return out;
  }

  // Shared by the live panel and the replay: thought, actions, state, error, notes.
  async function renderStep(ids, rec) {
    setText(ids.thought, rec.thought || (rec.step != null && !rec.thought ? "(no thought)" : ""));
    $(ids.actions).replaceChildren(...actionItems(rec.executed, "none"));
    $(ids.state).replaceChildren(...stateView(rec.state));
    const err = $(ids.error);
    err.hidden = !rec.error;
    err.textContent = rec.error ? "Error: " + rec.error : "";
    const shown = Array.isArray(rec.notes_shown) ? rec.notes_shown : null;
    const notesEl = $(ids.notes);
    if (!shown) { notesEl.replaceChildren(h("li", { class: "muted", text: "memory off" })); return; }
    if (!shown.length) { notesEl.replaceChildren(h("li", { class: "muted", text: "none" })); return; }
    const token = (notesEl._token = (notesEl._token || 0) + 1);
    const byId = await knownNotes(shown);
    if (notesEl._token !== token) return;
    notesEl.replaceChildren(...shown.map((id) => {
      const note = byId.get(id);
      return h("li", null, h("span", { class: "nid", text: id }), note ? note.text : "(unknown note)");
    }));
  }

  // ---------- Results ----------

  let runsLimit = 50;
  let runsCache = [];

  async function loadRuns() {
    try {
      const [runs, status] = await Promise.all([
        api(`/api/runs?limit=${runsLimit}`),
        api("/api/status").catch((e) => { if (e instanceof AuthError) throw e; return null; }),
      ]);
      runsCache = runs.runs || [];
      if (status) budgetMeter($("res-budget"), status.budget);
      renderRuns();
    } catch (e) {
      report(e);
    }
  }

  function outcomeBadge(outcome) {
    const cls = { won: "ok", died: "bad", abandoned: "warn" }[outcome] || "";
    return h("span", { class: "badge " + cls, text: dash(outcome) });
  }

  function renderRuns() {
    const showReset = $("show-reset").checked;
    const rows = runsCache.filter((r) => showReset || r.kind !== "reset");
    const td = (label, content, cls) => h("td", { "data-label": label, class: cls || null }, content);
    $("runs-table").querySelector("tbody").replaceChildren(...rows.map((r) => {
      const open = () => { location.hash = "#results/" + encodeURIComponent(r.run_id); };
      return h("tr", {
        class: "clickable", tabindex: 0, "data-run": r.run_id, "data-kind": r.kind || "",
        onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); },
      },
        td("Time", localTime(r.started_at)),
        td("Kind", r.mode === "coop" ? "co-op" : dash(r.kind)),
        td("Model", dash(r.model)),
        td("Outcome", outcomeBadge(r.outcome)),
        td("Floor", dash(r.floor), "n"),
        td("Score", isNum(r.score) ? r.score.toFixed(1) : "-", "n"),
        td("Cost", money(r.cost_usd), "n"),
        td("Steps", dash(r.steps), "n"),
        td("End reason", dash(r.end_reason)));
    }));
    $("runs-empty").hidden = rows.length > 0;
    $("runs-more").hidden = runsCache.length < runsLimit;
  }

  // Evals: memory A/B experiments (sts_cua.eval)

  let evalOpen = null;
  const fixed = (v, digits) => (isNum(v) ? v.toFixed(digits) : "-");
  const pText = (v) => (isNum(v) ? (v < 0.001 ? "<0.001" : v.toFixed(3)) : "-");

  async function loadEvals() {
    try {
      const data = await api("/api/evals");
      const list = data.evals || [];
      const td = (label, content, cls) => h("td", { "data-label": label, class: cls || null }, content);
      $("evals-table").querySelector("tbody").replaceChildren(...list.map((e) => {
        const open = () => openEval(e.name);
        return h("tr", {
          class: "clickable" + (e.name === evalOpen ? " selected" : ""), tabindex: 0, "data-eval": e.name,
          onclick: open, onkeydown: (ev) => { if (ev.key === "Enter") open(); },
        },
          td("Name", e.name),
          td("Mode", dash(e.mode)),
          td("Comparison", (e.arms || []).slice().reverse().join(" vs ")),
          td("Runs", `${dash(e.valid_runs)}/${dash(e.planned_runs)}`, "n"),
          td("Cost", money(e.cost_usd), "n"),
          td("p", pText(e.p_value), "n"),
          td("Status", e.complete ? "complete" : dash(e.stop_reason || "not started")));
      }));
      $("evals-empty").hidden = list.length > 0;
      if (evalOpen && !list.some((e) => e.name === evalOpen)) { evalOpen = null; $("eval-detail").hidden = true; }
    } catch (e) {
      report(e);
    }
  }

  async function openEval(name) {
    evalOpen = name;
    for (const row of $("evals-table").querySelectorAll("tbody tr")) row.classList.toggle("selected", row.dataset.eval === name);
    const box = $("eval-detail");
    box.hidden = false;
    box.replaceChildren(h("p", { class: "muted", text: "Loading..." }));
    try {
      const data = await api(`/api/evals/${encodeURIComponent(name)}`);
      if (evalOpen !== name) return;
      const r = data.report;
      const td = (label, content, cls) => h("td", { "data-label": label, class: cls || null, text: content });
      const arms = r.arms.map((label) => r.per_arm[label]);
      const t = r.test;
      const testLine = t
        ? `${t.test === "wilcoxon_signed_rank" ? "Wilcoxon signed-rank" : "Mann-Whitney U"} (${t.method}): ` +
          `p = ${pText(t.p_value)}, rank-biserial r = ${fixed(t.effect_size, 2)}, ` +
          `mean difference ${fixed(t.mean_difference, 2)}, median difference ${fixed(t.median_difference, 2)}` +
          (t.pairs != null ? `, ${t.pairs} pairs` : `, ${t.n} runs`)
        : "No test yet: needs runs in both arms" + (r.mode === "paired" ? " on the same seed." : ".");
      const w = r.win_rate_test;
      box.replaceChildren(
        h("h3", { text: `${r.name}: ${r.comparison}` }),
        h("p", { class: "muted small", text: `${r.mode}, ${dash(r.character)}, A${dash(r.ascension)}, ` +
          `${r.valid_runs}/${r.planned_runs} valid runs, ${money(r.cost_usd)}` + (r.stop_reason ? `, last stop: ${r.stop_reason}` : "") }),
        h("table", { class: "cards" },
          h("thead", null, h("tr", null, ["Arm", "Runs", "Mean score", "Median", "Win rate", "Mean floor", "Cost"].map(
            (x, i) => h("th", { class: i ? "n" : null, text: x })))),
          h("tbody", null, arms.map((a) => h("tr", { "data-arm": a.arm },
            td("Arm", a.arm), td("Runs", `${a.runs}` + (a.invalid_runs ? ` (+${a.invalid_runs} invalid)` : ""), "n"),
            td("Mean score", fixed(a.mean_score, 1), "n"), td("Median", fixed(a.median_score, 1), "n"),
            td("Win rate", isNum(a.win_rate) ? `${(a.win_rate * 100).toFixed(0)}% (${a.wins})` : "-", "n"),
            td("Mean floor", fixed(a.mean_floor, 1), "n"), td("Cost", money(a.cost_usd), "n"))))),
        h("p", { class: "eval-test", text: testLine }),
        w ? h("p", { class: "muted small", text: `Win rate: ${w.test === "fisher_exact" ? "Fisher exact" : "exact McNemar"} p = ${pText(w.p_value)}` }) : null,
        h("ul", { class: "small" }, [...(r.notes || []), ...(r.warnings || [])].map((n) => h("li", { text: n }))),
      );
    } catch (e) {
      report(e);
      box.replaceChildren(h("p", { class: "error", text: e.message }));
    }
  }

  $("evals-refresh").addEventListener("click", loadEvals);

  $("show-reset").addEventListener("change", renderRuns);
  $("runs-refresh").addEventListener("click", loadRuns);
  $("runs-more").addEventListener("click", () => { runsLimit += 50; loadRuns(); });

  // Replay

  let replay = null; // {runId, total, index, steps: Map(index -> record), pages: Map(offset -> Promise)}

  function closeReplay() {
    replay = null;
    $("replay").hidden = true;
    $("results-list").hidden = false;
    loadRuns();
    loadEvals();
  }

  function kvList(obj, skip = []) {
    const entries = Object.entries(obj || {}).filter(([k]) => !skip.includes(k));
    if (!entries.length) return h("p", { class: "muted", text: "none" });
    return h("dl", { class: "kv-list" }, entries.map(([k, v]) => [
      h("dt", { text: k }),
      h("dd", { text: v != null && typeof v === "object" ? JSON.stringify(v) : dash(v) }),
    ]));
  }

  async function openReplay(runId) {
    $("results-list").hidden = true;
    $("replay").hidden = false;
    if (replay && replay.runId === runId) return;
    replay = { runId, total: 0, index: 0, steps: new Map(), pages: new Map() };
    setText("replay-title", `Run ${runId}`);
    $("replay-info").replaceChildren(h("p", { class: "muted", text: "Loading..." }));
    $("replay-combats").replaceChildren();
    clearStepView();
    const enc = encodeURIComponent(runId);
    try {
      const info = await api(`/api/runs/${enc}`);
      if (!replay || replay.runId !== runId) return;
      const score = info.score || null;
      const scoreBox = [h("h3", { text: "Score" })];
      if (score) {
        scoreBox.push(h("p", null, outcomeBadge(score.outcome), " ", h("strong", { text: isNum(score.score) ? score.score.toFixed(1) : "-" })));
        scoreBox.push(kvList(score, ["combats", "outcome", "score"]));
      } else scoreBox.push(h("p", { class: "muted", text: "not scored" }));
      $("replay-info").replaceChildren(
        h("div", null, h("h3", { text: "Meta" }), kvList(info.meta)),
        h("div", null, h("h3", { text: "Summary" }), info.summary ? kvList(info.summary) : h("p", { class: "muted", text: "running or unfinished" })),
        h("div", null, scoreBox),
      );
      if (score && score.combats && score.combats.length) {
        const td = (label, content, cls) => h("td", { "data-label": label, class: cls || null, text: content });
        $("replay-combats").replaceChildren(h("h3", { text: "Combats" }), h("table", { class: "cards" },
          h("thead", null, h("tr", null, ["Floor", "Room", "Enemies", "Result", "HP lost", "Steps"].map((t, i) => h("th", { class: i === 0 || i >= 4 ? "n" : null, text: t })))),
          h("tbody", null, score.combats.map((c) => h("tr", null,
            td("Floor", dash(c.floor), "n"), td("Room", dash(c.room)), td("Enemies", (c.enemies || []).join(", ") || "-"),
            td("Result", dash(c.result)), td("HP lost", dash(c.hp_lost), "n"),
            td("Steps", `${dash(c.steps)} (${dash(c.first_step)}-${dash(c.last_step)})`, "n"))))));
      }
    } catch (e) {
      report(e);
      $("replay-info").replaceChildren(h("p", { class: "error", text: e.message }));
    }
    await showStep(0);
  }

  function clearStepView() {
    setText("step-label", "-");
    setText("step-meta", "");
    setText("step-thought", "");
    $("step-shot").removeAttribute("src");
    $("step-shot-link").removeAttribute("href");
    $("step-actions").replaceChildren();
    $("step-state").replaceChildren();
    $("step-notes").replaceChildren();
    $("step-error").hidden = true;
    $("step-reasoning-box").hidden = true;
    $("step-proposed-box").hidden = true;
  }

  async function fetchStep(index) {
    const r = replay;
    if (r.steps.has(index)) return r.steps.get(index);
    const offset = Math.floor(index / STEP_PAGE) * STEP_PAGE;
    if (!r.pages.has(offset)) {
      r.pages.set(offset, api(`/api/runs/${encodeURIComponent(r.runId)}/steps?offset=${offset}&limit=${STEP_PAGE}`).then((d) => {
        r.total = d.total || 0;
        (d.steps || []).forEach((s, i) => r.steps.set(offset + i, s));
      }).catch((e) => { r.pages.delete(offset); throw e; }));
    }
    await r.pages.get(offset);
    return r.steps.get(index) || null;
  }

  async function showStep(index) {
    const r = replay;
    if (!r) return;
    if (r.total && (index < 0 || index >= r.total)) return;
    r.index = Math.max(0, index);
    let rec;
    try {
      rec = await fetchStep(r.index);
    } catch (e) {
      report(e);
      return;
    }
    if (replay !== r || r.index !== index) return;
    $("step-prev").disabled = r.index <= 0;
    $("step-next").disabled = r.index >= r.total - 1;
    const range = $("step-range");
    range.max = String(Math.max(0, r.total - 1));
    range.value = String(r.index);
    if (!rec) {
      clearStepView();
      setText("step-label", r.total ? "-" : "No steps");
      return;
    }
    setText("step-label", `Step ${dash(rec.step)} (${r.index + 1} of ${r.total})`);
    const meta = [localTime(rec.time)];
    if (rec.usage && isNum(rec.usage.cost_usd)) meta.push(money(rec.usage.cost_usd));
    if (rec.stop_reason) meta.push(`stop: ${rec.stop_reason}`);
    if (rec.done) meta.push("done");
    setText("step-meta", meta.join(" · "));

    const m = typeof rec.screenshot === "string" ? rec.screenshot.match(STEP_SHOT) : null;
    if (m) {
      const url = `/api/runs/${encodeURIComponent(r.runId)}/screenshots/${m[1]}`;
      $("step-shot").src = url;
      $("step-shot-link").href = url;
    } else {
      $("step-shot").removeAttribute("src");
      $("step-shot-link").removeAttribute("href");
    }

    $("step-reasoning-box").hidden = !rec.reasoning;
    setText("step-reasoning", rec.reasoning || "");
    const proposed = rec.proposed || [];
    const differs = JSON.stringify(proposed) !== JSON.stringify(rec.executed || []);
    $("step-proposed-box").hidden = !(proposed.length && differs);
    $("step-proposed").replaceChildren(...actionItems(proposed, "none"));
    renderStep({ thought: "step-thought", actions: "step-actions", state: "step-state", error: "step-error", notes: "step-notes" }, rec);
  }

  $("step-prev").addEventListener("click", () => replay && showStep(replay.index - 1));
  $("step-next").addEventListener("click", () => replay && showStep(replay.index + 1));
  $("step-range").addEventListener("input", (ev) => replay && showStep(parseInt(ev.target.value, 10)));

  document.addEventListener("keydown", (ev) => {
    if (!replay || currentTab !== "results" || $("replay").hidden) return;
    if ($("confirm").open) return;
    const tag = (ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      ev.preventDefault();
      showStep(replay.index + (ev.key === "ArrowLeft" ? -1 : 1));
    }
  });

  // ---------- Memory ----------

  async function loadNotes() {
    const status = $("notes-status").value;
    try {
      const data = await api(`/api/memory/notes?status=${encodeURIComponent(status)}`);
      const notes = data.notes || [];
      if (status === "all") notesById = new Map(notes.map((n) => [n.id, n]));
      setText("notes-path", data.path ? `File: ${data.path}` : "");
      renderNotes(notes);
    } catch (e) {
      report(e);
    }
  }

  function utilityText(u) {
    if (!u || u.mean == null) {
      const n = u && u.shown ? u.shown.n : 0;
      return n ? `not enough data (${n} shown)` : "-";
    }
    const sign = (v) => (v > 0 ? "+" : "") + v.toFixed(2);
    const range = u.lo != null && u.hi != null ? ` [${sign(u.lo)}, ${sign(u.hi)}]` : "";
    return `${sign(u.mean)}${range}`;
  }

  function renderNotes(notes) {
    const td = (label, content, cls) => h("td", { "data-label": label, class: cls || null }, h("span", null, content));
    const statusCls = { active: "ok", probation: "accent", retired: "" };
    $("notes-table").querySelector("tbody").replaceChildren(...notes.map((n) => {
      const keys = Object.entries(n.keys || {}).map(([k, v]) => h("span", { text: `${k}=${v}` }));
      const retire = n.status === "retired" ? null : h("button", {
        type: "button", class: "small", text: "Retire", "data-note": n.id,
        onclick: (ev) => retireNote(n, ev.currentTarget),
      });
      const u = n.utility || {};
      return h("tr", { class: n.status === "retired" ? "retired" : null, "data-note": n.id },
        td("ID", n.id, "num"),
        td("Status", h("span", { class: "badge " + (statusCls[n.status] || ""), text: dash(n.status) })),
        td("Kind", dash(n.kind)),
        td("Keys", keys.length ? keys : "-", "keys"),
        td("Text", n.text || "", "text wide"),
        td("Shown", dash(n.shown_n), "n"),
        td("Utility", h("span", { title: u.hidden ? `shown n=${(u.shown || {}).n || 0}, hidden n=${u.hidden.n || 0}; > 0 means it helped` : null, text: utilityText(u) })),
        td("", retire));
    }));
    $("notes-empty").hidden = notes.length > 0;
  }

  async function retireNote(note, button) {
    const ok = await confirmDialog(
      `Retire note ${note.id}?`,
      `The agent stops seeing it. Notes are never deleted, so old run logs still resolve it.\n\n"${note.text || ""}"`,
      "Retire",
    );
    if (!ok) return;
    const data = await post(button, `/api/memory/notes/${encodeURIComponent(note.id)}/retire`, {}, `Retired ${note.id}`);
    if (data) loadNotes();
  }

  async function loadReflectRuns() {
    try {
      const data = await api("/api/runs?limit=50");
      const sel = $("reflect-run");
      const prev = sel.value;
      const runs = (data.runs || []).filter((r) => r.kind !== "reset");
      sel.replaceChildren(...(runs.length
        ? runs.map((r) => h("option", { value: r.run_id, text: `${r.run_id} · ${dash(r.model)} · ${dash(r.outcome)} · floor ${dash(r.floor)}` }))
        : [h("option", { value: "", text: "no runs" })]));
      if (prev && runs.some((r) => r.run_id === prev)) sel.value = prev;
      $("reflect-go").disabled = !runs.length;
    } catch (e) {
      report(e);
    }
  }

  $("reflect-go").addEventListener("click", async (ev) => {
    const runId = $("reflect-run").value;
    if (!runId) return;
    await post(ev.currentTarget, "/api/memory/reflect", { run_id: runId }, `Reflecting on ${runId}; refresh the notes in a minute`);
  });
  $("notes-status").addEventListener("change", loadNotes);
  $("notes-refresh").addEventListener("click", loadNotes);

  // ---------- Bake-off ----------
  // Each button runs one `python -m sts_cua.bench` job in the background (one at a time); the job card tails it.

  const ACTION_LABELS = {
    extract: "Extract targets", review: "Contact sheets", delete: "Delete targets", grounding: "Grounding",
    baseline: "Baseline", resources: "Resources", clicks: "Clicks", write_docs: "Write to docs", external: "Bench job",
  };
  const BENCH_KEY = "sts-cua.bench-form";
  const benchFields = ["bench-mode", "bench-limit", "bench-serve", "bench-res-model", "bench-res-mode", "bench-res-game",
    "bench-res-serve", "bench-clicks-model", "bench-clicks-n"];

  let lastBench = null;
  let benchRunning = false;
  let benchModelsKey = "";
  let benchSheetsKey = "";
  let benchTargetsKey = "";
  let benchChecked = new Set(); // grounding models ticked
  const benchSelected = new Set(); // target ids ticked for deletion

  const pct = (v) => (isNum(v) ? `${Math.round(v * 100)}%` : "-");
  const secs = (a, b) => `${fixed(a, 2)} / ${fixed(b, 2)} s`;

  function saveBench() {
    try {
      const values = { models: [...benchChecked] };
      for (const id of benchFields) values[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
      localStorage.setItem(BENCH_KEY, JSON.stringify(values));
    } catch (e) { /* storage unavailable */ }
  }

  function restoreBench() {
    try {
      const values = JSON.parse(localStorage.getItem(BENCH_KEY) || "{}");
      if (Array.isArray(values.models)) benchChecked = new Set(values.models.filter((m) => typeof m === "string"));
      for (const id of benchFields) if (id in values) setField(id, values[id]);
    } catch (e) { /* storage unavailable */ }
  }

  async function refreshBench() {
    let d;
    try {
      d = await api("/api/bench");
    } catch (e) {
      if (!(e instanceof AuthError)) setText("bench-job-line", e.message);
      throw e;
    }
    const was = benchRunning;
    benchRunning = !!(d.job && d.job.running);
    lastBench = d;
    renderBench(d);
    if (was && !benchRunning) loadBenchResults();
    return d;
  }

  function exitText(last) {
    if (last.running) return "running";
    const code = last.exit_code;
    if (!isNum(code)) return "finished";
    if (code === 0) return "finished";
    return code < 0 ? `stopped (signal ${-code})` : `failed (exit ${code})`;
  }

  function renderBench(d) {
    renderBenchJob(d);
    renderBenchTargets(d.targets || {});
    renderBenchModels(d);
    renderBenchBaselines(d.baselines || {});
    if (d.clicks_display) setText("bench-clicks-display", d.clicks_display);
    updateBenchHints();
  }

  function renderBenchJob(d) {
    const job = d.job || {};
    const last = d.last;
    const badge = $("bench-job-badge");
    let state = "idle";
    let cls = "";
    if (job.running) { state = "running"; cls = "accent"; }
    else if (last) {
      state = exitText(last);
      cls = state === "finished" ? "ok" : state.startsWith("failed") ? "bad" : "warn";
    }
    badge.textContent = state;
    badge.className = "badge " + cls;
    $("bench-stop").disabled = !job.running;

    const line = $("bench-job-line");
    if (job.running) {
      const what = ACTION_LABELS[job.action] || dash(job.action);
      line.replaceChildren(`${what}, started ${ago(job.since)}${job.pid ? " · pid " + job.pid : ""}`
        + (job.external ? " (started outside the app; its output isn't shown here)" : ""));
    } else if (last) {
      line.replaceChildren(`Last: ${ACTION_LABELS[last.action] || dash(last.action)}, ${localTime(last.started_at)}, ${exitText(last)}. `);
    } else {
      line.replaceChildren("No bench job has run yet. Each button below runs one ", h("code", { text: "python -m sts_cua.bench" }), " job; one at a time.");
    }
    if (last && Array.isArray(last.argv) && !(job.running && job.external)) {
      line.append(h("code", { class: "argv", text: ["python", ...last.argv.slice(1)].join(" ") }));
    }
    line.className = "small" + (job.running ? "" : " muted");

    const pre = $("bench-output");
    const lines = (last && last.output) || [];
    const show = !!last && !(job.running && job.external);
    pre.hidden = !show;
    if (show) {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      const text = lines.length ? lines.join("\n") : "(no output yet)";
      if (pre.textContent !== text) {
        pre.textContent = text;
        if (atBottom || job.running) pre.scrollTop = pre.scrollHeight;
      }
    }

    const box = $("bench-changed");
    const ch = last && last.action === "write_docs" && !last.running ? last : null;
    box.hidden = !ch;
    if (ch) {
      if (!ch.changed) {
        box.replaceChildren(h("p", { class: "muted small", text: "The report didn't say what it changed; see Logs > bench." }));
      } else {
        const rows = ch.changed.rows || [];
        const diff = ch.changed.diff || [];
        box.replaceChildren(
          h("p", { class: "small", text: rows.length ? `Measured rows filled in docs/hosting.md: ${rows.join(", ")}.`
            : "No measure rows filled; only the bench block was rewritten." }),
          diff.length
            ? h("pre", { class: "log-tail diff" }, diff.map((l) => h("span", { class: l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : null, text: l + "\n" })))
            : h("p", { class: "muted small", text: "docs/hosting.md is unchanged." }),
        );
      }
    }
  }

  function renderBenchTargets(t) {
    setText("bench-target-count", `${t.count || 0} targets` + (t.rejected ? ` · ${t.rejected} deleted` : ""));
    let hint = "";
    let warn = false;
    const sheets = t.sheets || [];
    if (t.error) { hint = t.error; warn = true; }
    else if (!t.count) hint = "No targets yet: Extract targets reads the Claude runs in runs/.";
    else if (t.sheets_stale) { hint = "The contact sheets are older than the targets: Make contact sheets to see the new ones."; warn = true; }
    else if (!sheets.length) hint = "No contact sheets yet: Make contact sheets.";
    const hintEl = $("bench-target-hint");
    hintEl.hidden = !hint;
    hintEl.textContent = hint;
    hintEl.className = "small " + (warn ? "warn-text" : "muted");

    const urls = sheets.filter((s) => typeof s.url === "string" && SHEET_URL.test(s.url));
    const key = urls.map((s) => s.url).join(" ");
    if (key !== benchSheetsKey) {
      benchSheetsKey = key;
      $("bench-sheets").replaceChildren(...urls.map((s) => h("figure", { class: "sheet" },
        h("a", { href: s.url, target: "_blank", rel: "noopener" }, h("img", { src: s.url, alt: `Contact sheet ${s.name}`, loading: "lazy" })),
        h("figcaption", { class: "muted small", text: s.name }))));
    }

    const items = t.items || [];
    const tkey = items.map((i) => i.id).join(" ");
    if (tkey !== benchTargetsKey) {
      benchTargetsKey = tkey;
      for (const id of [...benchSelected]) if (!items.some((i) => i.id === id)) benchSelected.delete(id);
      $("bench-target-list").replaceChildren(...items.map((i) => h("li", { "data-target": i.id },
        h("label", { class: "check small" },
          h("input", { type: "checkbox", value: i.id, checked: benchSelected.has(i.id), onchange: (ev) => {
            if (ev.currentTarget.checked) benchSelected.add(i.id); else benchSelected.delete(i.id);
            updateDeleteButton();
          } }),
          h("span", { class: "nid", text: i.id }),
          h("span", { text: i.instruction || "" })))));
      filterTargets();
    }
    updateDeleteButton();
  }

  function filterTargets() {
    const q = $("bench-filter").value.trim().toLowerCase();
    for (const li of $("bench-target-list").children) li.hidden = !!q && !li.textContent.toLowerCase().includes(q);
  }

  function updateDeleteButton() {
    const n = benchSelected.size;
    $("bench-delete").disabled = !n || benchRunning;
    $("bench-delete").textContent = n ? `Delete ${n} selected` : "Delete selected";
  }

  function modelNote(m) {
    const fits = ["coop", "solo", "max"].filter((mode) => m.fits && m.fits[mode]).map((mode) => (mode === "coop" ? "co-op" : mode));
    return `${m.need_gib.toFixed(1)} GiB · fits ${fits.join(", ") || "none"}${m.downloaded ? "" : " · not downloaded"}`;
  }

  function renderBenchModels(d) {
    const models = d.models || { local: [], reference: [] };
    const key = JSON.stringify(models);
    if (key === benchModelsKey) return;
    benchModelsKey = key;
    const local = models.local || [];
    const refs = models.reference || [];
    const check = (name, note, api) => h("label", { class: "check model-check" },
      h("input", { type: "checkbox", value: name, checked: benchChecked.has(name), onchange: (ev) => {
        if (ev.currentTarget.checked) benchChecked.add(name); else benchChecked.delete(name);
        saveBench();
        updateBenchHints();
      } }),
      h("span", null, h("span", { class: "mname", text: name }), " ",
        api ? h("span", { class: "badge warn", text: "API credits" }) : null,
        h("span", { class: "muted small mnote", text: note })));
    $("bench-models").replaceChildren(
      ...local.map((m) => check(m.name, modelNote(m), false)),
      ...refs.map((name) => check(name, "Claude reference", true)));

    const fill = (id, options) => {
      const sel = $(id);
      const keep = sel.dataset.want || sel.value;
      sel.replaceChildren(...options.map(([value, text]) => h("option", { value, text })));
      if (options.some(([value]) => value === keep)) sel.value = keep;
      delete sel.dataset.want;
    };
    fill("bench-res-model", local.map((m) => [m.name, m.name + (m.downloaded ? "" : " (not downloaded)")]));
    fill("bench-clicks-model", [...local.map((m) => [m.name, m.name]), ...refs.map((n) => [n, `${n} (API credits)`])]);
  }

  function renderBenchBaselines(b) {
    $("bench-baselines").replaceChildren(...["desktop", "pre-server"].map((label) => {
      const x = b[label];
      const text = x
        ? `${localTime(x.time)}` + (isNum(x.gpu_used_mib) ? ` · GPU ${gib(x.gpu_used_mib)} GiB` : " · no GPU reading")
          + (isNum(x.ram_used_mib) ? ` · RAM ${gib(x.ram_used_mib)} GiB` : "") + (x.mode ? ` · ${x.mode}` : "")
        : "not taken yet";
      return h("li", { "data-baseline": label }, h("span", { class: "mname", text: label + ": " }), h("span", { class: x ? "" : "muted", text }));
    }));
  }

  const benchLocalNames = () => new Set(((lastBench && lastBench.models && lastBench.models.local) || []).map((m) => m.name));

  // What the model server does for a job on `model` without "start it for me": [text, problem].
  function benchServerHint(model, untick) {
    const srv = (lastBench && lastBench.server) || {};
    if (!srv.running) return [`No model server is running: start ${model} in the Control tab${untick ? ", or tick " + untick : ""}.`, true];
    if (srv.model && srv.model !== model) return [`The model server is serving ${srv.model}, not ${model}.`, true];
    return [`Uses the running model server (${dash(srv.model)}).`, false];
  }

  function setHint(id, parts, problem) {
    setText(id, parts.filter(Boolean).join(" "));
    $(id).className = "small " + (problem ? "warn-text" : "muted");
  }

  function updateBenchHints() {
    if (!lastBench) return;
    const locals = benchLocalNames();
    const srv = lastBench.server || {};
    const running = benchRunning;
    for (const id of ["bench-extract", "bench-review", "bench-grounding-run", "bench-baseline-desktop",
      "bench-baseline-pre", "bench-res-run", "bench-clicks-run", "bench-write-docs"]) $(id).disabled = running;
    updateDeleteButton();
    const busyNote = running ? "A bench job is running; wait for it or stop it." : "";

    // Grounding
    const picked = [...$("bench-models").querySelectorAll("input:checked")].map((i) => i.value);
    const local = picked.filter((m) => locals.has(m));
    const refs = picked.filter((m) => !locals.has(m));
    const serve = $("bench-serve").checked;
    const parts = [busyNote];
    let problem = false;
    if (!picked.length) { parts.push("Pick at least one model."); problem = true; }
    if (refs.length) parts.push(`${refs.join(" and ")} cost${refs.length === 1 ? "s" : ""} API credits (one request per target).`);
    if (local.length && serve) {
      parts.push(`Starts each local model in ${$("bench-mode").value} mode in turn, stopping the model server between them`
        + (srv.running ? ` (${dash(srv.model)} first).` : "."));
    } else if (local.length > 1) {
      parts.push("Only one model server runs at a time: tick Start each model for me."); problem = true;
    } else if (local.length === 1) {
      const [text, bad] = benchServerHint(local[0], "Start each model for me");
      parts.push(text); problem = problem || bad;
    }
    setHint("bench-grounding-hint", parts, problem);
    $("bench-grounding-run").disabled = running || !picked.length;

    // Resources
    const resModel = $("bench-res-model").value;
    const resParts = [busyNote];
    let resProblem = false;
    if (!lastBench.baselines || !lastBench.baselines["pre-server"]) resParts.push("No pre-server baseline yet: the model's VRAM will be missing.");
    if ($("bench-res-serve").checked) {
      if (srv.running) { resParts.push(`Stop the model server (${dash(srv.model)}) first, or untick Start it for me.`); resProblem = true; }
      else resParts.push(`Starts ${resModel} in ${$("bench-res-mode").value} mode, measures, and leaves it running.`);
    } else if (resModel) {
      const [text, bad] = benchServerHint(resModel, "Start it for me");
      resParts.push(text); resProblem = bad;
    }
    if (srv.running) resParts.push("Baselines need the model server stopped.");
    setHint("bench-res-hint", resParts, resProblem);

    // Clicks
    const clicksModel = $("bench-clicks-model").value;
    const clickParts = [busyNote];
    let clickProblem = false;
    if (locals.has(clicksModel)) {
      const [text, bad] = benchServerHint(clicksModel, "");
      clickParts.push(text); clickProblem = bad;
    } else if (clicksModel) clickParts.push(`${clicksModel} costs API credits (one request per click).`);
    setHint("bench-clicks-hint", clickParts, clickProblem);
  }

  async function benchPost(button, action, body, message) {
    const data = await post(button, `/api/bench/${action}`, body, message);
    if (data) {
      benchRunning = true;
      startPolling(); // refresh now, then every 2 s while it runs
    }
    return data;
  }

  $("bench-extract").addEventListener("click", (ev) => benchPost(ev.currentTarget, "extract", {}, "Extracting targets from the Claude runs"));
  $("bench-review").addEventListener("click", (ev) => benchPost(ev.currentTarget, "review", {}, "Making contact sheets"));
  $("bench-filter").addEventListener("input", filterTargets);
  $("bench-delete").addEventListener("click", async (ev) => {
    const ids = [...benchSelected];
    if (!ids.length) return;
    const ok = await confirmDialog(`Delete ${ids.length} target${ids.length === 1 ? "" : "s"}?`,
      `They are removed from bench/targets.jsonl with their images, and remembered in bench/rejected.txt so Extract never brings them back. The contact sheets are remade.\n\n${ids.slice(0, 12).join("\n")}${ids.length > 12 ? "\n..." : ""}`,
      "Delete");
    if (!ok) return;
    const data = await benchPost(ev.currentTarget, "delete", { ids }, `Deleting ${ids.length} target${ids.length === 1 ? "" : "s"}`);
    if (data) benchSelected.clear();
  });

  $("bench-grounding").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const locals = benchLocalNames();
    const models = [...$("bench-models").querySelectorAll("input:checked")].map((i) => i.value);
    const refs = models.filter((m) => !locals.has(m));
    const limit = parseInt($("bench-limit").value, 10);
    const n = lastBench && lastBench.targets ? lastBench.targets.count : 0;
    const per = Number.isFinite(limit) && limit > 0 ? Math.min(limit, n || limit) : n;
    if (refs.length) {
      const ok = await confirmDialog("This costs API credits",
        `${refs.join(" and ")} will get one request per target (${per} each).`, "Run grounding");
      if (!ok) return;
    }
    saveBench();
    await benchPost($("bench-grounding-run"), "grounding", {
      models, mode: $("bench-mode").value, serve: $("bench-serve").checked,
      limit: Number.isFinite(limit) && limit > 0 ? limit : null,
    }, `Grounding ${models.join(", ")}`);
  });
  for (const id of ["bench-serve", "bench-mode", "bench-res-model", "bench-res-mode", "bench-res-serve", "bench-res-game",
    "bench-clicks-model", "bench-clicks-n", "bench-limit"]) {
    $(id).addEventListener("change", () => { saveBench(); updateBenchHints(); });
  }

  $("bench-baseline-desktop").addEventListener("click", (ev) => benchPost(ev.currentTarget, "baseline", { label: "desktop" }, "Taking the desktop baseline"));
  $("bench-baseline-pre").addEventListener("click", (ev) => benchPost(ev.currentTarget, "baseline", { label: "pre-server", mode: $("bench-res-mode").value }, "Taking the pre-server baseline"));
  $("bench-res-run").addEventListener("click", (ev) => {
    saveBench();
    benchPost(ev.currentTarget, "resources", {
      model: $("bench-res-model").value, mode: $("bench-res-mode").value,
      game: $("bench-res-game").checked, serve: $("bench-res-serve").checked,
    }, `Measuring ${$("bench-res-model").value}`);
  });
  $("bench-clicks-run").addEventListener("click", (ev) => {
    let n = parseInt($("bench-clicks-n").value, 10);
    if (!Number.isFinite(n)) n = 20;
    n = Math.min(Math.max(n, 1), 100);
    $("bench-clicks-n").value = String(n);
    saveBench();
    benchPost(ev.currentTarget, "clicks", { model: $("bench-clicks-model").value, n }, `Clicking with ${$("bench-clicks-model").value}`);
  });
  $("bench-write-docs").addEventListener("click", async (ev) => {
    const ok = await confirmDialog("Write the results to docs/hosting.md?",
      "Rewrites the bench block (\"Measured on the PC\") and fills the **measure** rows that now have a measurement. Other text stays. The changed lines show in the job card.",
      "Write to docs");
    if (!ok) return;
    benchPost(ev.currentTarget, "write-docs", {}, "Writing the report to docs/hosting.md");
  });
  $("bench-stop").addEventListener("click", async (ev) => {
    const data = await post(ev.currentTarget, "/api/bench/stop", {}, "Stopping the bench job");
    if (data) setTimeout(startPolling, 1000);
  });
  $("bench-results-refresh").addEventListener("click", loadBenchResults);

  async function loadBenchResults() {
    try {
      renderBenchResults(await api("/api/bench/results"));
    } catch (e) {
      report(e);
    }
  }

  function fitShort(f) {
    if (!f || f.how === "api") return "API";
    return `${f.fits ? "yes" : "no"} (${f.how} ${fixed(f.need_gib, 1)} GiB)`;
  }

  function renderBenchResults(r) {
    const ranking = r.ranking || [];
    const rec = r.recommended || {};
    setText("bench-recommended", ranking.length
      ? `Recommended: co-op ${rec.coop || "none that fits"}, solo ${rec.solo || "none that fits"}.`
      : "No grounding results yet: run Grounding.");
    $("bench-ranking").replaceChildren(...ranking.map((x) => h("li", { "data-model": x.model },
      h("strong", { text: x.model }), x.local ? null : h("span", { class: "badge", text: "API" }),
      h("span", { text: ` ${pct(x.hit_rate)} hits, ${fixed(x.latency_p50_s, 2)} s median · co-op ${fitShort(x.fits && x.fits.coop)}, solo ${fitShort(x.fits && x.fits.solo)}` }))));

    const td = (label, content, cls) => h("td", { "data-label": label, class: cls || null, text: content });
    const g = r.grounding || [];
    $("bench-grounding-table").querySelector("tbody").replaceChildren(...g.map((x) => h("tr", { "data-model": x.model },
      td("Model", x.model + (x.local ? "" : " (API)")), td("Measured", localTime(x.created_at)),
      td("Hit rate", `${pct(x.hit_rate)} (${dash(x.hits)}/${dash(x.answered)})`, "n"),
      td("Median error", isNum(x.median_error_px) ? `${x.median_error_px.toFixed(1)} px` : "-", "n"),
      td("Latency p50 / p95", secs(x.latency_p50_s, x.latency_p95_s), "n"),
      td("Tokens/s", fixed(x.tokens_per_s, 1), "n"), td("Malformed", dash(x.malformed), "n"))));
    $("bench-grounding-table").hidden = !g.length;
    $("bench-grounding-empty").hidden = g.length > 0;

    const c = r.clicks || [];
    $("bench-clicks-box").hidden = !c.length;
    $("bench-clicks-table").querySelector("tbody").replaceChildren(...c.map((x) => h("tr", { "data-model": x.model },
      td("Model", x.model), td("Measured", localTime(x.created_at)), td("Hits", `${dash(x.hits)}/${dash(x.answered)}`, "n"),
      td("Latency p50 / p95", secs(x.latency_p50_s, x.latency_p95_s), "n"), td("Malformed", dash(x.malformed), "n"))));

    const res = r.resources || [];
    $("bench-res-box").hidden = !res.length;
    const g1 = (v) => (isNum(v) ? `${v.toFixed(1)} GiB` : "-");
    $("bench-res-table").querySelector("tbody").replaceChildren(...res.map((x) => h("tr", { "data-model": x.model },
      td("Model", x.model), td("Mode", x.mode === "coop" ? "co-op" : dash(x.mode)), td("Measured", localTime(x.created_at)),
      td("GPU used", `${fixed(x.vram_used_gib, 1)} / ${fixed(x.vram_total_gib, 1)} GiB`, "n"),
      td("Model VRAM", g1(x.model_vram_gib), "n"), td("Budget", g1(x.budget_gib), "n"),
      td("Fits", x.fits == null ? "-" : x.fits ? "yes" : "no"),
      td("Server RSS", g1(x.server_rss_gib), "n"), td("Game RSS", g1(x.game_rss_gib), "n"))));

    $("bench-markdown-box").hidden = !r.markdown;
    $("bench-markdown").textContent = r.markdown || "";
  }

  // ---------- Logs drawer ----------

  let logTimer = null;

  async function loadLogs() {
    clearTimeout(logTimer);
    if ($("logs").hidden || !authed) return;
    const name = $("log-name").value;
    const pre = $("log-text");
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    try {
      const data = await api(`/api/logs/${encodeURIComponent(name)}?lines=${encodeURIComponent($("log-lines").value)}`);
      if ($("log-name").value !== name) return;
      const lines = data.lines || [];
      pre.textContent = lines.length ? lines.join("\n") : "(empty)";
      if (atBottom || $("log-follow").checked) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      if (!(e instanceof AuthError)) pre.textContent = e.message;
    }
    if ($("log-follow").checked && !$("logs").hidden && !document.hidden && authed) logTimer = setTimeout(loadLogs, 3000);
  }

  function openLogs() {
    $("logs").hidden = false;
    $("log-text").textContent = "Loading...";
    loadLogs();
  }

  function closeLogs() {
    clearTimeout(logTimer);
    $("logs").hidden = true;
  }

  $("logs-open").addEventListener("click", () => ($("logs").hidden ? openLogs() : closeLogs()));
  $("logs-close").addEventListener("click", closeLogs);
  $("log-refresh").addEventListener("click", loadLogs);
  $("log-name").addEventListener("change", () => { $("log-text").textContent = "Loading..."; loadLogs(); });
  $("log-lines").addEventListener("change", loadLogs);
  $("log-follow").addEventListener("change", loadLogs);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !$("logs").hidden && !$("confirm").open) closeLogs();
  });

  // ---------- Start ----------

  restoreForm();
  updateRunHint();
  restoreCoop();
  updateCoopHint();
  restoreBench();
  route();
})();
