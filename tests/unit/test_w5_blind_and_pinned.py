"""M6 W5's two small RECAP changes: a held-out external problem may be BLIND (no answer
before its runs), and only such a problem; and an Ollama provider may pin its model's
weights, refusing a server whose model is another file."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from recap.core.config import load_settings
from recap.core.errors import ProviderError
from recap.environments.kubernetes.scenario import KindScenarioSpec
from recap.providers.factory import create_provider

ALERT = {"service": "frontend", "not_ready": True, "crash_looping": True, "rounds": 2}
EXTERNAL = {"harness": "sregym", "release": "1.1", "commit": "a" * 40, "problem": "some_problem"}


def spec(**fields: Any) -> KindScenarioSpec:
    base: dict[str, Any] = {
        "id": "sregym-blind",
        "title": "t",
        "family": "unlabelled",
        "partition": "test",
        "external": EXTERNAL,
        "alert": ALERT,
        "expected": {},
    }
    return KindScenarioSpec.model_validate({**base, **fields})


def test_a_held_out_external_problem_may_be_blind() -> None:
    blind = spec()
    assert (blind.family, blind.expected) == ("unlabelled", {})


@pytest.mark.parametrize(
    "fields",
    [
        {"partition": "demo"},  # a development problem must carry its answer
        {"external": None, "baseline": {"frontend": "v1.0.0"}, "faults": []},  # RECAP-injected
        {"family": "bad_deployment"},  # half blind: labelled family, no answer
        {"expected": {"root_cause_service": "geo", "mechanism": "bad_deployment", "fix": "x"}},
    ],
)
def test_only_a_held_out_external_problem_may_be_blind(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec(**fields)


def test_a_labelled_problem_still_needs_its_full_answer() -> None:
    with pytest.raises(ValidationError):
        spec(family="bad_deployment", partition="demo", expected={"root_cause_service": "geo"})


def ollama_server(weights: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.34.4"})
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={
                    "modelfile": f"FROM /root/.ollama/models/blobs/{weights.replace(':', '-')}\n",
                    "capabilities": ["completion", "tools"],
                    "details": {"parameter_size": "8.2B", "quantization_level": "Q4_K_M"},
                    "model_info": {"qwen3.context_length": 40960},
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


PINNED = "sha256:" + "a" * 64


def provider_with_pin(server_weights: str) -> Any:
    settings = load_settings(target="")
    spec = settings.providers.providers["ollama"].model_copy(update={"weights": PINNED})
    providers = settings.providers.model_copy(update={"providers": {"ollama": spec}})
    return create_provider(
        "ollama",
        settings.model_copy(update={"providers": providers}),
        mock_http=ollama_server(server_weights),
    )


def test_a_pinned_model_runs_only_on_its_own_weights() -> None:
    ready = asyncio.run(provider_with_pin(PINNED).ensure_ready())
    assert ready["weights"] == PINNED
    with pytest.raises(ProviderError, match="not the pinned"):
        asyncio.run(provider_with_pin("sha256:" + "b" * 64).ensure_ready())


def test_the_vm_model_is_pinned_to_the_weights_m5_ran() -> None:
    vm = load_settings(target="sregym-hotel").providers.providers["ollama-vm"]
    assert vm.weights == "sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f"
    with pytest.raises(ValidationError):
        vm.model_validate({**vm.model_dump(), "weights": "latest"})
