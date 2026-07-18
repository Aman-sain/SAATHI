"""Pipeline protocol (§5/§6): every pipeline exposes .start/.stop/.healthy so
main.py can treat TTS/LLM/ASR/vision uniformly — OPTIONAL subsystems that fail
into a degraded state instead of taking the emergency loop down (§8/§16)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Pipeline(Protocol):
    healthy: bool

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
