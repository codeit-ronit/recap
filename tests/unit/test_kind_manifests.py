"""The Kind demo's cluster files keep M5's contract, offline: no Docker, no cluster, no kubectl.

Every manifest is parsed (yaml.safe_load_all) and held to what docs/m5-integration-plan.md
sections 3 and 4 promise: the two namespaces and their Pod Security labels, pods that meet
"restricted", third-party images pinned by digest, RECAP's four identities with exactly the
contract's verbs in recap-demo only (nothing cluster-wide, no Secrets), the admission policy,
and every host port on 127.0.0.1. The scripts get `bash -n`, and their guards and helpers run
against a stub `kubectl` (a shell function), so a refusal is tested before any cluster exists.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from recap.telemetry.redact import SECRET_PATTERNS

ROOT = Path(__file__).resolve().parents[2]
KIND = ROOT / "infra" / "kind"
MANIFESTS = sorted((KIND / "manifests").glob("*.yaml"))
SCRIPTS = KIND / "scripts"
APP = KIND / "app" / "demo_app.py"
RUNNABLE = ("setup.sh", "mint-tokens.sh", "status.sh", "inject-fault.sh", "reset.sh", "cleanup.sh")
DEMO_IMAGES = {
    "recap-demo/frontend:v1.0.0",
    "recap-demo/backend:v1.0.0",
    "recap-demo/backend:v1.1.0",
    "recap-demo/backend:v1.1.1",
    "recap-demo/backend:v1.2.0",
}
NODE_IMAGE = (
    "kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
PINNED = re.compile(r"[a-z0-9][a-z0-9./_-]*(:[\w.-]+)?@sha256:[0-9a-f]{64}")
DEMO_IMAGE = re.compile(r"recap-demo/(frontend|backend):v\d+\.\d+\.\d+")
IDENTITIES = ("recap-reader", "recap-writer", "recap-oracle", "recap-scenario")
SA_USER = "system:serviceaccount:recap-system:{}"

# docs/m5-integration-plan.md section 3.4, as (API group, resource, verb, resource name) grants.
# "events" is readable through both event APIs (core and events.k8s.io), read-only.
EXPECTED_GRANTS: dict[str, set[tuple[str, str, str, str]]] = {
    "recap-reader": {
        *{(g, "events", v, "") for g in ("", "events.k8s.io") for v in ("get", "list")},
        *{("", "pods", v, "") for v in ("get", "list")},
        ("", "pods/log", "get", ""),
        *{("apps", r, v, "") for r in ("deployments", "replicasets") for v in ("get", "list")},
    },
    "recap-writer": {
        *{("apps", "deployments", v, n) for v in ("get", "patch") for n in ("frontend", "backend")},
        ("apps", "replicasets", "list", ""),
    },
    "recap-oracle": {
        *{("apps", "deployments", v, "") for v in ("get", "list")},
        *{("", "pods", v, "") for v in ("get", "list")},
    },
    "recap-scenario": {
        *{("apps", "deployments", v, n) for v in ("get", "patch") for n in ("frontend", "backend")},
        ("apps", "replicasets", "list", ""),
        ("", "pods", "list", ""),
    },
}


def _documents() -> list[dict[str, Any]]:
    documents = []
    for path in MANIFESTS:
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if document is not None:
                documents.append(document)
    return documents


DOCS = _documents()


def of_kind(kind: str) -> list[dict[str, Any]]:
    return [doc for doc in DOCS if doc["kind"] == kind]


def find(kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
    found = [
        doc
        for doc in of_kind(kind)
        if doc["metadata"]["name"] == name and doc["metadata"].get("namespace") == namespace
    ]
    assert len(found) == 1, (kind, name, namespace)
    return found[0]


def pod_spec(name: str, namespace: str) -> dict[str, Any]:
    spec: dict[str, Any] = find("Deployment", name, namespace)["spec"]["template"]["spec"]
    return spec


def containers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [*spec.get("initContainers", []), *spec["containers"]]


def grants(role: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    return {
        (group, resource, verb, name)
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
        for name in rule.get("resourceNames") or [""]
    }


def lib_images() -> set[str]:
    """The images lib.sh says setup.sh builds and loads."""
    found = re.search(r'^DEMO_IMAGES="([^"]+)"$', (SCRIPTS / "lib.sh").read_text(), re.MULTILINE)
    assert found is not None
    return {f"recap-demo/{image}" for image in found[1].split()}


# --- Pod Security "restricted", checked the way the admission plugin checks a pod ---------

ALLOWED_VOLUMES = {
    "configMap",
    "csi",
    "downwardAPI",
    "emptyDir",
    "ephemeral",
    "persistentVolumeClaim",
    "projected",
    "secret",
}
SECCOMP = {"RuntimeDefault", "Localhost"}


def restricted_problems(spec: dict[str, Any]) -> list[str]:
    """What keeps a pod spec from meeting Pod Security "restricted" (empty when it does)."""
    problems = [key for key in ("hostNetwork", "hostPID", "hostIPC") if spec.get(key)]
    pod = spec.get("securityContext", {})
    for volume in spec.get("volumes", []):
        problems += [f"volume type {kind}" for kind in set(volume) - {"name"} - ALLOWED_VOLUMES]
    for container in containers(spec):
        name, own = container["name"], container.get("securityContext", {})
        if own.get("privileged"):
            problems.append(f"{name}: privileged")
        if own.get("allowPrivilegeEscalation") is not False:
            problems.append(f"{name}: privilege escalation not disabled")
        capabilities = own.get("capabilities", {})
        if "ALL" not in capabilities.get("drop", []):
            problems.append(f"{name}: capabilities not all dropped")
        if set(capabilities.get("add", [])) - {"NET_BIND_SERVICE"}:
            problems.append(f"{name}: capabilities added")
        if own.get("runAsNonRoot", pod.get("runAsNonRoot")) is not True:
            problems.append(f"{name}: may run as root")
        if own.get("runAsUser", pod.get("runAsUser")) == 0:
            problems.append(f"{name}: runs as user 0")
        seccomp = own.get("seccompProfile", pod.get("seccompProfile", {}))
        if seccomp.get("type") not in SECCOMP:
            problems.append(f"{name}: no seccomp profile")
        if any(port.get("hostPort") for port in container.get("ports", [])):
            problems.append(f"{name}: host port")
    return problems


DEPLOYMENTS = [
    ("frontend", "recap-demo"),
    ("backend", "recap-demo"),
    ("prometheus", "recap-system"),
    ("traffic", "recap-system"),
]


# --- manifests -----------------------------------------------------------------------------


def test_every_manifest_parses_and_stays_in_the_two_namespaces() -> None:
    assert [path.name for path in MANIFESTS] == [
        "00-namespaces.yaml",
        "10-recap-rbac.yaml",
        "20-recap-admission-policy.yaml",
        "30-demo-app.yaml",
        "40-prometheus.yaml",
        "50-traffic.yaml",
    ]
    cluster_scoped = {"Namespace", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
    for doc in DOCS:
        assert doc["apiVersion"] and doc["metadata"]["name"], doc
        if doc["kind"] in cluster_scoped:
            assert "namespace" not in doc["metadata"], doc["metadata"]
        else:
            assert doc["metadata"]["namespace"] in {"recap-demo", "recap-system"}, doc["metadata"]
    kinds = {doc["kind"] for doc in DOCS}
    assert not kinds & {"ClusterRole", "ClusterRoleBinding", "Secret", "PersistentVolumeClaim"}
    assert sorted(
        (d["metadata"]["name"], d["metadata"]["namespace"]) for d in of_kind("Deployment")
    ) == sorted(DEPLOYMENTS)


def test_both_namespaces_enforce_pod_security_restricted() -> None:
    for name in ("recap-demo", "recap-system"):
        labels = find("Namespace", name)["metadata"]["labels"]
        for mode in ("enforce", "audit", "warn"):
            assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted", (name, mode)
            assert labels[f"pod-security.kubernetes.io/{mode}-version"] == "v1.37", (name, mode)
    assert {ns["metadata"]["name"] for ns in of_kind("Namespace")} == {"recap-demo", "recap-system"}


@pytest.mark.parametrize("service", ["frontend", "backend"])
def test_the_demo_deployments_follow_the_contract(service: str) -> None:
    deployment = find("Deployment", service, "recap-demo")
    spec = deployment["spec"]
    annotations = deployment["metadata"]["annotations"]
    assert annotations["kubernetes.io/change-cause"] == "release v1.0.0"
    if service == "frontend":
        assert annotations["recap.dev/depends-on"] == "backend"
    else:
        assert "recap.dev/depends-on" not in annotations
    assert spec["replicas"] == 2
    assert spec["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }
    assert spec["progressDeadlineSeconds"] == 60 and spec["revisionHistoryLimit"] == 5
    assert spec["selector"]["matchLabels"] == {"app": service}
    assert spec["template"]["metadata"]["labels"]["app"] == service
    (container,) = spec["template"]["spec"]["containers"]
    assert container["name"] == service
    assert container["image"] == f"recap-demo/{service}:v1.0.0"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["ports"] == [{"name": "http", "containerPort": 8080, "protocol": "TCP"}]
    assert container["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": "http"}
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    resources = container["resources"]
    assert set(resources["requests"]) == set(resources["limits"]) == {"cpu", "memory"}
    env = {item["name"]: item for item in container["env"]}
    limit = env["MEMORY_LIMIT_BYTES"]["valueFrom"]["resourceFieldRef"]
    assert limit == {"containerName": service, "resource": "limits.memory"}
    if service == "frontend":
        assert env["BACKEND_URL"]["value"] == "http://backend.recap-demo.svc.cluster.local"


@pytest.mark.parametrize(("name", "namespace"), DEPLOYMENTS)
def test_every_pod_meets_pod_security_restricted(name: str, namespace: str) -> None:
    spec = pod_spec(name, namespace)
    assert restricted_problems(spec) == []
    assert all(c["securityContext"]["readOnlyRootFilesystem"] is True for c in containers(spec))
    assert spec["enableServiceLinks"] is False
    # Only Prometheus needs its ServiceAccount token (service discovery reads the API with it).
    assert spec["automountServiceAccountToken"] is (name == "prometheus")


def test_the_restricted_check_itself_finds_what_it_should() -> None:
    privileged = {
        "hostNetwork": True,
        "volumes": [{"name": "root", "hostPath": {"path": "/"}}],
        "containers": [
            {
                "name": "x",
                "securityContext": {"privileged": True, "capabilities": {"add": ["SYS_ADMIN"]}},
                "ports": [{"containerPort": 80, "hostPort": 80}],
            }
        ],
    }
    problems = restricted_problems(privileged)
    for expected in (
        "hostNetwork",
        "hostPath",
        "privileged",
        "escalation",
        "added",
        "root",
        "seccomp",
    ):
        assert any(expected in problem for problem in problems), expected


def test_services_expose_the_contract_node_ports_and_nothing_else() -> None:
    node_ports = {}
    for service in of_kind("Service"):
        meta, spec = service["metadata"], service["spec"]
        assert spec["type"] == "NodePort"
        (port,) = spec["ports"]
        node_ports[(meta["namespace"], meta["name"])] = (
            port["port"],
            port["nodePort"],
            port["targetPort"],
        )
        deployment = find("Deployment", meta["name"], meta["namespace"])
        assert spec["selector"] == deployment["spec"]["selector"]["matchLabels"]
    assert node_ports == {
        ("recap-demo", "frontend"): (80, 30080, "http"),
        ("recap-demo", "backend"): (80, 30081, "http"),
        ("recap-system", "prometheus"): (9090, 30090, "http"),
    }


def test_the_cluster_and_every_host_port_listen_on_loopback_only() -> None:
    cluster = yaml.safe_load((KIND / "cluster.yaml").read_text(encoding="utf-8"))
    assert cluster["kind"] == "Cluster" and cluster["apiVersion"] == "kind.x-k8s.io/v1alpha4"
    assert cluster["name"] == "recap-m5"
    assert cluster["networking"] == {"apiServerAddress": "127.0.0.1"}
    (node,) = cluster["nodes"]
    assert node["role"] == "control-plane" and node["image"] == NODE_IMAGE
    mappings = node["extraPortMappings"]
    assert all(mapping["listenAddress"] == "127.0.0.1" for mapping in mappings)
    assert {(m["containerPort"], m["hostPort"]) for m in mappings} == {
        (30080, 30080),
        (30081, 30081),
        (30090, 30090),
    }
    node_ports = {s["spec"]["ports"][0]["nodePort"] for s in of_kind("Service")}
    assert node_ports == {mapping["containerPort"] for mapping in mappings}


def test_third_party_images_are_pinned_and_demo_images_are_the_five_built() -> None:
    images = {c["image"] for name, ns in DEPLOYMENTS for c in containers(pod_spec(name, ns))}
    demo = {image for image in images if DEMO_IMAGE.fullmatch(image)}
    assert demo <= DEMO_IMAGES == lib_images()
    others = images - demo
    assert others == {
        "prom/prometheus:v3.15.1@sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2"
    }
    dockerfile = (KIND / "app" / "Dockerfile").read_text(encoding="utf-8")
    bases = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    assert bases and all(PINNED.fullmatch(base) for base in [*bases, *others, NODE_IMAGE])
    for name, namespace in DEPLOYMENTS:
        for container in containers(pod_spec(name, namespace)):
            assert container["imagePullPolicy"] == "IfNotPresent", name


def test_the_image_runs_as_the_non_root_user_the_manifests_name() -> None:
    dockerfile = (KIND / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER 10001:10001$", dockerfile, re.MULTILINE)
    assert re.search(r"^ARG SERVICE$", dockerfile, re.MULTILINE)
    assert re.search(r"^ARG VERSION$", dockerfile, re.MULTILINE)
    assert 'ENTRYPOINT ["python", "-I", "-B", "/app/demo_app.py"]' in dockerfile
    assert re.findall(r"^COPY (.+)$", dockerfile, re.MULTILINE) == ["demo_app.py /app/demo_app.py"]
    ignored = (KIND / "app" / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert [line for line in ignored if not line.startswith("#")] == ["*", "!demo_app.py"]
    for name in ("frontend", "backend", "traffic"):
        security = pod_spec(name, "recap-demo" if name != "traffic" else "recap-system")
        assert security["securityContext"]["runAsUser"] == 10001


# --- RECAP's identities ----------------------------------------------------------------------


def test_recap_identities_have_exactly_the_contract_permissions() -> None:
    for identity in IDENTITIES:
        account = find("ServiceAccount", identity, "recap-system")
        assert account["automountServiceAccountToken"] is False  # no pod ever carries it
        assert grants(find("Role", identity, "recap-demo")) == EXPECTED_GRANTS[identity], identity
    bound = {}
    for binding in of_kind("RoleBinding"):
        assert binding["metadata"]["namespace"] == "recap-demo"
        assert binding["roleRef"]["kind"] == "Role"
        for subject in binding["subjects"]:
            assert subject["kind"] == "ServiceAccount" and subject["namespace"] == "recap-system"
            bound[subject["name"]] = binding["roleRef"]["name"]
    assert bound == {identity: identity for identity in IDENTITIES} | {
        "prometheus": "prometheus-discovery"
    }


def test_no_identity_reaches_secrets_writes_broadly_or_acts_cluster_wide() -> None:
    forbidden_resources = {
        "secrets",
        "configmaps",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "services/proxy",
        "nodes/proxy",
        "serviceaccounts/token",
        "deployments/scale",
    }
    forbidden_verbs = {
        "create",
        "update",
        "delete",
        "deletecollection",
        "escalate",
        "bind",
        "impersonate",
    }
    for role in of_kind("Role"):
        assert role["metadata"]["namespace"] == "recap-demo"
        for rule in role["rules"]:
            assert "*" not in [*rule["apiGroups"], *rule["resources"], *rule["verbs"]], rule
            assert not forbidden_resources & set(rule["resources"]), rule
            assert not forbidden_verbs & set(rule["verbs"]), rule
    writes = {
        (role["metadata"]["name"], grant)
        for role in of_kind("Role")
        for grant in grants(role)
        if grant[2] not in {"get", "list", "watch"}
    }
    assert writes == {
        (identity, ("apps", "deployments", "patch", name))
        for identity in ("recap-writer", "recap-scenario")
        for name in ("frontend", "backend")
    }
    assert not of_kind("ClusterRole") and not of_kind("ClusterRoleBinding")


def test_the_admission_policy_keeps_recap_writes_in_their_shape() -> None:
    spec = find("ValidatingAdmissionPolicy", "recap-writes-keep-their-shape")["spec"]
    assert spec["failurePolicy"] == "Fail"
    assert spec["matchConstraints"]["resourceRules"] == [
        {
            "apiGroups": ["apps"],
            "apiVersions": ["v1"],
            "operations": ["UPDATE"],
            "resources": ["deployments"],
        }
    ]
    condition = " ".join(c["expression"] for c in spec["matchConditions"])
    users = set(re.findall(r"system:serviceaccount:[\w:-]+", condition))
    assert users == {SA_USER.format("recap-writer"), SA_USER.format("recap-scenario")}
    expressions = [validation["expression"] for validation in spec["validations"]]
    assert all(validation["message"] for validation in spec["validations"])
    assert "object.spec.replicas == oldObject.spec.replicas" in expressions
    everything = " ".join(expressions)
    pattern = re.search(r"\.matches\('([^']+)'\)", everything)
    assert pattern is not None
    accepted = [
        "recap-demo/backend:v1.0.0",
        "recap-demo/frontend:v1.0.0",
        "recap-demo/backend:v1.12.3",
    ]
    refused = [
        "recap-demo/backend:latest",
        "recap-demo/backend:v1.0",
        "recap-demo/db:v1.0.0",
        "evil/backend:v1.0.0",
        "docker.io/recap-demo/backend:v1.0.0",
        "recap-demo/backend:v1.0.0@sha256:" + "0" * 64,
    ]
    assert all(re.search(pattern[1], image) for image in accepted)
    assert not any(re.search(pattern[1], image) for image in refused)
    assert set(re.findall(r"'(recap-demo/[a-z]+:v[0-9.]+)'", everything)) == DEMO_IMAGES
    for rule in (
        "startsWith('recap-demo/' + c.name + ':')",
        "!has(c.command) && !has(c.args)",
        "!has(object.spec.template.spec.initContainers)",
        "!has(object.spec.template.spec.volumes)",
        "!has(object.spec.template.spec.serviceAccountName)",
        "object.spec.template.spec.automountServiceAccountToken == false",
    ):
        assert rule in everything, rule
    binding = find("ValidatingAdmissionPolicyBinding", "recap-writes-keep-their-shape")
    assert binding["spec"] == {
        "policyName": "recap-writes-keep-their-shape",
        "validationActions": ["Deny"],
    }


@pytest.mark.parametrize("service", ["frontend", "backend"])
def test_the_demo_templates_themselves_pass_the_admission_policy(service: str) -> None:
    """Otherwise every rollback, restart and fault injection would be denied."""
    spec = pod_spec(service, "recap-demo")
    assert "initContainers" not in spec and "volumes" not in spec
    assert "serviceAccountName" not in spec and spec["automountServiceAccountToken"] is False
    for container in spec["containers"]:
        assert "command" not in container and "args" not in container
        assert container["image"] in DEMO_IMAGES
        assert container["image"].startswith(f"recap-demo/{container['name']}:")


def test_prometheus_reads_only_recap_demo_pods_and_keeps_no_volume() -> None:
    assert find("ServiceAccount", "prometheus", "recap-system")
    role = find("Role", "prometheus-discovery", "recap-demo")
    assert grants(role) == {("", "pods", verb, "") for verb in ("get", "list", "watch")}
    configmap = find("ConfigMap", "prometheus-config", "recap-system")
    config = yaml.safe_load(configmap["data"]["prometheus.yml"])
    assert set(config) == {"global", "scrape_configs"}  # no remote write, no rules, no alerts
    (job,) = config["scrape_configs"]
    assert job["kubernetes_sd_configs"] == [
        {"role": "pod", "namespaces": {"names": ["recap-demo"]}}
    ]
    relabels = job["relabel_configs"]
    targets = {r["target_label"]: r["source_labels"] for r in relabels if "target_label" in r}
    assert targets == {
        "namespace": ["__meta_kubernetes_namespace"],
        "pod": ["__meta_kubernetes_pod_name"],
        "app": ["__meta_kubernetes_pod_label_app"],
    }
    keeps = [r for r in relabels if r.get("action") == "keep"]
    assert {
        "source_labels": ["__meta_kubernetes_pod_label_app"],
        "regex": "frontend|backend",
        "action": "keep",
    } in keeps
    spec = pod_spec("prometheus", "recap-system")
    assert spec["serviceAccountName"] == "prometheus"
    assert {kind for volume in spec["volumes"] for kind in volume if kind != "name"} == {
        "configMap",
        "emptyDir",
    }
    (container,) = spec["containers"]
    assert "--storage.tsdb.retention.time=6h" in container["args"]
    assert not any("admin-api" in arg or "lifecycle" in arg for arg in container["args"])


def test_the_traffic_generator_is_the_demo_program_aimed_at_the_frontend() -> None:
    spec = pod_spec("traffic", "recap-system")
    assert find("Deployment", "traffic", "recap-system")["spec"]["replicas"] == 1
    (container,) = spec["containers"]
    assert container["image"] == "recap-demo/frontend:v1.0.0"
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env == {
        "DEMO_SERVICE": "traffic",
        "TRAFFIC_TARGET": "http://frontend.recap-demo.svc.cluster.local/",
        "TRAFFIC_RPS": "5",
    }


# --- no secrets, no local paths, a README that names every file ----------------------------


def kind_files() -> list[Path]:
    return sorted(
        path for path in KIND.rglob("*") if path.is_file() and "__pycache__" not in path.parts
    )


def test_no_secret_shapes_or_local_paths_in_the_kind_files() -> None:
    for path in [*kind_files(), ROOT / "infra" / "README.md"]:
        text = path.read_text(encoding="utf-8")
        assert not any(pattern.search(text) for pattern in SECRET_PATTERNS), path.name
        lowered = text.lower()
        assert not any(p in lowered for p in ("/users/", "/home/", "/var/folders/")), path.name


def test_the_readme_names_every_file_and_where_it_ran() -> None:
    readme = (KIND / "README.md").read_text(encoding="utf-8")
    for path in kind_files():
        if path.name != "README.md":
            assert f"`{path.relative_to(KIND).as_posix()}`" in readme, path
    assert "Run on a real cluster in M5" in readme  # and what is still to check, said so
    assert "Still to check" in readme


# --- scripts ---------------------------------------------------------------------------------


def bash() -> str:
    found = shutil.which("bash")
    if found is None:
        pytest.skip("bash is not installed")
    return found


def code_lines(path: Path) -> Iterator[str]:
    """The script's lines with continuations joined, comments left out."""
    for line in path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        if not line.lstrip().startswith("#"):
            yield line


