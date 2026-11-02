"""The Kind adapter's typed Kubernetes operations (recap/environments/kubernetes/kubectl.py).

No model ever writes a kubectl command: the adapter picks one operation from a closed list,
every argument is validated again, and the argument list is built by code, never a shell
string. These tests pin the argument lists, refuse injection attempts before any process
starts, check RECAP's own copy of the RBAC matrix, and run the real subprocess code against
a stand-in `kubectl` (a small script written into tmp_path) to check the child's minimal
environment, timeouts, output limits, strict JSON and what an error may show.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from recap.environments.kubernetes.kubectl import (
    IDENTITY_OPS,
    CommandRecord,
    KubectlClient,
    KubeError,
    KubeOp,
    KubeOutputError,
    build_command,
    duration,
    parse_list,
    parse_object,
    sanitize,
    selector_text,
)

SERVICES = ("frontend", "backend")
INJECTIONS = [
    "-l a,b",
    "--all-namespaces",
    "x; rm -rf ~",
    "$(reboot)",
    "backend --dry-run=none",
    "../secrets",
    "Backend",
    "",
    "-",
    "a" * 64,
    "pods/exec",
]


# --- argument lists ---------------------------------------------------------------------------


def test_reads_build_fixed_argument_lists_with_json_output() -> None:
    assert build_command(KubeOp.LIST_DEPLOYMENTS).tail == ("get", "deployments", "-o", "json")
    assert build_command(KubeOp.GET_DEPLOYMENT, name="backend").tail == (
        "get",
        "deployment",
        "backend",
        "-o",
        "json",
    )
    pods = build_command(KubeOp.LIST_PODS, selector={"app": "backend", "tier": "api"})
    assert pods.tail == ("get", "pods", "-l", "app=backend,tier=api", "-o", "json")
    assert build_command(KubeOp.LIST_PODS, selector=None).tail == ("get", "pods", "-o", "json")
    assert build_command(KubeOp.LIST_EVENTS).tail == ("get", "events", "-o", "json")
    assert build_command(KubeOp.VERSION).tail == ("version", "-o", "json")


def test_logs_name_one_pod_and_container_with_bounds_and_no_output_flag() -> None:
    command = build_command(
        KubeOp.POD_LOGS,
        pod="backend-6d4f8b7c9d-x7k2p",
        container="backend",
        since_time=datetime(2026, 11, 2, 10, 0, 5, 123, tzinfo=UTC),
        tail=40,
        limit_bytes=256_000,
        previous=True,
    )
    assert command.tail == (
        "logs",
        "pod/backend-6d4f8b7c9d-x7k2p",
        "-c",
        "backend",
        "--prefix",
        "--timestamps",
        "--since-time=2026-11-02T10:00:05Z",
        "--tail=40",
        "--limit-bytes=256000",
        "--previous",
    )  # `kubectl logs` prints text: it has no -o


def test_writes_are_typed_and_the_dry_run_is_server_side() -> None:
    undo = build_command(KubeOp.ROLLOUT_UNDO, name="backend", revision=3, dry_run=True)
    assert undo.tail == (
        "rollout",
        "undo",
        "deployment/backend",
        "--to-revision=3",
        "-o",
        "json",
        "--dry-run=server",
    )
    assert undo.writes and undo.dry_run
    at = datetime(2026, 11, 2, 12, 45, 23, tzinfo=UTC)
    restart = build_command(KubeOp.ROLLOUT_RESTART, name="frontend", dry_run=True, restarted_at=at)
    # `kubectl rollout restart` has no --dry-run in 1.37 (seen on the live cluster): the same
    # change is sent as the patch that command makes, which a server-side dry run accepts.
    annotation = {"kubectl.kubernetes.io/restartedAt": "2026-11-02T12:45:23Z"}
    body = json.dumps({"spec": {"template": {"metadata": {"annotations": annotation}}}})
    body = body.replace(" ", "")  # compact, as the command builder writes it
    assert restart.tail == (
        "patch",
        "deployment",
        "frontend",
        "--type=strategic",
        "-p",
        body,
        "-o",
        "json",
        "--dry-run=server",
    )
    assert restart.writes and restart.dry_run
    with pytest.raises(KubeError, match="needs the time"):
        build_command(KubeOp.ROLLOUT_RESTART, name="frontend", dry_run=False)


def test_set_image_is_a_fixed_patch_of_one_demo_image() -> None:
    command = build_command(
        KubeOp.SET_IMAGE,
        services=SERVICES,
        name="backend",
        container="backend",
        version="v1.1.0",
        dry_run=False,
    )
    verb, kind, name, patch_type, flag, body, *rest = command.tail
    assert (verb, kind, name, patch_type, flag, rest) == (
        "patch",
        "deployment",
        "backend",
        "--type=strategic",
        "-p",
        ["-o", "json"],
    )
    assert json.loads(body) == {
        "metadata": {"annotations": {"kubernetes.io/change-cause": "release v1.1.0"}},
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "backend", "image": "recap-demo/backend:v1.1.0"}]}
            }
        },
    }


@pytest.mark.parametrize("value", INJECTIONS)
def test_injection_attempts_as_a_name_are_refused_before_any_process(value: str) -> None:
    with pytest.raises(KubeError, match="refused"):
        build_command(KubeOp.GET_DEPLOYMENT, name=value)
    with pytest.raises(KubeError, match="refused"):
        build_command(KubeOp.ROLLOUT_RESTART, name=value, dry_run=True)
    with pytest.raises(KubeError, match="refused"):
        build_command(KubeOp.ROLLOUT_UNDO, name=value, revision=1, dry_run=True)


@pytest.mark.parametrize("value", [v for v in INJECTIONS if len(v) < 64])
def test_injection_attempts_as_a_pod_or_container_are_refused(value: str) -> None:
    since = datetime(2026, 11, 2, tzinfo=UTC)
    common = {"since_time": since, "tail": 10, "limit_bytes": 4096}
    with pytest.raises(KubeError, match="refused"):
        build_command(KubeOp.POD_LOGS, pod=value, container="backend", **common)
    with pytest.raises(KubeError, match="refused"):
        build_command(KubeOp.POD_LOGS, pod="backend-1", container=value, **common)


@pytest.mark.parametrize(
    "selector",
    ["app=backend", {"app": "a,b"}, {"app": "x y"}, {"-l": "x"}, {"app": "backend; ls"}, {}],
    ids=["text", "comma", "space", "flag", "shell", "empty"],
)
def test_a_selector_is_built_from_validated_labels_never_taken_as_text(selector: Any) -> None:
    with pytest.raises(KubeError, match="refused"):
        selector_text(selector)


@pytest.mark.parametrize("revision", [0, -1, True, "3", 3.0, 10**9], ids=str)
def test_a_revision_must_be_a_positive_integer(revision: Any) -> None:
    with pytest.raises(KubeError, match="revision"):
        build_command(KubeOp.ROLLOUT_UNDO, name="backend", revision=revision, dry_run=True)


@pytest.mark.parametrize(
    ("name", "version", "services"),
    [
        ("backend", "latest", SERVICES),
        ("backend", "v1.1", SERVICES),
        ("backend", "v1.1.0;ls", SERVICES),
        ("payments", "v1.0.0", SERVICES),
        ("backend", "v1.0.0", ()),
    ],
    ids=["tag", "short", "shell", "not-a-demo-service", "no-allowlist"],
)
def test_set_image_deploys_only_demo_images_with_a_version_tag(
    name: str, version: str, services: tuple[str, ...]
) -> None:
    with pytest.raises(KubeError, match="refused"):
        build_command(
            KubeOp.SET_IMAGE,
            services=services,
            name=name,
            container="backend",
            version=version,
            dry_run=True,
        )


def test_each_identity_runs_only_what_its_role_grants() -> None:
    assert KubeOp.ROLLOUT_UNDO not in IDENTITY_OPS["reader"]
    assert KubeOp.SET_IMAGE not in IDENTITY_OPS["reader"]
    assert IDENTITY_OPS["writer"] & {KubeOp.POD_LOGS, KubeOp.LIST_EVENTS, KubeOp.SET_IMAGE} == set()
    assert KubeOp.POD_LOGS not in IDENTITY_OPS["oracle"]
    assert IDENTITY_OPS["oracle"] & {KubeOp.ROLLOUT_UNDO, KubeOp.ROLLOUT_RESTART} == set()
    assert KubeOp.ROLLOUT_UNDO not in IDENTITY_OPS["scenario"]
    # No identity has an operation outside the closed list (there is no "run a command").
    assert set().union(*IDENTITY_OPS.values()) <= set(KubeOp)


# --- strict output ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json", "not JSON"),
        ('{"kind": "Deployment", "x": NaN}', "not JSON"),
        ('{"kind": "Deployment", "x": Infinity}', "not JSON"),
        ("[1, 2]", "not an object"),
        ('{"kind": "Pod"}', "returned kind"),
    ],
    ids=["text", "nan", "infinity", "array", "wrong-kind"],
)
def test_output_of_an_unexpected_shape_is_refused_never_guessed(text: str, reason: str) -> None:
    with pytest.raises(KubeOutputError, match=reason):
        parse_object(text, KubeOp.GET_DEPLOYMENT, {"Deployment"})


def test_list_items_must_be_objects_of_the_listed_kind() -> None:
    good = {"kind": "List", "items": [{"kind": "Pod", "metadata": {"name": "p"}}]}
    assert parse_list(json.dumps(good), KubeOp.LIST_PODS, "Pod")[0]["metadata"]["name"] == "p"
    assert parse_list('{"kind": "PodList", "items": []}', KubeOp.LIST_PODS, "Pod") == []
    for bad in (
        {"kind": "List"},
        {"kind": "List", "items": [1]},
        {"kind": "List", "items": [{"kind": "Secret"}]},
    ):
        with pytest.raises(KubeOutputError):
            parse_list(json.dumps(bad), KubeOp.LIST_PODS, "Pod")


def test_error_text_loses_paths_urls_addresses_and_tokens() -> None:
    token = "eyJ" + "hbGciOiJSUzI1NiJ9" + "." + "eyJ" + "zdWIiOiJyZWNhcCJ9" + "." + "c2ln" * 4
    text = (
        "error: stat /Users/someone/.config/recap/kind/recap-reader.kubeconfig: no such file\n"
        "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused "
        "(https://127.0.0.1:6443/api?timeout=4s) Authorization: "
        + "Bearer "
        + "abc.def.ghi "
        + token
        + " ~/.kube/config"
    )
    shown = sanitize(text, hidden=["/opt/tools/kubectl"])
    assert "/Users/" not in shown and ".kubeconfig" not in shown and "~/.kube" not in shown
    assert "127.0.0.1" not in shown and "https://" not in shown
    assert "eyJ" not in shown and "Bearer abc" not in shown
    assert "\n" not in shown and len(shown) <= 300
    assert "connection refused" in shown  # the reason itself stays readable


def test_durations_are_exact_for_kubectl() -> None:
    assert (duration(4.0), duration(2), duration(0.5), duration(1.25)) == (
        "4s",
        "2s",
        "500ms",
        "1250ms",
    )


# --- the real subprocess, against a stand-in kubectl ------------------------------------------

STAND_IN = """#!{python}
import json, os, pathlib, sys, time
here = pathlib.Path(__file__).parent
with open(here / "calls.jsonl", "a") as calls:
    calls.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
