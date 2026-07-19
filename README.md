# SAATHI — an offline AI guardian for elders

> **saathi** (साथी, Hindi): *companion*

Millions of elders in India live alone. The three emergencies that actually kill —
an LPG leak from a stove left on, a fall in an empty room, a cry for help nobody
hears — are all badly served by what exists today. Cloud cameras trade away privacy.
Panic buttons don't work when you're unconscious, or when the button is in the other
room. And everything with "smart" in the name dies the moment the internet does,
which in an Indian home is not a rare event.

SAATHI is our answer: a guardian that senses, thinks, and escalates **entirely on a
local Wi-Fi network**, with no cloud in the emergency path at all. You can pull the
internet cable mid-demo and it keeps protecting. That's not a fallback mode — it's
the design.

Built by a team of three in 24 hours at the **Snapdragon Multiverse Hackathon**
(Noida, July 18–19, 2026).

---

## The four devices

| Device | Job |
|---|---|
| **Arduino UNO Q** | The senses. MQ-2 gas, PIR motion, DHT11 temperature, KY-037 sound — sampled on the MCU, published over MQTT by the Linux side |
| **Snapdragon X AI PC** | The brain. Runs everything: MQTT broker, sensor fusion, Whisper ASR, a local Llama 3.2 3B, Hindi voice alerts, the web server |
| **Caregiver's phone** | The lifeline. A PWA over local Wi-Fi with live status and full-screen alerts, plus lock-screen pages via a self-hosted ntfy server |
| **Qualcomm Cloud AI 100** | The optional scribe. Writes a daily plain-language digest for the family — only when internet exists, never for emergencies |

