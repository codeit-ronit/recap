"""scripts/demo.py's memory watchdog: critical pressure cancels a local model's run."""

from __future__ import annotations

import asyncio

import pytest

from scripts import demo


async def test_critical_pressure_cancels_a_local_models_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo, "WATCH_SECONDS", 0.01)
    monkeypatch.setattr(demo, "pressure_critical", lambda: True)
    cancelled = asyncio.Event()

    async def run() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(demo.MemoryWatchdogStop, match="critical"):
        await demo._watched(run(), local_model=True)  # type: ignore[arg-type]
    assert cancelled.is_set()


async def test_a_run_without_a_local_model_is_not_watched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo, "pressure_critical", lambda: True)

    async def run() -> str:
        return "done"

    assert await demo._watched(run(), local_model=False) == "done"  # type: ignore[arg-type]


async def test_calm_pressure_lets_the_run_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo, "WATCH_SECONDS", 0.01)
    monkeypatch.setattr(demo, "pressure_critical", lambda: False)

    async def run() -> str:
        await asyncio.sleep(0.05)
        return "done"

    assert await demo._watched(run(), local_model=True) == "done"  # type: ignore[arg-type]
