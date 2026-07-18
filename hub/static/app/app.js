/* SAATHI caregiver PWA (§7 P1). Plain JS, no lib: one state object, targeted
 * re-renders on every WS message. Server is the source of truth.
 * XSS rule (§18): all dynamic strings rendered via textContent, never innerHTML. */
"use strict";

const state = {
  connected: false,
  status: null,      // StatusResponse from /api/status or WS "status"
  alerts: [],        // newest first
  activeAlert: null, // OPEN/ANNOUNCED/ESCALATED alert → full-screen overlay
  digest: null,
  lastMotionTs: null, // client-tracked from telemetry samples
  acking: false,
  armed: false,      // guardian mode (Phase 3b) — vibrate while an alert is active
};

const $ = (id) => document.getElementById(id);
const ACTIVE_STATES = ["OPEN", "ANNOUNCED", "ESCALATED"];

/* --- §7 shared pattern: fetch wrapper (10 s timeout, JSON errors → toast) --- */

async function api(path, opts = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10000);
  try {
    const resp = await fetch(path, { ...opts, signal: ctrl.signal });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error((body.error && body.error.message) || `HTTP ${resp.status}`);
      err.status = resp.status;
      err.code = body.error && body.error.code;
      throw err;
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
}

/* --- §7 shared pattern: WS with auto-reconnect (3 s backoff) ---
 * Zombie-tab hardening (live finding 2026-07-12 11:08): a network switch kills
 * the socket WITHOUT a close frame, so the app showed hour-old "ALL OK" while
 * a real alert was firing. The server pushes status every 5 s — 12 s of
 * silence therefore means a dead pipe: force-close (→ reconnect bar + retry),
 * and resync full state on every (re)open so no alert is ever missed. */

function connectWS(url, onMessage) {
  let ws;
  let lastMsgTs = Date.now();
  const open = () => {
    ws = new WebSocket(url);
    ws.onopen = () => {
      state.connected = true;
      lastMsgTs = Date.now();
      renderConnection();
      refreshState(); // resync anything missed while the pipe was down
    };
    ws.onmessage = (e) => {
      lastMsgTs = Date.now();
      onMessage(JSON.parse(e.data));
    };
    ws.onclose = () => {
      state.connected = false;
      renderConnection();
      setTimeout(open, 3000);
    };
    ws.onerror = () => ws.close();
  };
  setInterval(() => {
    if (state.connected && Date.now() - lastMsgTs > 12000) {
      ws.close(); // silent death — surface it instead of showing stale calm
    }
  }, 3000);
  open();
}

async function refreshState() {
  try {
    applyStatus(await api("/api/status"));
    const hist = await api("/api/alerts?limit=20");
    for (const a of hist.alerts.reverse()) upsertAlert(a);
  } catch {
    // unreachable hub: the reconnect bar / watchdog already tell that story
  }
}

/* --- WS message handling --- */

function onWsMessage(msg) {
  if (msg.type === "status") {
    applyStatus(msg.status);
  } else if (msg.type === "alert.created" || msg.type === "alert.updated") {
    upsertAlert(msg.alert);
  } else if (msg.type === "digest") {
    state.digest = msg.digest;
    renderDigest();
  }
}

function applyStatus(status) {
  state.status = status;
  if (status.telemetry && status.telemetry.motion) {
    state.lastMotionTs = status.telemetry.ts;
  }
  if (status.active_alert) {
    upsertAlert(status.active_alert);
  } else if (state.activeAlert) {
    // server says nothing is active any more — drop the overlay
    state.activeAlert = null;
    renderOverlay();
    syncVibration();
  }
  renderHome();
}

function upsertAlert(alert) {
  const i = state.alerts.findIndex((a) => a.id === alert.id);
  if (i >= 0) state.alerts[i] = alert; else state.alerts.unshift(alert);
  state.alerts.sort((a, b) => b.created_ts - a.created_ts);

  if (ACTIVE_STATES.includes(alert.state)) {
    state.activeAlert = alert;
  } else if (state.activeAlert && state.activeAlert.id === alert.id) {
    state.activeAlert = null;
  }
  renderAlerts();
  renderOverlay();
  renderHome();
  syncVibration();
}

/* --- guardian mode (Phase 3b): vibration ONLY, no audio ---
 * Browsers require a user gesture before navigator.vibrate() works — that is
 * what the arming tap is for. Vibration also stops the moment the page leaves
 * the screen (OS/browser rule, not ours): the interval below keeps re-issuing
 * the pattern so it resumes as soon as the app is visible again. */

