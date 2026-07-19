"""SAATHI UNO Q node — Bridge -> contract telemetry + edge events (CONTRACTS §1)."""
from arduino.app_utils import *
import json, time

# ---- config ----------------------------------------------------------------
NODE_ID     = "node1"
BROKER_HOST = "192.168.137.1"   # hub laptop on OUR hotspot (contract §2 / HARDWARE §2);
                                # "" = print-only bench mode
BROKER_PORT = 1883
INTERVAL_S  = 2.0
FW          = "unoq-1.0"

MOTION_ENABLED = False      # DEMO = False: live motion defers the hub's escalation
                            # and would suppress the phone page (scope-lock 2026-07-14).
                            # Bench-confirmed 2026-07-19: PIR saw the operator and the
                            # hub cancelled the 30 s clock ("t-esc cancelled
                            # reason=motion near node") — no page. Keep False for demo.

# ---- calibration (map RAW hardware range -> contract 0..1) -----------------
# THIS rig, 2026-07-19: clean-air baseline ~60 raw; open-air waft peaked ~650.
GAS_FULL = 1000.0   # raw that counts as gas_norm=1.0 (refine: jar plateau / 0.5)
SND_FULL = 60.0     # peak-to-peak that counts as sound_rms=1.0 (clap ~37 observed)

def norm(raw, full):
    return max(0.0, min(1.0, raw / full))

# Bench evidence 2026-07-19 (THIS rig, dividers in): clean air ~0.087 norm
# (raw ~87), strongest open-air waft ~0.65 norm (raw ~650) — so the old 0.70
# crit default sat ABOVE the sensor's max and could never fire.
# HIGH 0.40: margin over warm-up drift; re-arms at 0.40-0.05=0.35, deliberately
#            equal to the hub's resolve line (GAS_WARN=0.35).
# CRIT 0.55: reachable with ~0.10 headroom under the 0.65 max; re-arms below 0.50.
GAS_HIGH, GAS_CRIT, LOUD, REARM = 0.40, 0.55, 0.60, 0.05
TEMP_FALLBACK = 31.5

# ---- optional MQTT ----------------------------------------------------------
client = None
if BROKER_HOST:
    try:
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.connect(BROKER_HOST, BROKER_PORT)
        client.loop_start()
        print(f"[node] MQTT connected {BROKER_HOST}:{BROKER_PORT}")
    except Exception as e:
        client = None
        print(f"[node] MQTT OFF ({e}) — printing only")

T_TOPIC = f"saathi/node/{NODE_ID}/telemetry"
E_TOPIC = f"saathi/node/{NODE_ID}/event"

def send_event(type_, value=None):
    payload = {"node_id": NODE_ID, "ts": round(time.time(), 3), "type": type_}
    if value is not None:
        payload["value"] = value
    print(f"[node] EVENT {type_} value={value}")
    if client:                       # contract §1: qos=1, retain=false
        client.publish(E_TOPIC, json.dumps(payload), qos=1, retain=False)

# ---- edge detection (fire on crossing, re-arm after REARM drop) -------------
armed = {"GAS_HIGH": True, "GAS_CRIT": True, "LOUD_NOISE": True}
prev_motion = False

def edge(name, value, thresh):
    if value >= thresh and armed[name]:
        armed[name] = False
        send_event(name, round(value, 3))
    elif value < thresh - REARM:
        armed[name] = True

booted = False
def loop():
    global prev_motion, booted
    if not booted:
        booted = True
        send_event("NODE_BOOT")

    gas_raw = Bridge.call("read_gas")
    snd_pp  = Bridge.call("read_sound")
    motion  = bool(Bridge.call("read_pir")) if MOTION_ENABLED else False
    temp    = Bridge.call("read_temp")
    dht_err = Bridge.call("read_dht_err")   # 0 = OK; -91 wiring, -92 timing, -93 checksum

    gas_norm  = round(norm(gas_raw, GAS_FULL), 3)
    sound_rms = round(norm(snd_pp, SND_FULL), 3)
    temp_ok   = temp >= 0
    temp_c    = float(temp) if temp_ok else TEMP_FALLBACK

    payload = {"node_id": NODE_ID, "ts": round(time.time(), 3),
               "gas_raw": gas_raw, "gas_norm": gas_norm, "temp_c": temp_c,
               "motion": motion, "sound_rms": sound_rms, "fw": FW}
    print(f"[node] gas_raw={gas_raw} gas_norm={gas_norm:.3f} "
          f"sound={sound_rms:.3f} motion={motion} temp={temp_c}"
          f"{'' if temp_ok else ' (placeholder — no valid DHT read yet)'}"
          f"{f' dht_err={dht_err}' if dht_err else ''}")
    if client:
        client.publish(T_TOPIC, json.dumps(payload))

    edge("GAS_HIGH", gas_norm, GAS_HIGH)
    edge("GAS_CRIT", gas_norm, GAS_CRIT)
    edge("LOUD_NOISE", sound_rms, LOUD)
    if motion and not prev_motion:
        send_event("MOTION", 1.0)
    prev_motion = motion

    time.sleep(INTERVAL_S)

App.run(user_loop=loop)
