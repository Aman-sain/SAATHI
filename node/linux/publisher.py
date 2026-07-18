"""SAATHI node → hub MQTT publisher (speaks docs/CONTRACTS.md §1 EXACTLY).

Owner: Divya (M2, hardware) — this lives under node/** which is M2's area. It is
built strictly AGAINST the frozen contract (docs/CONTRACTS.md §1); it imports nothing
from hub/** and changes no contract. If the contract ever looks wrong, FLAG it — don't
edit it here.

WHAT IT DOES
  Publishes to a plain MQTT broker (local mosquitto, NO hub required):

    saathi/node/{node_id}/telemetry   every --interval s (2.0 = contract cadence)
        {"node_id","ts","gas_raw","gas_norm","temp_c","motion","sound_rms","fw"}
        gas_norm and sound_rms are 0..1 normalised (contract §1).

    saathi/node/{node_id}/event       edge-triggered, qos=1, retain=false (contract §1)
        {"node_id","ts","type","value"}
        type ∈ {GAS_HIGH, GAS_CRIT, MOTION, LOUD_NOISE, NODE_BOOT}
        NODE_BOOT is sent once on startup (no value — matches the live-verified
        scripts/mock_node.py convention; the hub accepts an event with no value).

TWO SOURCES OF READINGS
  DEFAULT = SIMULATED. Runs with NO sensor and NO hub — just a broker. Forces a gas
  spike on demand so you can watch GAS_HIGH → GAS_CRIT fire:
      python node/linux/publisher.py                 # then press Enter to spike
      python node/linux/publisher.py --auto-spike     # hands-free spike a few ticks in

  OPTIONAL = REAL SENSORS over serial. Reads the Arduino's data lines
  (saathi_sensors.ino, 9600 baud):
      MQ-2 raw: <n> | PIR: <p> | SND: <s>
      (n = 0..1023 gas ADC, p = 0/1 motion, s = 0..1023 mic peak-to-peak)
  and maps them into gas_raw/gas_norm (contract §1), motion true/false and
  sound_rms 0..1 (= s/1023). A rising motion edge fires MOTION; sound_rms
  crossing --loud fires LOUD_NOISE (EdgeDetector below). Lines from OLDER
  sketch generations (gas-only, gas+PIR) still parse — the missing fields just
  stay at their flat placeholders. temp_c remains a placeholder (DHT11 pending):
      python node/linux/publisher.py --serial COM5

  A USB drop mid-run does NOT kill the process (D5): telemetry pauses, the port is
  retried every few seconds, and publishing resumes when the board re-enumerates —
  the MQTT side stays connected throughout. Only the INITIAL open fails fast (a
  mistyped port name needs a human, not a retry loop).

WATCH IT (no hub needed) — in another terminal:
  & 'C:\\Program Files\\mosquitto\\mosquitto_sub.exe' -h 127.0.0.1 -t 'saathi/#' -v

If the broker/deps are missing, this script prints exact install/start steps and exits.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from collections import deque
from queue import Empty, Queue

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - guidance path
    mqtt = None

# --- contract + node-owned constants -------------------------------------------------

ADC_MAX = 1023  # Arduino Uno R3: 10-bit ADC, raw range 0..1023 (see saathi_sensors.ino)

# The node owns its own thresholds (normalised 0..1). Defaults chosen so a clean-air
# MQ-2 baseline (~95 raw ≈ 0.09) sits well clear, and a gas whiff into the hundreds
# crosses GAS_HIGH. Tunable via CLI so Divya can match her real sensor on the bench.
DEF_GAS_HIGH = 0.35   # mirrors mock_node's GAS_WARN — the hub's R-GAS expects this shape
DEF_GAS_CRIT = 0.70
DEF_LOUD = 0.60
REARM = 0.05          # hysteresis: re-arm an event only after the value drops this far back
RECONNECT_S = 3.0     # serial mode: how often to retry reopening a dropped USB port (D5)

# Simulated baselines (a calm household).
SIM_GAS_BASE = 0.09
SIM_TEMP_BASE = 31.5
SIM_SOUND_BASE = 0.04

# One scripted gas spike: crosses GAS_HIGH (0.35) then GAS_CRIT (0.70), peaks, and
# decays back below both (so the events re-arm) — a full leak-and-clear cycle.
SPIKE_RAMP = [0.28, 0.42, 0.58, 0.74, 0.82, 0.74, 0.55, 0.34, 0.16]

# Serial mode fallbacks: temp is a fixed, honest placeholder until the DHT11 lands;
# sound falls back to a flat placeholder only if the sketch predates the SND token.
SERIAL_TEMP = 31.5
SERIAL_SOUND = 0.04

# All matched with .search() against each serial line
# ("MQ-2 raw: <n> | PIR: <p> | SND: <s>"), so lines from older sketch
# generations (gas-only, gas+PIR) still parse.
RAW_RE = re.compile(r"MQ-2 raw:\s*(\d+)")
PIR_RE = re.compile(r"PIR:\s*([01])")
SND_RE = re.compile(r"SND:\s*(\d+)")


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# --- reading sources -----------------------------------------------------------------
# Each source yields a "reading": a dict with gas_raw, gas_norm, temp_c, motion,
# sound_rms. The Publisher turns readings into contract telemetry + edge events.


class Simulator:
    """Synthetic sensor. Forces gas/motion/noise on demand — no hardware, no hub."""

    def __init__(self) -> None:
        self._gas = deque()      # queued forced gas levels (a spike ramp)
        self._sound = deque()    # queued forced sound levels (a noise burst)
        self._motion_once = False

    def trigger_gas(self) -> None:
        self._gas.extend(SPIKE_RAMP)

    def trigger_motion(self) -> None:
        self._motion_once = True

    def trigger_noise(self) -> None:
        self._sound.extend([0.75, 0.68, 0.30])  # cross LOUD (0.60), then recover

    @staticmethod
    def _jitter(v: float, j: float) -> float:
        return clamp01(v + random.uniform(-j, j))

    def read(self) -> dict:
        gas = self._jitter(self._gas.popleft() if self._gas else SIM_GAS_BASE, 0.01)
        sound = self._jitter(self._sound.popleft() if self._sound else SIM_SOUND_BASE, 0.01)
        motion, self._motion_once = self._motion_once, False
        return {
            "gas_norm": round(gas, 3),
            "gas_raw": round(gas * ADC_MAX),
            "temp_c": round(SIM_TEMP_BASE + random.uniform(-0.4, 0.4), 1),
            "motion": motion,
            "sound_rms": round(sound, 3),
        }


class SerialSource:
    """Reads 'MQ-2 raw: <n> | PIR: <p> | SND: <s>' lines → gas/motion/sound (contract §1).

    Survives a USB drop (D5): a serial error mid-run closes the port, read() returns
    None (telemetry pauses — stale readings are never republished), and a reopen is
    attempted every RECONNECT_S seconds until the board is back. The process never
    exits on a serial error and the MQTT connection is untouched. Only the INITIAL
    open still fails fast with guidance — a mistyped --serial port needs a human,
    not a retry loop.
    """

    def __init__(self, port: str, baud: int) -> None:
        try:
            import serial  # pyserial
        except ImportError:  # pragma: no cover - guidance path
            _die("pyserial is not installed.\n"
                 "  Install it:  python -m pip install pyserial")
        self._serial = serial  # module handle — needed to reopen the port after a drop
        self.port, self.baud = port, baud
        self.ser = None
        self._retry_at = 0.0   # time.monotonic() deadline for the next reopen attempt
        try:
            self._open()
        except Exception as e:  # noqa: BLE001 - surface any open failure clearly
            _die(f"Could not open serial port {port} at {baud} baud: {e}\n"
                 "  • Check the port in Arduino IDE → Tools → Port (e.g. COM5).\n"
                 "  • Close the Arduino Serial Monitor — it holds the port open.")
        print(f"[publisher] serial open: {port} @ {baud} — waiting for 'MQ-2 raw:' lines")

    def _open(self) -> None:
        self.ser = self._serial.Serial(self.port, self.baud, timeout=1.0)
        # Fresh parse state on every (re)open: wait for real lines from the board
        # rather than republishing values remembered from before a drop.
        self._last_raw: int | None = None
        self._last_pir = False
        self._last_snd: int | None = None   # newest SND seen (None until the sketch sends one)
        self._batch_snd: int | None = None  # loudest SND inside the current drain batch

    def _lost(self, err: Exception) -> None:
        """USB dropped mid-run: pause telemetry and start the reopen cycle — never exit."""
        print(f"[publisher] WARNING serial lost on {self.port}: {err}")
        print(f"[publisher] telemetry paused — retrying every {RECONNECT_S:g} s until the "
              "board is back (MQTT stays connected)")
        self.close()
        self.ser = None
        self._retry_at = time.monotonic() + RECONNECT_S

    def _reopen(self) -> bool:
        """One rate-limited reopen attempt; True when the port is usable again."""
        if time.monotonic() < self._retry_at:
            return False
        self._retry_at = time.monotonic() + RECONNECT_S
        try:
            self._open()
        except Exception as e:  # noqa: BLE001 - board still absent; keep retrying
            print(f"[publisher] serial reopen failed ({self.port}): {e}")
            return False
        print(f"[publisher] serial reconnected: {self.port} @ {self.baud} — resuming")
        return True

    def _scan(self, line: str) -> bool:
        """Parse one line into the freshest gas/PIR/sound state; True if PIR was high."""
        m = RAW_RE.search(line)
        if m:
            self._last_raw = int(m.group(1))
        s = SND_RE.search(line)
        if s:
            self._last_snd = int(s.group(1))
            if self._batch_snd is None or self._last_snd > self._batch_snd:
                self._batch_snd = self._last_snd
        p = PIR_RE.search(line)
        if p:
            self._last_pir = p.group(1) == "1"
            return self._last_pir
        return False

    def _drain_latest(self) -> tuple[int | None, bool, int | None]:
        motion_seen = False
        self._batch_snd = None
        # Consume everything buffered since last tick: freshest gas reading wins, but
        # motion LATCHES if ANY drained line was high, and sound keeps the LOUDEST
        # drained line — a short PIR pulse or a clap that rises and falls between two
        # ticks still yields one motion=true / loud telemetry tick (and so one
        # MOTION / LOUD_NOISE rising edge in the EdgeDetector).
        while self.ser.in_waiting:
            motion_seen |= self._scan(self.ser.readline().decode("ascii", "replace").strip())
        if self._last_raw is None:  # nothing seen yet — one blocking read (skips banner lines)
            motion_seen |= self._scan(self.ser.readline().decode("ascii", "replace").strip())
        snd = self._batch_snd if self._batch_snd is not None else self._last_snd
        return self._last_raw, (motion_seen or self._last_pir), snd

    def read(self) -> dict | None:
        if self.ser is None and not self._reopen():
            return None  # board still unplugged — publish nothing this tick
        try:
            raw, motion, snd = self._drain_latest()
        except OSError as e:  # pyserial SerialException ⊂ OSError — the USB link dropped
            self._lost(e)
            return None
        if raw is None:
            return None  # no reading available this tick — skip publishing
        raw = max(0, min(ADC_MAX, raw))
        if snd is None:
            sound = SERIAL_SOUND  # sketch predates the SND token — flat placeholder
        else:
            sound = round(clamp01(min(ADC_MAX, snd) / ADC_MAX), 3)  # contract §1: 0..1
        return {
            "gas_norm": round(clamp01(raw / ADC_MAX), 3),  # contract §1 mapping
            "gas_raw": raw,
            "temp_c": SERIAL_TEMP,      # placeholder: DHT11 not wired yet
            "motion": motion,           # real PIR (HW-416A/HC-SR501 OUT -> D2)
            "sound_rms": sound,         # real KY-037 mic peak-to-peak / 1023
        }

    def close(self) -> None:
        if self.ser is None:
            return
        try:
            self.ser.close()
        except Exception:  # noqa: BLE001
            pass


# --- edge/threshold detection --------------------------------------------------------


class EdgeDetector:
    """Turns a stream of readings into edge-triggered events (contract §1 types).

    Each threshold arms once, fires on the rising crossing, and re-arms only after the
    value falls REARM below the threshold — so a held-high value fires exactly once.
    """

    def __init__(self, gas_high: float, gas_crit: float, loud: float) -> None:
        self.gas_high, self.gas_crit, self.loud = gas_high, gas_crit, loud
        self._armed = {"GAS_HIGH": True, "GAS_CRIT": True, "LOUD_NOISE": True}
        self._prev_motion = False

    def _edge(self, name: str, value: float, thresh: float):
        if value >= thresh and self._armed[name]:
            self._armed[name] = False
            return (name, round(value, 3))
        if value < thresh - REARM:
            self._armed[name] = True
        return None

    def detect(self, r: dict) -> list[tuple[str, float]]:
        events: list[tuple[str, float]] = []
        # GAS_HIGH before GAS_CRIT so a single big jump reports both, in order.
        for ev in (self._edge("GAS_HIGH", r["gas_norm"], self.gas_high),
                   self._edge("GAS_CRIT", r["gas_norm"], self.gas_crit),
                   self._edge("LOUD_NOISE", r["sound_rms"], self.loud)):
            if ev:
                events.append(ev)
        if r["motion"] and not self._prev_motion:
            events.append(("MOTION", 1.0))  # rising edge false→true
        self._prev_motion = r["motion"]
        return events


# --- MQTT publisher ------------------------------------------------------------------


class Publisher:
    def __init__(self, host: str, port: int, node_id: str, fw: str) -> None:
        self.node_id, self.fw = node_id, fw
        self.t_topic = f"saathi/node/{node_id}/telemetry"
        self.e_topic = f"saathi/node/{node_id}/event"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        try:
            self.client.connect(host, port)
        except Exception as e:  # noqa: BLE001 - connection refused / no broker
            _die(f"Could not connect to the MQTT broker at {host}:{port}: {e}\n\n"
                 + BROKER_HELP)
        self.client.loop_start()
        print(f"[publisher] connected {host}:{port} as {node_id} (fw={fw})")

    def telemetry(self, r: dict) -> None:
        payload = {
            "node_id": self.node_id,
            "ts": round(time.time(), 3),
            "gas_raw": r["gas_raw"],
            "gas_norm": r["gas_norm"],
            "temp_c": r["temp_c"],
            "motion": r["motion"],
            "sound_rms": r["sound_rms"],
            "fw": self.fw,
        }
        self.client.publish(self.t_topic, json.dumps(payload))
        print(f"[publisher] telemetry gas_raw={payload['gas_raw']} "
              f"gas_norm={payload['gas_norm']:.3f} motion={payload['motion']} "
              f"sound_rms={payload['sound_rms']:.3f}")

    def event(self, type_: str, value: float | None = None) -> None:
        payload = {"node_id": self.node_id, "ts": round(time.time(), 3), "type": type_}
        if value is not None:  # NODE_BOOT carries no value (mock_node convention)
            payload["value"] = value
        # contract §1: events are qos=1, retain=false
        self.client.publish(self.e_topic, json.dumps(payload), qos=1, retain=False)
        print(f"[publisher] EVENT {type_} value={value}")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


# --- interactive keyboard control (sim mode) -----------------------------------------


def _stdin_reader(q: "Queue[str]") -> None:
    """Daemon thread: turn typed lines into commands for the main loop."""
    for line in sys.stdin:
        q.put(line.strip().lower())


# --- helpers -------------------------------------------------------------------------

BROKER_HELP = (
    "SAATHI needs a LOCAL mosquitto broker on 127.0.0.1:1883 (NO hub required).\n"
    "Install + start it (Windows, one-time):\n"
    "  1. winget install --id EclipseFoundation.Mosquitto -e\n"
    "     (installs to 'C:\\Program Files\\mosquitto' and registers an auto-start service)\n"
    "  2. Start-Service mosquitto        # or reboot; it is Automatic start\n"
    "  3. Verify it is listening:\n"
    "       Get-Service mosquitto\n"
    "       Get-NetTCPConnection -LocalPort 1883 -State Listen\n"
    "Then re-run this publisher."
)


def _die(msg: str) -> None:
    print("\n[publisher] " + msg, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="SAATHI node MQTT publisher (docs/CONTRACTS.md §1)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1", help="broker host")
    ap.add_argument("--port", type=int, default=1883, help="broker port")
    ap.add_argument("--node-id", default="node1")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between telemetry publishes (2.0 = contract cadence)")
    ap.add_argument("--serial", metavar="COMx", default=None,
                    help="read the real MQ-2 over this serial port instead of simulating")
    ap.add_argument("--baud", type=int, default=9600, help="serial baud (sketch uses 9600)")
    ap.add_argument("--fw", default=None,
                    help="firmware string in telemetry (default: sim-1.0 / serial-1.0)")
    ap.add_argument("--auto-spike", action="store_true",
                    help="sim only: auto-trigger one gas spike a few ticks after boot")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after this many seconds (0 = run until Ctrl+C)")
    ap.add_argument("--gas-high", type=float, default=DEF_GAS_HIGH)
    ap.add_argument("--gas-crit", type=float, default=DEF_GAS_CRIT)
    ap.add_argument("--loud", type=float, default=DEF_LOUD)
    args = ap.parse_args()

    if mqtt is None:
        _die("paho-mqtt is not installed.\n"
             "  Install it:  python -m pip install paho-mqtt")

    serial_mode = args.serial is not None
    fw = args.fw or ("serial-1.2" if serial_mode else "sim-1.0")  # 1.2 = MQ-2 + PIR + KY-037 sound
    source = SerialSource(args.serial, args.baud) if serial_mode else Simulator()

    pub = Publisher(args.host, args.port, args.node_id, fw)
    detector = EdgeDetector(args.gas_high, args.gas_crit, args.loud)

    # Interactive control only makes sense for the simulator on a real terminal.
    cmd_q: "Queue[str]" = Queue()
    interactive = (not serial_mode) and sys.stdin is not None and sys.stdin.isatty()
    if interactive:
        threading.Thread(target=_stdin_reader, args=(cmd_q,), daemon=True).start()
        print("[publisher] keys:  <Enter>/g = gas spike   m = motion   "
              "n = loud noise   q = quit")
    elif not serial_mode and not args.auto_spike:
        print("[publisher] (no TTY) tip: pass --auto-spike to force a gas spike hands-free")

    pub.event("NODE_BOOT")  # contract §1: announce boot once

    tick = 0
    start = time.time()
    try:
        while True:
            if args.duration and (time.time() - start) >= args.duration:
                break
            # drain any typed commands (sim only)
            while True:
                try:
                    cmd = cmd_q.get_nowait()
                except Empty:
                    break
                if cmd in ("q", "quit", "exit"):
                    raise KeyboardInterrupt
                if cmd in ("", "g", "gas") and isinstance(source, Simulator):
                    source.trigger_gas(); print("[publisher] >>> gas spike forced")
                elif cmd == "m" and isinstance(source, Simulator):
                    source.trigger_motion(); print("[publisher] >>> motion forced")
                elif cmd == "n" and isinstance(source, Simulator):
                    source.trigger_noise(); print("[publisher] >>> loud noise forced")

            if args.auto_spike and isinstance(source, Simulator) and tick == 3:
                source.trigger_gas(); print("[publisher] >>> gas spike forced (--auto-spike)")

            reading = source.read()
            if reading is not None:
                pub.telemetry(reading)
                for type_, value in detector.detect(reading):
                    pub.event(type_, value)

            tick += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[publisher] stopping")
    finally:
        pub.close()
        if isinstance(source, SerialSource):
            source.close()


if __name__ == "__main__":
    main()
