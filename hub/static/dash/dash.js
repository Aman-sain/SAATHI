/* SAATHI dashboard (§7 P2): read-only view of the hub's event bus.
 * XSS rule (§18): all dynamic strings via textContent. */
"use strict";

const $ = (id) => document.getElementById(id);
const TICKER_MAX = 15;

/* --- §7 shared pattern: WS with auto-reconnect (3 s backoff) --- */

function connectWS(url, onMessage) {
  let ws;
  const open = () => {
    ws = new WebSocket(url);
    ws.onopen = () => setWsState(true);
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      setWsState(false);
      setTimeout(open, 3000);
    };
    ws.onerror = () => ws.close();
  };
  open();
}

function setWsState(up) {
  const el = $("ws-state");
  el.textContent = up ? "WS: live" : "WS: reconnecting…";
  el.className = "chip " + (up ? "chip-up" : "chip-down");
}

/* --- message handlers --- */

function onWsMessage(msg) {
  if (msg.type === "busevent") addTickerLine(msg);
  else if (msg.type === "status") applyStatus(msg.status);
  else if (msg.type === "keypoints") applyKeypoints(msg);
  else if (msg.type === "metrics") applyMetrics(msg);
  // alert.* also arrive here; the ticker already narrates them via busevents
}

function addTickerLine(ev) {
  const li = document.createElement("li");
  li.className = `tick tick-${ev.level}`;
  const t = document.createElement("time");
  t.textContent = new Date(ev.ts * 1000).toLocaleTimeString();
  const span = document.createElement("span");
  span.textContent = ev.text;
  li.append(t, span);
  const ticker = $("ticker");
  ticker.prepend(li);
  while (ticker.children.length > TICKER_MAX) ticker.lastChild.remove();
}

const CHIP_ORDER = ["node", "broker", "vision", "asr", "llm", "cloud", "internet"];

function applyStatus(s) {
  const strip = $("health-strip");
  strip.replaceChildren();
  for (const name of CHIP_ORDER) {
    const status = name === "internet"
      ? (s.internet ? "up" : "down")
      : (s.subsystems[name] || "down");
    const chip = document.createElement("span");
    chip.className = "chip " +
      (status === "up" ? "chip-up" : status === "degraded" ? "chip-warn" : "chip-down");
    chip.textContent = `${name}: ${status}`;
    strip.append(chip);
  }
  $("ep-badge").textContent = `EP: ${s.ep.toUpperCase()}`;
  $("offline-banner").classList.toggle("hidden", s.internet);
  if (!lastKeypointsTs) setCameraBadge(s.camera_state);
}

let lastKeypointsTs = 0;

function applyKeypoints(msg) {
  lastKeypointsTs = Date.now();
  Skeleton.draw($("skeleton"), msg.kp);
  setCameraBadge(msg.camera_state);
  $("pose-fps").textContent = msg.fps ? `${msg.fps.toFixed(1)} fps` : "";
  $("skel-caption").textContent = "live keypoints (frames never leave the vision process)";
}

function setCameraBadge(state) {
  const el = $("camera-badge");
  const glyph = { SLEEPING: "😴", VERIFYING: "👁", INCIDENT: "🚨" }[state] || "";
  el.textContent = `${glyph} ${state}`;
  el.className = "chip " + (
    state === "SLEEPING" ? "chip-sleep" : state === "VERIFYING" ? "chip-warn" : "chip-down"
  );
}

function applyMetrics(m) {
  if (m.pose_ms != null) $("m-pose").textContent = `${m.pose_ms} ms`;
  if (m.asr_ms != null) $("m-asr").textContent = `${m.asr_ms} ms`;
  if (m.llm_tps != null) $("m-llm").textContent = `${m.llm_tps} tok/s`;
  if (m.ep) $("ep-badge").textContent = `EP: ${m.ep.toUpperCase()}`;
}

/* --- §26 demo panel: hidden until 'D'; everything it fires is SYNTHETIC --- */

document.addEventListener("keydown", (e) => {
  if (e.key.toLowerCase() === "d" && !e.repeat) {
    $("demo-panel").classList.toggle("hidden");
  }
});

document.querySelectorAll("#demo-panel button").forEach((btn) =>
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await fetch("/api/demo/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario: btn.dataset.scenario, synthetic: true }),
      });
    } finally {
      btn.disabled = false;
    }
  })
);

/* --- boot: fixture pose on the canvas (Phase 3), then go live --- */

Skeleton.draw($("skeleton"), Skeleton.FIXTURE);
const wsProto = location.protocol === "https:" ? "wss" : "ws";
connectWS(`${wsProto}://${location.host}/ws/dashboard`, onWsMessage);
