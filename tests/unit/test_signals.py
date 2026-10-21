"""The log-signal parsers read a fixed family of phrasings (recap/core/signals.py).

Both directions are pinned: what RECAP understands, including phrasings no scenario uses,
and what it does not, so a reader knows exactly where its log parsing stops.
"""

from __future__ import annotations

import pytest

from recap.core import signals


@pytest.mark.parametrize(
    ("message", "dependency"),
    [
        ("upstream payment call failed: timeout after 3000ms", "payment"),  # the default style
        ("rpc to inventory failed: DEADLINE_EXCEEDED (timeout after 3000ms)", "inventory"),
        ("request to cart timed out after 2s", "cart"),  # no scenario writes these two
        ("error calling payment: connection refused", "payment"),
        ("Calls to Checkout failed: 503", "checkout"),
    ],
)
def test_failed_calls_are_read_in_several_phrasings(message: str, dependency: str) -> None:
    assert signals.upstream_dependency(message) == dependency


@pytest.mark.parametrize(
    "message",
    [
        "payment is down",  # names a service but no failed call: not recognized
        "upstream cache hit ratio 0.62",
        "GET /api/cart/items 200 in 90ms",
        "connection refused",
    ],
)
def test_other_text_names_no_failing_dependency(message: str) -> None:
    assert signals.upstream_dependency(message) is None


@pytest.mark.parametrize(
    ("message", "version"),
    [
        ("NullPointerException (version=v2.4.1)", "v2.4.1"),
        ("handler panic: IndexError [build v3.0.2]", "v3.0.2"),
        ("release v1.2.3 rejected the request", "v1.2.3"),
        ("v9.9.9 is mentioned without a marker", None),
    ],
)
def test_version_tags(message: str, version: str | None) -> None:
    assert signals.version_tag(message) == version


@pytest.mark.parametrize(
    ("message", "oom"),
    [
        ("java.lang.OutOfMemoryError: Java heap space", True),
        ("fatal error: runtime: out of memory", True),
        ("container OOMKilled", True),
        ("memory pressure: 93% of the container limit in use", False),  # a warning, not OOM
    ],
)
def test_out_of_memory_text(message: str, oom: bool) -> None:
    assert signals.is_oom_message(message) is oom


def test_the_simulators_rpc_style_is_inside_the_family() -> None:
    rpc = signals.RPC_UPSTREAM_ERROR_TEMPLATE.format(dependency="inventory", reason="x")
    release = signals.RPC_RELEASE_ERROR_TEMPLATE.format(exception="E", version="v1.0.0")
    assert signals.upstream_dependency(rpc) == "inventory"
    assert signals.version_tag(release) == "v1.0.0"
    assert signals.is_oom_message(signals.RPC_OOM_ERROR_MESSAGE)