@pytest.mark.parametrize("name", [*RUNNABLE, "lib.sh"])
def test_every_script_parses(name: str) -> None:
    result = subprocess.run([bash(), "-n", str(SCRIPTS / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_scripts_are_strict_and_share_the_guards() -> None:
    assert sorted(p.name for p in SCRIPTS.glob("*.sh")) == sorted([*RUNNABLE, "lib.sh"])
    for name in RUNNABLE:
        path = SCRIPTS / name
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n"), name
        assert "\nset -euo pipefail\n" in text, name
        assert '\nsource "$(dirname "$0")/lib.sh"\n' in text, name
        assert os.access(path, os.X_OK), f"{name} is not executable"
        if name not in ("setup.sh", "cleanup.sh"):  # setup creates the cluster; cleanup deletes it
            assert "\nrequire_our_cluster\n" in text, name
    for name in ("inject-fault.sh", "reset.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert (
            "\nrequire_identity recap-scenario\n" in text and '\nACTOR="recap-scenario"\n' in text
        )


def test_every_kubectl_call_names_the_private_kubeconfig_and_the_context() -> None:
    call = re.compile(r"(?:^|[;&|({]|\$\()\s*kubectl\s")
    calls = [
        (p.name, line) for p in SCRIPTS.glob("*.sh") for line in code_lines(p) if call.search(line)
    ]
    assert {name for name, _ in calls} == {"lib.sh"}  # every other script goes through its wrappers
    assert len(calls) == 4  # kadmin, kas, ca_fingerprint, api_server_of
    for _, line in calls:
        assert "--kubeconfig" in line and '--context "$KUBE_CONTEXT"' in line, line
    lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
    assert '\nKUBE_CONTEXT="kind-recap-m5"\n' in lib
    assert '\nKIND_HOME="${RECAP_KIND_HOME:-$HOME/.config/recap/kind}"\n' in lib
    assert '\nexport KUBECONFIG="$ADMIN_KUBECONFIG"\n' in lib
    kind_writes = re.compile(r"\bkind (?:create cluster|delete cluster|export kubeconfig)\b")
    for path in SCRIPTS.glob("*.sh"):
        for line in code_lines(path):
            assert not re.search(r"\.kube/", line), (path.name, line)  # never ~/.kube/config
            if kind_writes.search(line):
                assert "--kubeconfig" in line, (path.name, line)


def test_tokens_are_short_lived_and_never_on_a_command_line() -> None:
    mint = (SCRIPTS / "mint-tokens.sh").read_text(encoding="utf-8")
    assert "\numask 077\n" in mint and "\nset +x" in mint
    assert 'create token "$identity" --duration="$duration"' in mint
    assert '  cat >"$tmp" <<EOF\n' in mint and "      token: $token\n" in mint
    for path in SCRIPTS.glob("*.sh"):
        assert not any("--token" in line for line in code_lines(path)), path.name


# --- the scripts' helpers, run against a stub kubectl ----------------------------------------


def run_lib(snippet: str, home: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Source lib.sh in a fresh bash whose HOME is `home`, then run `snippet`."""
    script = f'set -euo pipefail\nsource "{SCRIPTS / "lib.sh"}"\n{snippet}\n'
    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), **env}
    return subprocess.run(
        [bash(), "-c", script], capture_output=True, text=True, env=environ, timeout=60
    )


@pytest.mark.parametrize(
    ("fields", "state"),
    [
        ("3|3|2|2|2|2|NewReplicaSetAvailable", "complete"),
        ("4|3|2|2|2|2|NewReplicaSetAvailable", "progressing"),  # the change is not observed yet
        ("4|4|2|1|3|2|ReplicaSetUpdated", "progressing"),  # one new pod, an old one still there
        ("4|4|2|2|3|2|ReplicaSetUpdated", "progressing"),  # all new, an old pod not yet gone
        ("4|4|2|2|2|1|ReplicaSetUpdated", "progressing"),  # a new pod not available yet
        ("4|4|2|1|3|2|ProgressDeadlineExceeded", "failed"),
        ("4|4|2||2|2|", "progressing"),  # a missing count is zero
    ],
)
def test_the_scripts_rollout_rule_is_the_plans(fields: str, state: str, tmp_path: Path) -> None:
    result = run_lib(f"kubectl() {{ printf '%s' '{fields}'; }}\nrollout_state backend", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == state


def recorded_calls(path: Path) -> list[list[str]]:
    return [call.splitlines() for call in path.read_text().split("<end>\n") if call]


STUB_RECORDER = 'kubectl() { printf "%s\\n" "$@" >>"$CALLS"; printf "<end>\\n" >>"$CALLS"; }'


def test_a_release_is_one_patch_of_image_and_change_cause_dry_run_first(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    snippet = f"{STUB_RECORDER}\nACTOR=recap-scenario\nset_release backend v1.1.0"
    result = run_lib(snippet, tmp_path, CALLS=str(calls))
    assert result.returncode == 0, result.stderr
    dry, real = recorded_calls(calls)
    kubeconfig = str(tmp_path / ".config" / "recap" / "kind" / "recap-scenario.kubeconfig")
    head = ["--kubeconfig", kubeconfig, "--context", "kind-recap-m5", "--namespace", "recap-demo"]
    assert real[:6] == head and real[6:11] == [
        "patch",
        "deployment",
        "backend",
        "--type",
        "strategic",
    ]
    assert dry == [*real, "--dry-run=server"]
    patch = json.loads(real[real.index("--patch") + 1])
    assert patch == {
        "metadata": {"annotations": {"kubernetes.io/change-cause": "release v1.1.0"}},
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "backend", "image": "recap-demo/backend:v1.1.0"}]}
            }
        },
    }
    assert patch["spec"]["template"]["spec"]["containers"][0]["image"] in DEMO_IMAGES


def test_a_restart_is_the_patch_rollout_restart_sends(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    result = run_lib(f"{STUB_RECORDER}\nrestart_deployment backend", tmp_path, CALLS=str(calls))
    assert result.returncode == 0, result.stderr
    dry, real = recorded_calls(calls)
    assert dry == [*real, "--dry-run=server"]
    assert real[1] == str(tmp_path / ".config" / "recap" / "kind" / "admin.kubeconfig")
    patch = json.loads(real[real.index("--patch") + 1])
    stamp = patch["spec"]["template"]["metadata"]["annotations"][
        "kubectl.kubernetes.io/restartedAt"
    ]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp)


@pytest.mark.parametrize(
    ("snippet", "refusal"),
    [
        ("set_release database v1.0.0", "not a demo Deployment"),
        ("set_release backend latest", "not a version"),
        ("restart_deployment prometheus", "not a demo Deployment"),
        ("kas cluster-admin get pods", "not one of RECAP's identities"),
    ],
)
def test_the_helpers_refuse_anything_outside_the_demo(
    snippet: str, refusal: str, tmp_path: Path
) -> None:
    calls = tmp_path / "calls"
    result = run_lib(f"{STUB_RECORDER}\n{snippet}", tmp_path, CALLS=str(calls))
    assert result.returncode == 1 and refusal in result.stderr
    assert not calls.exists()  # refused before any kubectl call


@pytest.fixture(scope="module")
def ca_pem(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A throwaway self-signed CA certificate (its key stays in a temporary folder)."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    folder = tmp_path_factory.mktemp("ca")
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
        + ["-subj", "/CN=recap-test-ca", "-keyout", str(folder / "key.pem")]
        + ["-out", str(folder / "ca.pem")],
        check=True,
        capture_output=True,
    )
    return (folder / "ca.pem").read_text()


STUB_CLUSTER = """
kubectl() {
  case "$*" in
    *"config get-contexts"*) return 0 ;;
    *"certificate-authority-data"*) printf '%s' "$FAKE_CA" ;;
    *"cluster.server"*) printf '%s' "$FAKE_SERVER" ;;
    *) echo "unexpected kubectl call: $*" >&2; return 99 ;;
  esac
}
"""


@contextmanager
def credentials(home: Path, fingerprint: str | None, admin: bool = True) -> Iterator[Path]:
    folder = home / ".config" / "recap" / "kind"
    folder.mkdir(parents=True)
    if admin:
        (folder / "admin.kubeconfig").write_text("# stands in for the admin kubeconfig\n")
    if fingerprint is not None:
        (folder / "ca.sha256").write_text(fingerprint + "\n")
    yield folder


def test_the_ca_fingerprint_is_the_sha256_of_the_der_certificate(
    ca_pem: str, tmp_path: Path
) -> None:
    """The definition RECAP's own preflight must use to compare with ca.sha256."""
    expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(ca_pem)).hexdigest()
    fake_ca = base64.b64encode(ca_pem.encode()).decode()
    with credentials(tmp_path, fingerprint=None):
        result = run_lib(
            f'{STUB_CLUSTER}\nca_fingerprint "$ADMIN_KUBECONFIG"', tmp_path, FAKE_CA=fake_ca
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected and re.fullmatch(r"[0-9a-f]{64}", expected)


@pytest.mark.parametrize(
    ("server", "recorded", "admin", "refusal"),
    [
        ("https://127.0.0.1:6443", "match", True, None),
        ("https://10.0.0.5:6443", "match", True, "not on 127.0.0.1"),
        ("https://127.0.0.1.example.com:6443", "match", True, "not on 127.0.0.1"),
        ("https://127.0.0.1:6443", "0" * 64, True, "not the one recorded at creation"),
        ("https://127.0.0.1:6443", None, True, "no CA fingerprint recorded"),
        ("https://127.0.0.1:6443", "match", False, "no admin kubeconfig"),
    ],
)
def test_only_our_loopback_cluster_with_the_recorded_ca_is_accepted(
    ca_pem: str,
    tmp_path: Path,
    server: str,
    recorded: str | None,
    admin: bool,
    refusal: str | None,
) -> None:
    match = hashlib.sha256(ssl.PEM_cert_to_DER_cert(ca_pem)).hexdigest()
    fingerprint = match if recorded == "match" else recorded
    fake_ca = base64.b64encode(ca_pem.encode()).decode()
    with credentials(tmp_path, fingerprint, admin=admin):
        result = run_lib(
            f'{STUB_CLUSTER}\nrequire_our_cluster\necho "accepted"',
            tmp_path,
            FAKE_CA=fake_ca,
            FAKE_SERVER=server,
        )
    if refusal is None:
        assert result.returncode == 0 and result.stdout.strip() == "accepted", result.stderr
    else:
        assert result.returncode == 1 and refusal in result.stderr
        assert "accepted" not in result.stdout


@pytest.mark.parametrize(
    ("home", "refusal"), [("relative/kind", "absolute path"), ("<repo>", "inside the repository")]
)
def test_the_credentials_folder_must_be_absolute_and_outside_the_repository(
    home: str, refusal: str, tmp_path: Path
) -> None:
    kind_home = str(ROOT / "infra" / "kind-credentials") if home == "<repo>" else home
    result = run_lib("check_kind_home", tmp_path, RECAP_KIND_HOME=kind_home)
    assert result.returncode == 1 and refusal in result.stderr
    assert run_lib("check_kind_home", tmp_path).returncode == 0  # the default is outside


@contextmanager
def stub_tools(tmp_path: Path) -> Iterator[tuple[dict[str, str], Path]]:
    """PATH with kubectl, kind, docker, curl and openssl stubs that record any call."""
    stubs, marker = tmp_path / "bin", tmp_path / "called"
    stubs.mkdir()
    for tool in ("kubectl", "kind", "docker", "curl", "openssl"):
        stub = stubs / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >>"{marker}"\nexit 99\n')
        stub.chmod(0o755)
    yield (
        {"PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(tmp_path)},
        marker,
    )


@pytest.mark.parametrize(
    ("script", "args", "message"),
    [
        ("inject-fault.sh", [], "usage:"),
        ("inject-fault.sh", ["delete-everything"], "usage:"),
        ("inject-fault.sh", ["bad-release", "now"], "usage:"),
        ("mint-tokens.sh", ["--duration", "5m"], "usage:"),
        ("mint-tokens.sh", ["--duration", "25h"], "usage:"),
        ("mint-tokens.sh", ["--duration", "1d"], "usage:"),
        ("mint-tokens.sh", ["--duration"], "usage:"),
        ("reset.sh", ["--timeout", "soon"], "usage:"),
        ("reset.sh", ["--force"], "usage:"),
        ("status.sh", ["--evidence"], "usage:"),
        ("cleanup.sh", ["--everything"], "usage:"),
        ("setup.sh", ["now"], "usage:"),
        # Valid arguments, but no cluster of ours: refused before any kubectl call.
        ("inject-fault.sh", ["bad-release"], "no admin kubeconfig"),
        ("reset.sh", [], "no admin kubeconfig"),
        ("mint-tokens.sh", [], "no admin kubeconfig"),
        ("status.sh", [], "no Kind cluster named recap-m5"),
        # Never unattended without --yes.
        ("cleanup.sh", [], "not a terminal"),
    ],
)
def test_scripts_refuse_before_touching_anything(
    script: str, args: list[str], message: str, tmp_path: Path
) -> None:
    with stub_tools(tmp_path) as (environ, marker):
        result = subprocess.run(
            [bash(), str(SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            env=environ,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
    assert result.returncode == 1, result.stderr
    assert message in result.stderr
    called = marker.read_text().splitlines() if marker.exists() else []
    # status.sh asks kind for its clusters first (read-only); nothing else is ever called.
    assert all(line.startswith("kind get clusters") for line in called), called


def test_the_probe_helper_counts_what_the_demo_program_answers(tmp_path: Path) -> None:
    """The scripts' probe against the real program: backend v1.1.0, ids in turn, 2 of 8 fail."""
    if shutil.which("curl") is None:
        pytest.skip("curl is not installed")
    environ = {
        "DEMO_SERVICE": "backend",
        "DEMO_BUILD": "v1.1.0",
        "HOST": "127.0.0.1",
        "PORT": "0",
        "MEMORY_LIMIT_BYTES": "0",
    }
    with demo_process(environ) as url:
        result = run_lib(f'probe "{url}/items" 8\nhttp_code "{url}/healthz"', tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n")[:2] == ["2 8", "200"]


@contextmanager
def demo_process(environ: dict[str, str]) -> Iterator[str]:
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(APP)],
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert process.stdout is not None
        first = process.stdout.readline()
        port = re.search(r"serving on :(\d+)", first)
        assert port is not None, first
        yield f"http://127.0.0.1:{port[1]}"
    finally:
        process.terminate()
        process.wait(timeout=10)
