"""Mock UNO Q node (§12.6): publishes CONTRACTS.md §1 telemetry + scripted events
over real MQTT, so the whole hub pipeline runs with zero hardware.

Usage:  python scripts/mock_node.py --scenario gas|fall|quiet
        [--host 127.0.0.1] [--port 1883] [--node-id node1] [--interval 2.0]

Scenarios (J1/J2 shapes):
  gas   — baseline, then gas_norm ramps past GAS_WARN (GAS_HIGH event fires once),
          holds high with NO motion → hub escalates on its 30 s timer (§13).
  fall  — baseline, then one LOUD_NOISE spike (Phase 6 wakes the camera on it).
  quiet — baseline telemetry only (node liveness / dashboards).

Ownership note: scripts/** is M1's area (see docs/DEVIATIONS.md D-003). This file
is written strictly against docs/CONTRACTS.md §1 and imports nothing from hub/app,
so M1 can review or replace it independently.
"""

from __future__ import annotations

import argparse
import json
import random
import time

import paho.mqtt.client as mqtt

GAS_WARN = 0.35  # mirror of .env defaults — the node owns its own thresholds (§6.15)

BASELINE = dict(gas=0.18, temp=31.5, sound=0.04)


def _telemetry(node_id: str, gas: float, motion: bool, sound: float) -> dict:
    jitter = lambda v, j: round(max(0.0, min(1.0, v + random.uniform(-j, j))), 3)
    gas = jitter(gas, 0.01)
    return {
        "node_id": node_id,
        "ts": round(time.time(), 3),
        "gas_raw": int(gas * 1023),
        "gas_norm": gas,
        "temp_c": round(BASELINE["temp"] + random.uniform(-0.4, 0.4), 1),
        "motion": motion,
        "sound_rms": jitter(sound, 0.01),
        "fw": "mock-1.0",
    }


def _event(node_id: str, type_: str, value: float | None) -> dict:
    ev = {"node_id": node_id, "ts": round(time.time(), 3), "type": type_}
    if value is not None:
        ev["value"] = value
    return ev


class MockNode:
    def __init__(self, host: str, port: int, node_id: str, interval: float) -> None:
        self.node_id, self.interval = node_id, interval
        self.t_topic = f"saathi/node/{node_id}/telemetry"
        self.e_topic = f"saathi/node/{node_id}/event"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.connect(host, port)
        self.client.loop_start()
        print(f"[mock_node] connected {host}:{port} as {node_id}")

    def telemetry(self, gas: float, motion: bool = False, sound: float | None = None) -> None:
        payload = _telemetry(self.node_id, gas, motion, sound or BASELINE["sound"])
        self.client.publish(self.t_topic, json.dumps(payload))
        print(f"[mock_node] telemetry gas={payload['gas_norm']:.2f} motion={motion}")

    def event(self, type_: str, value: float | None = None) -> None:
        payload = _event(self.node_id, type_, value)
        # contract §1: events are qos=1, retained=false
        self.client.publish(self.e_topic, json.dumps(payload), qos=1, retain=False)
        print(f"[mock_node] EVENT {type_} value={value}")

    def tick(self) -> None:
        time.sleep(self.interval)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def scenario_gas(node: MockNode) -> None:
    """J1 golden path: expect hub log OPEN → ANNOUNCED → (30 s, no motion) → ESCALATED."""
    node.event("NODE_BOOT")
    for _ in range(3):                      # calm household
        node.telemetry(BASELINE["gas"])
        node.tick()
    for gas in (0.24, 0.30, 0.38, 0.45):    # the leak builds
        if gas >= GAS_WARN:
            node.event("GAS_HIGH", gas)     # edge-triggered exactly once
            node.telemetry(gas)
            node.tick()
            break
        node.telemetry(gas)
        node.tick()
    hold_ticks = max(1, int(38 / node.interval))  # outlast the 30 s escalation timer
    for _ in range(hold_ticks):             # gas stays high, elder doesn't move
        node.telemetry(0.48, motion=False)
        node.tick()
    print("[mock_node] gas scenario complete — check hub log for ESCALATED")


def scenario_fall(node: MockNode) -> None:
    node.event("NODE_BOOT")
    for _ in range(3):
        node.telemetry(BASELINE["gas"])
        node.tick()
    node.event("LOUD_NOISE", 0.82)          # thud — Phase 6 wakes the camera on this
    node.telemetry(BASELINE["gas"], sound=0.82)
    for _ in range(5):
        node.tick()
        node.telemetry(BASELINE["gas"])
    print("[mock_node] fall scenario complete")


def scenario_quiet(node: MockNode) -> None:
    node.event("NODE_BOOT")
    for i in range(15):
        node.telemetry(BASELINE["gas"], motion=(i % 5 == 0))  # occasional wandering
        node.tick()
    print("[mock_node] quiet scenario complete")


SCENARIOS = {"gas": scenario_gas, "fall": scenario_fall, "quiet": scenario_quiet}


def main() -> None:
    ap = argparse.ArgumentParser(description="SAATHI mock node (CONTRACTS.md §1)")
    ap.add_argument("--scenario", choices=SCENARIOS, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--node-id", default="node1")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between telemetry ticks (2.0 = contract cadence)")
    args = ap.parse_args()

    node = MockNode(args.host, args.port, args.node_id, args.interval)
    try:
        SCENARIOS[args.scenario](node)
    finally:
        node.close()


if __name__ == "__main__":
    main()