mode = (here / "mode").read_text().strip()
if mode == "ok":
    print(json.dumps({{"kind": "Deployment", "metadata": {{"name": "backend"}}}}))
elif mode == "fail":
    sys.stderr.write("error: open /Users/someone/.kube/x.kubeconfig: denied; see https://10.0.0.1:6443/x\\n")
    sys.exit(1)
elif mode == "sleep":
    time.sleep(30)
elif mode == "big":
    sys.stdout.write("x" * 5_000_000)
elif mode == "nan":
    print('{{"kind": "Deployment", "replicas": NaN}}')
"""


@pytest.fixture
def stand_in(tmp_path: Path) -> Path:
    """A fake kubectl executable whose behaviour the test picks by writing `mode`."""
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "kubectl"
    script.write_text(STAND_IN.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (folder / "mode").write_text("ok", encoding="utf-8")
    return script


def client(
    stand_in: Path, tmp_path: Path, audit: list[CommandRecord], **kwargs: Any
) -> KubectlClient:
    return KubectlClient(
        kubectl=stand_in,
        kubeconfig=tmp_path / "creds" / "recap-reader.kubeconfig",
        home=tmp_path / "kubectl-home",
        identity=kwargs.pop("identity", "reader"),
        context="kind-recap-m5",
        namespace="recap-demo",
        request_timeout=kwargs.pop("request_timeout", 4.0),
        services=SERVICES,
        audit=audit.append,
    )


def calls(stand_in: Path) -> list[dict[str, Any]]:
    path = stand_in.parent / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_kubectl_gets_fixed_flags_and_a_minimal_environment(
    stand_in: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Key-shaped values are built from parts, so this file never holds one (scripts/secret_scan.py).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-" + "not-for-kubectl-0123456789")
    monkeypatch.setenv("KUBECONFIG", "/somewhere/else/config")
    audit: list[CommandRecord] = []

    deployment = await client(stand_in, tmp_path, audit).get_deployment("backend")

    assert deployment["metadata"]["name"] == "backend"
    [call] = calls(stand_in)
    kubeconfig = str(tmp_path / "creds" / "recap-reader.kubeconfig")
    assert call["argv"] == [
        "--kubeconfig",
        kubeconfig,
        "--context",
        "kind-recap-m5",
        "--namespace",
        "recap-demo",
        "--request-timeout",
        "4s",
        "get",
        "deployment",
        "backend",
        "-o",
        "json",
    ]
    # Nothing is inherited: no API key, and KUBECONFIG names this identity's file only.
    env = {k: v for k, v in call["env"].items() if k not in ("__CF_USER_TEXT_ENCODING",)}
    assert set(env) == {"KUBECONFIG", "HOME", "PATH", "LANG", "LC_ALL"}
    assert env["KUBECONFIG"] == kubeconfig and env["HOME"] == str(tmp_path / "kubectl-home")
    # The audit shows the command without the credential's path.
    [record] = audit
    assert record.argv[:3] == ["kubectl", "--kubeconfig", "<recap-reader kubeconfig>"]
    assert kubeconfig not in json.dumps(record.argv) and record.exit_code == 0


async def test_a_failed_call_shows_a_sanitized_reason_and_is_audited(
    stand_in: Path, tmp_path: Path
) -> None:
    (stand_in.parent / "mode").write_text("fail")
    audit: list[CommandRecord] = []

    with pytest.raises(KubeError, match="exit 1") as failure:
        await client(stand_in, tmp_path, audit).list_deployments()

    assert "/Users/" not in str(failure.value) and "10.0.0.1" not in str(failure.value)
    assert audit[0].exit_code == 1 and "/Users/" not in audit[0].error


async def test_a_hung_kubectl_is_killed_at_its_time_limit(stand_in: Path, tmp_path: Path) -> None:
    (stand_in.parent / "mode").write_text("sleep")
    started = time.monotonic()

    with pytest.raises(KubeError, match="did not finish"):
        await client(stand_in, tmp_path, [], request_timeout=0.5).list_deployments()

    assert time.monotonic() - started < 5


@pytest.mark.parametrize(("mode", "reason"), [("big", "too much output"), ("nan", "not JSON")])
async def test_oversized_or_non_finite_output_is_refused(
    stand_in: Path, tmp_path: Path, mode: str, reason: str
) -> None:
    (stand_in.parent / "mode").write_text(mode)
    with pytest.raises(KubeOutputError, match=reason):
        await client(stand_in, tmp_path, []).get_deployment("backend")


async def test_an_identity_cannot_run_another_roles_operation(
    stand_in: Path, tmp_path: Path
) -> None:
    reader = client(stand_in, tmp_path, [])
    with pytest.raises(KubeError, match="reader identity may not run rollout_undo"):
        await reader.rollout_undo("backend", 3, dry_run=True)
    oracle = client(stand_in, tmp_path, [], identity="oracle")
    with pytest.raises(KubeError, match="may not run pod_logs"):
        await oracle.pod_logs("p", "c", since_time=datetime.now(UTC), tail=1, limit_bytes=2048)
    assert calls(stand_in) == []  # refused before any process started


@pytest.mark.parametrize("name", INJECTIONS[:4])
async def test_an_injected_name_never_reaches_a_process(
    stand_in: Path, tmp_path: Path, name: str
) -> None:
    with pytest.raises(KubeError, match="refused"):
        await client(stand_in, tmp_path, []).get_deployment(name)
    assert calls(stand_in) == []
