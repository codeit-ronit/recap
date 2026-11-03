"""Room for a local model: refuse a run that would push this machine into swap.

A local model needs its weights and its context in memory at once (qwen3:8b with a
12,288-token context: about 6.8 GB, measured in M2). Started without that much free, the
machine swaps: every model step slows to minutes, a live target's API calls time out, and the
whole computer stops responding (an owner's Mac in M5, with two container VMs running beside
the model). The entry points that start a real run (scripts/demo.py and the API's run start)
ask `room_for_local_model` first; a model that is already loaded needs no new room.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

NEEDED_GB_ENV = "RECAP_LOCAL_MODEL_MEMORY_GB"
DEFAULT_NEEDED_GB = 7.0  # qwen3:8b with its context measured 6.8 GB; a little margin
GIB = 1024**3


def available_bytes() -> int | None:
    """Memory the system can hand out without swapping, or None when it cannot be read.

    Linux: MemAvailable. macOS: the kernel's own "memory free percentage" (memory_pressure)
    of the installed memory, the figure macOS itself judges pressure by; vm_stat's free,
    inactive and speculative pages when that is unavailable.
    """
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
        found = re.search(r"^MemAvailable:\s+(\d+) kB", text, re.MULTILINE)
        if found:
            return int(found.group(1)) * 1024
    except OSError:
        pass
    pressure = _run(["memory_pressure"])
    total = _run(["sysctl", "-n", "hw.memsize"])
    if pressure and total and total.strip().isdigit():
        share = _free_percentage(pressure)
        if share is not None:
            return int(int(total.strip()) * share / 100)
    stats = _run(["vm_stat"])
    return _vm_stat_available(stats) if stats else None


def _run(argv: list[str]) -> str | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _free_percentage(output: str) -> float | None:
    found = re.search(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%", output)
    return float(found.group(1)) if found else None


def _vm_stat_available(output: str) -> int | None:
    size = re.search(r"page size of (\d+) bytes", output)
    page = int(size.group(1)) if size else 4096
    pages = 0
    for name in ("Pages free", "Pages inactive", "Pages speculative"):
        found = re.search(rf"^{name}:\s+(\d+)\.", output, re.MULTILINE)
        if found:
            pages += int(found.group(1))
    return pages * page if pages else None


def model_loaded(base_url: str, model: str, *, timeout: float = 3.0) -> bool | None:
    """Whether the local server already holds `model` in memory (None: cannot tell)."""
    try:
        reply = httpx.get(f"{base_url.rstrip('/')}/api/ps", timeout=timeout, trust_env=False)
        reply.raise_for_status()
        models = reply.json().get("models") or []
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return any(isinstance(m, dict) and m.get("name") == model for m in models)


def needed_bytes() -> int:
    raw = os.environ.get(NEEDED_GB_ENV, "").strip()
    try:
        gigabytes = float(raw) if raw else DEFAULT_NEEDED_GB
    except ValueError:
        gigabytes = DEFAULT_NEEDED_GB
    return int(max(0.0, gigabytes) * GIB)


def room_for_local_model(
    model: str,
    *,
    loaded: bool | None,
    available: int | None,
    needed: int | None = None,
) -> str | None:
    """None when a run may start; otherwise why not, in words a person can act on.

    Unknown free memory never blocks a run (a measurement that failed is not a shortage).
    """
    if loaded:
        return None
    if available is None:
        return None
    needed = needed_bytes() if needed is None else needed
    if available >= needed:
        return None
    return (
        f"not enough free memory to load {model}: {available / GIB:.1f} GB free, about "
        f"{needed / GIB:.1f} GB needed. Close other applications or virtual machines (for "
        f"example `colima stop`) and try again; {NEEDED_GB_ENV} changes the amount required"
    )


def check_local_model(base_url: str, model: str) -> str | None:
    """The guard the entry points call: measures, then decides (see room_for_local_model)."""
    return room_for_local_model(
        model, loaded=model_loaded(base_url, model), available=available_bytes()
    )


def check_run_memory(settings: Any, provider_name: str | None) -> str | None:
    """The guard for one run's provider: only a local Ollama model is measured."""
    name = provider_name or settings.providers.default
    spec = settings.providers.providers.get(name)
    if spec is None or spec.origin != "local_llm" or spec.api != "ollama" or not spec.model:
        return None
    return check_local_model(spec.base_url or "http://127.0.0.1:11434", spec.model)


def pressure_critical() -> bool | None:
    """Whether the system is short of memory right now (None: cannot tell).

    macOS: the kernel's memory-pressure level is critical (4): it is compressing and swapping
    hard, the state in which the whole machine stops responding. Linux: less than 5% of memory
    is available.
    """
    level = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    if level and level.strip().isdigit():
        return int(level.strip()) >= 4
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    total = re.search(r"^MemTotal:\s+(\d+) kB", text, re.MULTILINE)
    free = re.search(r"^MemAvailable:\s+(\d+) kB", text, re.MULTILINE)
    if not (total and free):
        return None
    return int(free.group(1)) < 0.05 * int(total.group(1))
