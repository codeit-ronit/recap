"""The Docker Compose setup (M6 W3), checked offline: Docker is not needed to run this test,
and passing it does not mean the images build or the services start (infra/compose/README.md).

What it holds the files to: every published port on 127.0.0.1 only, Redis not published,
every service non-root (a numeric user, or an image whose own user is non-root), every
capability dropped with no privilege escalation and a read-only root filesystem, a health check
on every long-running service and startup gated on health, durable Redis (append-only file),
a worker grace period Docker waits for, and no secret anywhere.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from recap.core.config import REPO_ROOT

COMPOSE = REPO_ROOT / "infra" / "compose"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded = yaml.safe_load((COMPOSE / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return compose["services"]


def test_the_services_are_the_ones_w3_names(compose: dict[str, Any]) -> None:
    assert set(services(compose)) == {"redis", "api", "worker", "dashboard", "prometheus"}
    assert services(compose)["prometheus"]["profiles"] == ["metrics"]  # optional


def test_every_published_port_is_bound_to_localhost_and_redis_is_not_published(
    compose: dict[str, Any],
) -> None:
    assert "ports" not in services(compose)["redis"]
    for name, service in services(compose).items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_every_service_runs_hardened(compose: dict[str, Any]) -> None:
    for name, service in services(compose).items():
        assert service.get("read_only") is True, name
        assert service.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert any(str(t).startswith("/tmp") for t in service.get("tmpfs", [])), name
        assert service.get("privileged") is not True and "cap_add" not in service, name
        assert service.get("network_mode") != "host", name
    # Non-root: RECAP's image sets USER 10001, nginx-unprivileged and prometheus run as
    # non-root users of their own, and Redis is told its non-root user explicitly.
    assert services(compose)["redis"]["user"] == "999:1000"
    dockerfile = (COMPOSE / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "nginx-unprivileged" in dockerfile


def test_health_gates_the_startup_order(compose: dict[str, Any]) -> None:
    long_running = ("redis", "api", "worker", "dashboard")
    for name in long_running:
        assert services(compose)[name].get("healthcheck", {}).get("test"), name
    depends = {name: services(compose)[name].get("depends_on", {}) for name in services(compose)}
    assert depends["api"] == {"redis": {"condition": "service_healthy"}}
    assert depends["worker"]["redis"] == {"condition": "service_healthy"}
    assert depends["dashboard"] == {"api": {"condition": "service_healthy"}}
    assert services(compose)["worker"]["healthcheck"]["test"][-1] == "--check"


def test_one_service_builds_each_local_image(compose: dict[str, Any]) -> None:
    """Two services building one tag race when they tag it (seen in W3's first real build):
    the api builds recap:local, and the worker reuses it without ever pulling it."""
    builders: dict[str, list[str]] = {}
    for name, service in services(compose).items():
        if service.get("build"):
            builders.setdefault(service["image"], []).append(name)
    assert builders == {"recap:local": ["api"], "recap-dashboard:local": ["dashboard"]}
    worker = services(compose)["worker"]
    assert worker["image"] == "recap:local" and worker["pull_policy"] == "never"


def test_redis_keeps_the_queue_across_a_restart(compose: dict[str, Any]) -> None:
    command = services(compose)["redis"]["command"]
    assert command[command.index("--appendonly") + 1] == "yes"
    assert command[command.index("--appendfsync") + 1] == "everysec"
    assert "redis-data:/data" in services(compose)["redis"]["volumes"]


def test_the_api_and_the_workers_share_the_data_and_the_queue(compose: dict[str, Any]) -> None:
    for name in ("api", "worker"):
        service = services(compose)[name]
        env = service["environment"]
        assert env["RECAP_EXECUTION_BACKEND"] == "redis", name
        assert env["RECAP_REDIS_URL"] == "redis://redis:6379/0", name
        assert env["RECAP_DATA_DIR"] == "/data", name
        # Nothing may write outside /data and /tmp (a read-only root, a user without a home).
        assert env["RECAP_USAGE_LEDGER"] == "/data/llm-usage.db", name
        assert "recap-data:/data" in service["volumes"], name
    assert set(compose["volumes"]) == {"redis-data", "recap-data", "prometheus-data"}


def test_docker_waits_for_a_worker_s_graceful_stop(compose: dict[str, Any]) -> None:
    worker = services(compose)["worker"]
    grace = int(re.search(r":-(\d+)", worker["environment"]["RECAP_WORKER_GRACE_SECONDS"]).group(1))  # type: ignore[union-attr]
    assert worker["stop_signal"] == "SIGTERM"
    assert int(worker["stop_grace_period"].rstrip("s")) >= grace + 15  # time to settle too


def test_no_secret_is_committed_and_the_env_example_holds_none(compose: dict[str, Any]) -> None:
    text = "\n".join(
        (COMPOSE / name).read_text(encoding="utf-8")
        for name in ("compose.yaml", ".env.example", "Dockerfile", "nginx.conf", "prometheus.yml")
    )
    for pattern in (r"sk-[A-Za-z0-9]{10,}", r"gsk_[A-Za-z0-9]{10,}", r"ghp_[A-Za-z0-9]{10,}"):
        assert not re.search(pattern, text), pattern
    example = dict(
        line.split("=", 1)
        for line in (COMPOSE / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert example["RECAP_OPERATOR_TOKEN"] == ""  # generated on first start, never committed
    assert (
        services(compose)["api"]["environment"]["RECAP_OPERATOR_TOKEN"]
        == "${RECAP_OPERATOR_TOKEN:-}"
    )
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored  # infra/compose/.env included: the pattern matches at any depth


def test_the_build_context_leaves_out_data_secrets_and_history() -> None:
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".git", ".env", "data", "**/node_modules", ".venv", "tests"):
        assert entry in ignored, entry


def test_every_base_image_is_pinned_to_a_version(compose: dict[str, Any]) -> None:
    # The stack's own images (recap:local, recap-dashboard:local) are built here, not pulled.
    images = [s["image"] for s in services(compose).values() if not s["image"].endswith(":local")]
    images += re.findall(r"^ARG \w+_IMAGE=(\S+)", (COMPOSE / "Dockerfile").read_text(), re.M)
    assert len(images) == 5
    for image in images:
        name, _, tag = image.partition("@")[0].rpartition(":")
        assert name and re.match(r"v?\d+\.\d+\.\d+", tag), image  # never "latest" or a major only
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), image  # and the exact bytes


def test_nginx_proxies_the_api_on_one_origin_and_streams_events() -> None:
    conf = (COMPOSE / "nginx.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://api:8000/;" in conf  # /api/runs -> /runs, as the Vite dev server
    assert "proxy_set_header Host $http_host;" in conf  # the API's host check still applies
    assert "proxy_buffering off;" in conf  # server-sent events
    assert "listen 8080;" in conf and "pid /tmp/nginx.pid;" in conf  # unprivileged, read-only root
