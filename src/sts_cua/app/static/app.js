// sts-cua control app. Plain JS, no build step, no external requests.
// Every string from the API goes into the page through textContent or attributes,
// never innerHTML: thoughts and notes are model output.
"use strict";

(() => {
  const PROCESSES = ["steam", "xvfb", "game", "vllm", "supervisor"];
  const TABS = ["control", "run", "results", "memory"];
  const STEP_PAGE = 50;
  const SHOT_URL = /^\/api\/runs\/[\w.-]+\/screenshots\/[\w.-]+\.png$/;
  const STEP_SHOT = /^screenshots\/([\w.-]+\.png)$/;

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
    control: { fn: () => refreshStatus(), ms: () => 3000 },
    run: { fn: () => refreshLive(), ms: () => (liveRunning ? 2000 : 10000) },
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
    for (const id of ["stack-up", "stack-down", "vllm-start", "vllm-stop"]) $(id).disabled = !!busy;
    const vllmOn = !!(procs.vllm && procs.vllm.running);
    if (!busy) {
      $("vllm-start").disabled = vllmOn;
      $("vllm-stop").disabled = !vllmOn;
    }
  }

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
  $("vllm-start").addEventListener("click", async (ev) => {
    const model = $("vllm-model").value;
    await post(ev.currentTarget, "/api/vllm/start", { model }, `Starting vLLM (${model}); loading takes a minute or two`);
    refreshStatus().catch(() => {});
  });
  $("vllm-stop").addEventListener("click", async (ev) => {
    await post(ev.currentTarget, "/api/vllm/stop", {}, "Stopping vLLM");
    refreshStatus().catch(() => {});
  });
  $("wsl-shutdown").addEventListener("click", async (ev) => {
    const ok = await confirmDialog(
      "Shut down WSL?",
      "This runs wsl --shutdown: it stops any run, vLLM, the game, Steam and this app, and frees all of WSL's GPU memory and RAM for gaming on Windows. This page stops working until WSL and the app are started again from Windows.",
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
  const formFields = ["run-model", "run-thinking", "run-effort", "run-budget", "run-maxruns", "run-build", "run-memory"];

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
        if ($(id).type === "checkbox") $(id).checked = !!values[id];
        else $(id).value = values[id];
      }
    } catch (e) { /* storage unavailable */ }
  }

  function updateRunHint() {
    const model = $("run-model").value;
    const claude = model.startsWith("claude-");
    $("run-thinking").disabled = !claude;
    $("run-effort").disabled = !claude;
    let hint = "";
    if (model === "qwen3-vl-8b") hint = "Needs vLLM running (Control tab). Local runs cost nothing.";
    else if (model === "scripted") hint = "Scripted runs make no model calls.";
    else hint = "Thinking and effort apply to Claude models only.";
    setText("run-hint", hint);
  }

  $("run-model").addEventListener("change", updateRunHint);

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
  const coopFields = ["coop-model", "coop-thinking", "coop-effort", "coop-cost", "coop-idle", "coop-build", "coop-rules"];

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
        if ($(id).type === "checkbox") $(id).checked = !!values[id];
        else $(id).value = values[id];
      }
    } catch (e) { /* storage unavailable */ }
  }

  function updateCoopHint() {
    const claude = $("coop-model").value.startsWith("claude-");
    $("coop-thinking").disabled = !claude;
    $("coop-effort").disabled = !claude;
  }

  $("coop-model").addEventListener("change", updateCoopHint);

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
  route();
})();