const VIBE_PATTERN = [500, 200, 500, 200, 900]; // strong triple buzz, ~2.3 s
const VIBE_REPEAT_MS = 2600;
let vibeTimer = null;

function vibrationSupported() {
  return "vibrate" in navigator;
}

function syncVibration() {
  if (state.armed && state.activeAlert) startVibration();
  else stopVibration();
}

function startVibration() {
  if (!vibrationSupported() || vibeTimer !== null) return;
  navigator.vibrate(VIBE_PATTERN);
  vibeTimer = setInterval(() => navigator.vibrate(VIBE_PATTERN), VIBE_REPEAT_MS);
}

function stopVibration() {
  if (vibeTimer !== null) {
    clearInterval(vibeTimer);
    vibeTimer = null;
  }
  if (vibrationSupported()) navigator.vibrate(0);
}

function toggleGuardian() {
  if (!vibrationSupported()) return;
  state.armed = !state.armed;
  if (state.armed) navigator.vibrate(200); // confirmation buzz = the unlocking gesture
  syncVibration();
  renderGuardian();
}

/* --- renders (each small + targeted, §7) --- */

function renderConnection() {
  $("reconnect-bar").classList.toggle("hidden", state.connected);
}

function ringClass(level) {
  if (level >= 3) return "ring-alert";
  if (level >= 1) return "ring-warn";
  return "ring-ok";
}

function renderHome() {
  const s = state.status;
  if (!s) return;
  const ring = $("status-ring");
  ring.className = "ring " + ringClass(s.active_alert_level);
  $("ring-label").textContent =
    s.active_alert_level >= 3 ? "EMERGENCY" :
    s.active_alert_level >= 1 ? "WARNING" : "ALL OK";

  const t = s.telemetry;
  $("tile-gas").textContent = t ? t.gas_norm.toFixed(2) : "–";
  $("tile-temp").textContent = t ? `${t.temp_c.toFixed(1)}°C` : "–";
  $("tile-motion").textContent = motionLabel();
  $("tile-node").textContent = s.node_online ? "online" : "offline";
  $("tile-node").classList.toggle("value-bad", !s.node_online);

  setPill($("net-indicator"), s.internet, "internet", "offline");
  setPill($("node-indicator"), s.node_online, "node ✓", "node ✗");
  renderLastAlert();
}

/* D-008 addendum: after an ack the ring goes green and the overlay is gone —
 * without this line that read as "broken" (live confusion 2026-07-12). Show
 * the freshly closed alert as an explicit success for a while. */
function renderLastAlert() {
  const el = $("last-alert");
  const recent = state.activeAlert ? null : state.alerts.find((a) =>
    !ACTIVE_STATES.includes(a.state) &&
    Date.now() / 1000 - a.updated_ts < 30 * 60);
  el.classList.toggle("hidden", !recent);
  if (!recent) return;
  const when = new Date(recent.updated_ts * 1000)
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const how = recent.state === "FALSE_ALARM" ? "false alarm" : "acknowledged";
  el.textContent = `✓ ${recent.title} — ${how} at ${when}`;
}

function motionLabel() {
  if (state.lastMotionTs === null) return "not seen yet";
  const mins = Math.max(0, Math.round((Date.now() / 1000 - state.lastMotionTs) / 60));
  return mins === 0 ? "just now" : `${mins} min ago`;
}

function setPill(el, ok, okText, offText) {
  el.textContent = ok ? okText : offText;
  el.classList.toggle("pill-ok", ok);
  el.classList.toggle("pill-off", !ok);
}

function renderAlerts() {
  const list = $("alert-list");
  list.replaceChildren();
  for (const a of state.alerts) {
    const li = document.createElement("li");
    li.className = `alert-item level-${a.level}`;
    const head = document.createElement("div");
    head.className = "alert-item-head";
    const title = document.createElement("strong");
    title.textContent = a.title;
    const stateTag = document.createElement("span");
    stateTag.className = "badge";
    stateTag.textContent = a.state;
    head.append(title, stateTag);
    const msg = document.createElement("p");
    msg.textContent = a.message;
    const when = document.createElement("time");
    when.textContent = new Date(a.created_ts * 1000).toLocaleTimeString();
    li.append(head, msg, when);
    if (a.synthetic) {
      const synth = document.createElement("span");
      synth.className = "badge badge-synth";
      synth.textContent = "SYNTHETIC";
      li.append(synth);
    }
    list.append(li);
  }
  $("alerts-empty").classList.toggle("hidden", state.alerts.length > 0);
  $("alert-dot").classList.toggle("hidden", !state.activeAlert);
}

