"""The local-model memory guard: a run that would push the machine into swap is refused."""

from __future__ import annotations

import pytest

from recap.core.config import load_settings
from recap.providers import local_memory
from recap.providers.local_memory import (
    GIB,
    _free_percentage,
    _vm_stat_available,
    check_run_memory,
    room_for_local_model,
)

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                3693.
Pages active:                            189986.
Pages inactive:                          188954.
Pages speculative:                          141.
"""


def test_macos_outputs_are_read() -> None:
    assert _free_percentage("System-wide memory free percentage: 37%") == 37.0
    assert _free_percentage("nothing here") is None
    assert _vm_stat_available(VM_STAT) == (3693 + 188954 + 141) * 16384


def test_a_model_is_refused_without_room_and_the_message_says_what_to_do() -> None:
    refusal = room_for_local_model("qwen3:8b", loaded=False, available=5 * GIB, needed=7 * GIB)
    assert refusal is not None
    assert "qwen3:8b" in refusal and "5.0 GB free" in refusal and "7.0 GB needed" in refusal
    assert "colima stop" in refusal


def test_room_a_loaded_model_or_an_unknown_measurement_never_blocks() -> None:
    assert room_for_local_model("m", loaded=False, available=8 * GIB, needed=7 * GIB) is None
    assert room_for_local_model("m", loaded=True, available=1 * GIB, needed=7 * GIB) is None
    assert room_for_local_model("m", loaded=None, available=None, needed=7 * GIB) is None


def test_the_floor_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECAP_LOCAL_MODEL_MEMORY_GB", "9.5")
    assert local_memory.needed_bytes() == int(9.5 * GIB)
    monkeypatch.setenv("RECAP_LOCAL_MODEL_MEMORY_GB", "not a number")
    assert local_memory.needed_bytes() == int(local_memory.DEFAULT_NEEDED_GB * GIB)


def test_only_a_local_ollama_provider_is_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str]] = []

    def fake_check(base_url: str, model: str) -> str | None:
        asked.append((base_url, model))
        return "no room"

    monkeypatch.setattr(local_memory, "check_local_model", fake_check)
    settings = load_settings()
    assert check_run_memory(settings, "deterministic") is None  # the scripted policy
    assert check_run_memory(settings, "ollama") == "no room"
    assert asked == [("http://127.0.0.1:11434", "qwen3:8b")]