The elder never touches a screen. They talk (or don't — the sensors cover the case
where they can't), and SAATHI talks back in Hindi.

## What actually happens in an emergency

The gas scenario, end to end, because it's the one we've run a hundred times:

1. The MQ-2 value crosses the warn threshold. The UNO Q publishes a `GAS_HIGH`
   event over MQTT (it also streams telemetry every 2 seconds regardless).
2. The hub's fusion engine opens a **Level 2 alert** and the PC speaks, in Hindi:
   *"Gas ka star badh raha hai, kripya stove check karein."* A 30-second
   escalation timer starts.
3. Three things can stop the escalation: gas falls back below the threshold, the
   elder says an OK-phrase ("i am fine", "theek hoon"), or the caregiver
   acknowledges from the phone. Motion near the node counts too — it means
   someone responded.
4. Nothing happens for 30 seconds (or gas crosses the critical threshold) →
   **Level 3**. The local LLM writes a short, calm message for the caregiver
   ("Gas levels in the kitchen have been rising and Amma hasn't responded…"),
   the phone gets a full-screen red alert over WebSocket *and* a lock-screen
   page through ntfy, and everything is persisted to SQLite.
5. The caregiver taps **Acknowledge** → the alert resolves, the dashboard ticker
   updates, the voice announcements stop.

The crucial property, and the thing we tested hardest: **every optional subsystem
can die and this loop still completes.** LLM down? The alert ships instantly with a
template message and upgrades in place if the LLM comes back. ASR down? The timer
escalates on its own. Phone disconnected? The alert is queued in the DB and
delivered on reconnect. TTS failure? Logged, pipeline continues. We treat the
sensor → fusion → voice → phone chain as sacred and everything else as a bonus.

"Help" detection works the same way from the other direction: the PC's microphone
runs Whisper continuously (3-second chunks with a small overlap tail, simple energy
VAD), scanning transcripts for keyword substrings — "help", "bachao", "madad". No
semantic parsing, deliberately. Dumb string matching doesn't have bad days.

## Architecture

```
                     INTERNET (optional — never in the emergency path)
                         Cloud AI 100 · ntfy.sh family tier
                                      ▲
                                      │ HTTPS, when available
 ELDER'S HOME = our own Wi-Fi hotspot (SSID: SAATHI) — zero internet needed
┌─────────────────────────────────────┼───────────────────────────────────┐
│                                     │                                   │
│  ┌────────────┐   MQTT :1883  ┌─────┴─────────────────────────┐         │
│  │ UNO Q node │ ─────────────▶│  SNAPDRAGON X AI PC (the hub) │         │
│  │ MQ-2 · PIR │               │  Mosquitto broker :1883       │         │
│  │ DHT11      │               │  llama.cpp server :8080       │         │
│  │ KY-037     │               │  ntfy server :2586            │         │
│  └────────────┘               │  FastAPI app :8000            │         │
│                               │   ├ MQTT ingest (validate)    │         │
│   built-in mic ──────────────▶│   ├ fusion engine + rules     │         │
│   PC speaker  ◀───────────────│   ├ alert state machine       │         │
│                               │   ├ ASR / TTS / LLM pipelines │         │
│                               │   ├ SQLite (saathi.db, WAL)   │         │
│                               │   └ REST + WebSocket API      │         │
│                               └─────┬─────────────────────────┘         │
│                                     │ HTTP + WS over the LAN            │
│              ┌──────────────────────┼──────────────┐                    │
│              ▼                      ▼              │                    │
│      caregiver phone         judge dashboard       │                    │
│      PWA at /app             browser at /dash      │                    │
└─────────────────────────────────────────────────────────────────────────┘
```

Inside the hub, modules never import each other's instances — they talk through a
tiny in-process async event bus (`bus.py`, asyncio queues with drop-oldest
overflow so a slow subscriber can never block fusion). The layering rule we
enforced all weekend: business rules live *only* in `fusion/`, SQL lives *only* in
`storage/repo.py`, `.env` is read *only* in `config.py`, and model I/O lives *only*
in `pipelines/`. Every pipeline has a mock twin, which is why the whole system runs
on any laptop with every `MOCK_*` flag set — that's how we developed it before the
hardware existed, and it's the demo insurance if hardware misbehaves on stage.

Alerts move through a small state machine: `OPEN → ANNOUNCED → (ACKED | ESCALATED)
→ RESOLVED`, plus `FALSE_ALARM`. Every transition is persisted and broadcast.
Acks are idempotent because caregivers double-tap when they panic.

One semantic that's easy to get wrong, so it's written down: an OK-phrase only
**cancels the gas escalation timer**. It never resolves an alert, and it never ends
a HELP alert — those end by caregiver acknowledgement only. A distressed person
being told "someone's on the way" and then the system quietly deciding they're fine
is exactly the failure mode we refused to build.

## Tech choices (and the reasoning)

The hub is Windows 11 on ARM, which shaped nearly every decision — anything
without a win-arm64 wheel or binary was disqualified on the spot.

- **Python 3.11 + FastAPI + pydantic v2.** Async lets MQTT, WebSockets, and
  escalation timers share one process. One pydantic schema set validates MQTT,
  REST, and WS payloads alike.
- **SQLite via stdlib `sqlite3`**, WAL mode, one writer thread, hand-written SQL.
  Six tables. An ORM would have added weight and nothing else.
- **llama.cpp as a server binary** (official win-arm64 release) serving
  Llama 3.2 3B Q4. No compilation, no Python-binding wheel roulette, and its
  OpenAI-compatible API means the local LLM and Cloud AI 100 share one httpx
  client — switching engines is a base-URL swap.
- **Whisper base (English) ONNX** on onnxruntime for ASR. CPU execution first as a
  hard rule; the QNN/NPU execution provider is a measured upgrade, never a
  functional dependency, and the dashboard's EP badge shows whichever is true.
- **TTS is a bank of committed WAV files** played through stdlib `winsound`. The
  canonical Hindi alert phrases physically cannot fail to play. Dynamic TTS
  exists but nothing depends on it.
- **Frontends are vanilla HTML/CSS/JS served by the hub itself.** No React, no
  Node, no build step. Three screens didn't justify a toolchain, and same-origin
  serving means no CORS to fight at 3 a.m.
- **No Docker, no Redis, no Celery.** Single hub, 24-hour budget. Background work
  is plain asyncio tasks.

There's also a vision tier in the architecture — YOLOv8n-pose with a duty-cycled
camera (sleeps by default, wakes for 60 s on a loud noise or help keyword, feeds a
deterministic fall heuristic). It's built as a stretch tier and ships **disabled**
in the demo build; nothing in the emergency loop depends on it, which was the point
of building it that way.

## The offline moment

This is the demo climax and the reason the project exists. The hub probes
connectivity every few seconds; when internet drops, the dashboard shows an amber
**"INTERNET DOWN — SAATHI still protecting"** banner — and then we trigger the gas
scenario again and it behaves identically, because nothing in that path ever left
the LAN. The lock-screen page still arrives too: the ntfy server runs *on the hub*.

The daily digest is where the internet is allowed to help. One button in the PWA
hits `POST /api/digest/generate`, the hub assembles the day's event log from
SQLite, and the request walks a fallback ladder: **Cloud AI 100 → local
llama.cpp → deterministic template.** The response carries an `engine` field
(`cloud-ai-100` / `local-llm` / `mock`) and the UI displays it truthfully. When the
cloud works, judges watch the chip go green; when it doesn't, the badge says
"Local LLM" and nobody is lied to. That honesty rule runs through the whole build —
synthetic demo triggers are flagged `synthetic:true` end to end, and health chips
turn green only when a subsystem has actually done its job, not when it claims
it's configured.

## Privacy, enforced by schema

The privacy claim isn't a policy statement — it's the database design. There is no
table for camera frames, no table for audio, no table for transcripts. ASR stores
only the matched keyword and a confidence value. Camera frames (in the stretch
tier) are function-local variables: never encoded, never written, never leave the
process. If you want to verify the claim, read `hub/app/storage/db.py` — the
schema is the proof.

The LAN itself is the trust boundary: no auth, no TLS on the hotspot. A production
deployment would add token auth; for a 24-hour build on a private hotspot we chose
not to pretend, and wrote the decision down instead.

## Repository map

```
hub/
  run_hub.py            entry point
  app/
    main.py             app factory; ordered startup/shutdown of subsystems
    config.py           pydantic-settings; the only place .env is read
    bus.py              in-process async pub/sub
    domain.py           shared pydantic models (imports nothing from the app)
    ingest/             MQTT → validated events → bus + storage
    fusion/             engine, pure rule functions, alert state machine
    pipelines/          asr, tts, llm — model I/O only, each with a mock twin
    notify/             ntfy paging (local + optional internet family tier)
    cloud/              Cloud AI 100 digest client + fallback chain
    api/                REST routes, WS managers, demo trigger endpoints
    storage/            SQLite: connection/DDL in db.py, all SQL in repo.py
  static/
    app/                caregiver PWA (installable, offline shell)
    dash/               judge dashboard: ticker, health strip, offline banner
    audio/              the committed Hindi WAV bank
node/
  mcu/                  Arduino sketch: sensor sampling at 5 Hz
  linux/publisher.py    UNO Q Linux side: Bridge reads → MQTT publish
  uno_q_app/            the Arduino App Lab packaging of the above
scripts/                setup, preflight gate, start/reset, mock node, models
tests/                  182 tests; pytest -q must be green before any merge
docs/                   CONTRACTS.md (frozen interfaces), STATUS.md, runbook
```

The full design rationale — every module's inputs/outputs, error tables, the
phase plan — lives in [`MASTER_ARCHITECTURE.md`](MASTER_ARCHITECTURE.md). The
MQTT/REST/WS interfaces are frozen in [`docs/CONTRACTS.md`](docs/CONTRACTS.md);
changing one required team sign-off and a line in
[`docs/DEVIATIONS.md`](docs/DEVIATIONS.md), which is how three people shared one
codebase for 24 hours without stepping on each other.

## Running it

Windows, PowerShell:

```powershell
git clone https://github.com/AnshSareen/saathi.git ; cd saathi
powershell -ExecutionPolicy Bypass -File scripts\setup_hub.ps1   # venv + deps + checks
Copy-Item .env.example .env                                       # edit values as needed
.\hub\venv\Scripts\Activate.ps1
python scripts\preflight.py                                       # health gate — must be green
```

Then either the whole stack in one shot:

```powershell
scripts\start_hotspot.ps1      # bring up the SAATHI Wi-Fi network (demo topology)
scripts\start_all.ps1          # broker + ntfy + llama server + hub
```

or piece by piece (`scripts\start_llama.ps1`, `python hub\run_hub.py`).

**No hardware? No problem.** Set the `MOCK_*` flags in `.env` and everything runs
on any laptop. To simulate an emergency:

```powershell
python scripts\mock_node.py --scenario gas    # also: fall, help, quiet
```

Watch `logs/hub.log`: you'll see the alert open, the announcement, and the
escalation at exactly 30 seconds.

- **Phone:** join the demo Wi-Fi, open `http://<HUB_LAN_IP>:8000/app` (the hub
  prints the IP in its startup banner). Two supported topologies: phone-hosted
  hotspot (survives a power cut — the primary demo) or PC-hosted hotspot at
  `192.168.137.1` (venue fallback with an internet upstream).
- **Dashboard:** `http://localhost:8000/dash` on the hub.
- **Reset between demos:** `scripts\demo_reset.ps1` — clears active alerts,
  keeps history, idempotent.

Tests:

```powershell
pytest -q
```

## Team

| Name | Role | GitHub account | Email |
|---|---|---|---|
| Ansh Sareen | Hub core, API, frontends, integration | `AnshSareen` | anshsareen78@gmail.com |
| Aman Sain | DevOps, scripts, local network, cloud | `amansain01` | amansain2908@gmail.com |
| Divya Gupta | Hardware, Arduino node, sensors | `divya-gupta137` | 2007gupta.divya@gmail.com |

## Honest limits

Things we chose not to build, on purpose: user accounts, multi-home tenancy,
native mobile apps, video or audio recording of any kind, BLE wearables, trend-ML.
Some because of the 24-hour clock, most because they'd have compromised the two
properties the whole project stands on — the emergency loop must work with zero
internet, and the elder's home must not be surveilled to be protected.

License: [MIT](LICENSE).