function renderOverlay() {
  const a = state.activeAlert;
  $("overlay").classList.toggle("hidden", !a);
  if (!a) return;
  $("overlay-icon").textContent = { GAS: "🔥", FALL: "🧍", HELP: "🆘", NOISE: "🔊" }[a.kind] || "⚠️";
  $("overlay-title").textContent = `${a.title} (L${a.level})`;
  $("overlay-message").textContent = a.message;
  $("overlay-synthetic").classList.toggle("hidden", !a.synthetic);

  const facts = $("overlay-facts");
  facts.replaceChildren();
  for (const [k, v] of Object.entries(a.facts || {})) {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    td1.textContent = k;
    const td2 = document.createElement("td");
    td2.textContent = String(v);
    tr.append(td1, td2);
    facts.append(tr);
  }
  const btn = $("ack-btn");
  btn.disabled = state.acking;
  btn.textContent = state.acking ? "Acknowledging…" : "Acknowledge";
}

function renderGuardian() {
  const btn = $("guardian-btn");
  const hint = $("guardian-hint");
  if (!vibrationSupported()) {
    btn.disabled = true;
    btn.textContent = "Guardian mode unavailable";
    hint.textContent = "This browser can't vibrate (iPhone Safari doesn't allow it). " +
      "The full-screen alert still appears here.";
    return;
  }
  btn.textContent = state.armed ? "Guardian armed — tap to disarm" : "Arm guardian mode";
  btn.classList.toggle("btn-armed", state.armed);
}

function renderDigest() {
  if (!state.digest) return;
  $("digest-card").classList.remove("hidden");
  $("digest-text").textContent = state.digest.text;
  $("digest-engine").textContent =
    state.digest.engine === "cloud-ai-100" ? "Cloud AI 100" :
    state.digest.engine === "local-llm" ? "Local LLM" : state.digest.engine;
}

/* --- actions --- */

async function ackActiveAlert() {
  const a = state.activeAlert;
  if (!a || state.acking) return;
  state.acking = true;
  renderOverlay();
  try {
    const updated = await api(`/api/alerts/${a.id}/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ by: "caregiver" }),
    });
    upsertAlert(updated);
  } catch (err) {
    if (err.status === 409) {
      // already resolved elsewhere — treat as done
      state.activeAlert = null;
      renderOverlay();
      syncVibration();
    } else {
      toast(`Couldn't acknowledge: ${err.message}`);
    }
  } finally {
    state.acking = false;
    renderOverlay();
  }
}

async function generateDigest() {
  const btn = $("digest-btn");
  btn.disabled = true;
  btn.textContent = "Generating…";
  try {
    const body = await api("/api/digest/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    state.digest = body.digest;
    renderDigest();
  } catch (err) {
    toast(err.status === 404
      ? "Digest arrives in a later phase — not wired up yet."
      : `Digest failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate digest";
  }
}

let toastTimer;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}

/* --- tabs --- */

function showTab(name) {
  for (const sec of document.querySelectorAll(".tab")) {
    sec.classList.toggle("hidden", sec.id !== `tab-${name}`);
  }
  for (const btn of document.querySelectorAll(".tabbtn")) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  }
}

/* --- boot --- */

async function boot() {
  document.querySelectorAll(".tabbtn").forEach((b) =>
    b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("ack-btn").addEventListener("click", ackActiveAlert);
  $("digest-btn").addEventListener("click", generateDigest);
  $("guardian-btn").addEventListener("click", toggleGuardian);
  renderGuardian();

  try {
    applyStatus(await api("/api/status"));
    const hist = await api("/api/alerts?limit=20");
    for (const a of hist.alerts.reverse()) upsertAlert(a);
  } catch (err) {
    toast(`Initial load failed: ${err.message}`);
  }

  // phones freeze background tabs: the instant SAATHI is back on screen
  // (e.g. opened from the notification), pull the live truth
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshState();
  });

  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  connectWS(`${wsProto}://${location.host}/ws/caregiver`, onWsMessage);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}

boot();
