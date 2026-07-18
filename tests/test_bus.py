"""§6.2 bus tests: fan-out to 2 subscribers; overflow drops oldest without
deadlock; early publishes buffer; unsubscribe deregisters."""

import asyncio

import pytest

from app.bus import EventBus


@pytest.mark.asyncio
async def test_fanout_to_two_subscribers():
    bus = EventBus()
    s1 = bus.subscribe("node.event")
    s2 = bus.subscribe("node.event")
    bus.publish("node.event", {"type": "GAS_HIGH", "value": 0.62})
    assert await asyncio.wait_for(anext(s1), 1) == {"type": "GAS_HIGH", "value": 0.62}
    assert await asyncio.wait_for(anext(s2), 1) == {"type": "GAS_HIGH", "value": 0.62}


@pytest.mark.asyncio
async def test_publish_before_first_iteration_is_buffered():
    bus = EventBus()
    sub = bus.subscribe("alert.created")
    bus.publish("alert.created", "a-0001")  # subscriber has not iterated yet
    assert await asyncio.wait_for(anext(sub), 1) == "a-0001"


@pytest.mark.asyncio
async def test_overflow_drops_oldest_and_never_blocks():
    bus = EventBus(maxsize=100)
    sub = bus.subscribe("node.telemetry")
    for i in range(150):  # a sync loop: any blocking publish would deadlock here
        bus.publish("node.telemetry", i)
    received = [await asyncio.wait_for(anext(sub), 1) for _ in range(100)]
    assert received[0] == 50 and received[-1] == 149  # oldest 50 dropped


@pytest.mark.asyncio
async def test_unsubscribe_deregisters_queue():
    bus = EventBus()
    sub = bus.subscribe("system.health")
    bus.publish("system.health", "up")
    await anext(sub)
    await sub.aclose()
    assert bus._queues["system.health"] == []


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_a_noop():
    EventBus().publish("camera.wake", {})  # must not raise
