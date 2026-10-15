"""The trusted tool registry: which tools exist, how risky they are, and what arguments they take.

Where it sits: the MCP server *advertises* tools, but nothing it says is trusted. This
registry is built from configs/policies.yaml (RECAP's own trusted config) and is the only
source of truth for tool names, risk tiers and argument rules. The safety policy, the
tool gateway and the providers' tool descriptions all read from here.

Argument validation is strict on purpose: unknown arguments are rejected, types must be
exact (a bool is not an int, "5" is not 5), and services/namespaces must be allowlisted.
The registry also compares a server's advertised tool list with this trusted catalog
(`compare`), so lying or unexpected tools are reported instead of believed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from recap.core.config import ArgRule, PolicySettings, ToolPolicy
from recap.core.models import (
    Contract,
    DiscoveredTool,
    MitigationProposal,
    ToolRisk,
    ToolSpecView,
)

# ASCII digits only: "\d" would also accept look-alike Unicode digits such as "v١.٢.٣".
_VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
MAX_STRING_CHARS = 200
_TARGET_ARG_TYPES = ("service", "namespace")  # argument types a mitigation target can fill


class DiscoveryReport(Contract):
    """What an MCP server advertised, compared with the trusted registry."""

    transport: str
    advertised: list[str]
    usable: list[str]  # advertised and trusted
    unknown_tools: list[str]  # advertised but not trusted: never callable
    missing_tools: list[str]  # trusted but not offered by this server
    annotation_mismatches: list[str]  # server hints that disagree with the trusted risk tier


class ToolRegistry:
    """Trusted catalog of tools, built only from configs/policies.yaml."""

    def __init__(self, policies: PolicySettings) -> None:
        self._tools: dict[str, ToolPolicy] = dict(policies.tools)
        self._services = frozenset(policies.allowed_services)
        self._namespaces = frozenset(policies.allowed_namespaces)
        self._remediation_tools = list(policies.remediation_tools)

    # --- lookups ------------------------------------------------------------------------

    def get(self, name: str) -> ToolPolicy | None:
        """The trusted policy for a tool, or None if RECAP does not know it."""
        return self._tools.get(name)

    def is_known(self, name: str) -> bool:
        """True only for tools listed in the trusted config."""
        return name in self._tools

    def risk_of(self, name: str) -> ToolRisk | None:
        """The trusted risk tier of a tool (None for unknown tools)."""
        policy = self._tools.get(name)
        return policy.risk if policy else None

    # --- what providers are told about tools ---------------------------------------------

    def view(self, name: str) -> ToolSpecView:
        """Description of one tool for a provider. Raises KeyError for unknown tools."""
        policy = self._tools.get(name)
        if policy is None:
            raise KeyError(f"unknown tool '{name}'")
        return ToolSpecView(
            name=name,
            description=policy.description,  # from trusted config, never from the server
            risk=policy.risk,
            args={arg: self._describe_arg(rule) for arg, rule in policy.args.items()},
        )

    def views(self, names: Iterable[str]) -> list[ToolSpecView]:
        """Descriptions of several tools, in the given order."""
        return [self.view(name) for name in names]

    def remediation_views(self) -> list[ToolSpecView]:
        """The only tools a planner may propose as a mitigation."""
        return self.views(self._remediation_tools)

    def _describe_arg(self, rule: ArgRule) -> dict[str, Any]:
        described = rule.model_dump(exclude_none=True)
        # Tell providers the allowlist up front so they do not waste calls on invalid names.
        if rule.type == "service":
            described["values"] = sorted(self._services)
        elif rule.type == "namespace":
            described["values"] = sorted(self._namespaces)
        return described

    # --- validation ----------------------------------------------------------------------

    def validate_args(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Check arguments against the trusted rules. Returns (clean args, errors).

        Clean args contain only valid, non-None values. Callers must treat any error as a
        refusal. `approval_id` is not a registry argument: the gateway adds it itself.
        """
        policy = self._tools.get(name)
        if policy is None:
            return {}, [f"unknown tool {_preview(name)}"]
        errors = [f"unknown argument {_preview(key)}" for key in args if key not in policy.args]
        clean: dict[str, Any] = {}
        for arg, rule in policy.args.items():
            value = args.get(arg)
            if value is None:  # None means "not given"
                if rule.required:
                    errors.append(f"missing required argument '{arg}'")
                continue
            problem = self._check_value(arg, rule, value)
            if problem:
                errors.append(problem)
            else:
                clean[arg] = value
        return clean, errors

    def _check_value(self, arg: str, rule: ArgRule, value: Any) -> str | None:
        """Return a short problem description, or None if the value is valid."""
        # isinstance checks come first so unhashable values never reach a set lookup.
        if rule.type == "service":
            if isinstance(value, str) and value in self._services:
                return None
            return f"{arg}: {_preview(value)} is not an allowlisted service"
        if rule.type == "namespace":
            if isinstance(value, str) and value in self._namespaces:
                return None
            return f"{arg}: {_preview(value)} is not an allowlisted namespace"
        if rule.type == "version":
            if isinstance(value, str) and _VERSION_RE.fullmatch(value):
                return None
            return f"{arg}: {_preview(value)} is not a version like v1.2.3"
        if rule.type == "int":
            return _check_int(arg, rule, value)
        if rule.type == "enum":
            if isinstance(value, str) and value in (rule.values or []):
                return None
            return f"{arg}: {_preview(value)} is not one of {rule.values}"
        if rule.type == "string":
            if isinstance(value, str) and len(value) <= MAX_STRING_CHARS:
                return None
            return f"{arg} must be a string of at most {MAX_STRING_CHARS} characters"
        return f"{arg}: unsupported rule type '{rule.type}'"  # fail closed on unknown rules

    # --- comparing a server's advertised tools with the trusted catalog ------------------

    def compare(self, transport: str, tools: list[DiscoveredTool]) -> DiscoveryReport:
        """Compare what a server advertises with the trusted catalog. Nothing is believed."""
        names = [tool.name for tool in tools]
        return DiscoveryReport(
            transport=transport,
            advertised=names,
            usable=[name for name in names if name in self._tools],
            unknown_tools=[name for name in names if name not in self._tools],
            missing_tools=[name for name in self._tools if name not in names],
            annotation_mismatches=[p for tool in tools for p in self._hint_mismatches(tool)],
        )

    def _hint_mismatches(self, tool: DiscoveredTool) -> list[str]:
        """Server annotations that disagree with the trusted risk tier (reported, never used).

        A server claiming delete_namespace is read-only is exactly the lie a naive client
        would believe, so discovery surfaces it. Unknown tools are reported separately.
        """
        risk = self.risk_of(tool.name)
        if risk is None:
            return []
        problems = []
        read_only = _hint(tool.annotations, "readOnlyHint", "read_only_hint")
        if read_only is not None and read_only != (risk == ToolRisk.READ):
            problems.append(f"{tool.name}: server says readOnlyHint={read_only}, trusted: {risk}")
        destructive = _hint(tool.annotations, "destructiveHint", "destructive_hint")
        # MCP's "destructive" is ambiguous for reversible writes, so only clear tiers compare.
        clear_tier = risk in (ToolRisk.READ, ToolRisk.DESTRUCTIVE)
        if destructive is not None and clear_tier and destructive != (risk == ToolRisk.DESTRUCTIVE):
            problems.append(
                f"{tool.name}: server says destructiveHint={destructive}, trusted: {risk}"
            )
        return problems

    # --- mitigations ---------------------------------------------------------------------

    def target_arg(self, name: str) -> str | None:
        """The argument a mitigation's target fills: the tool's first service/namespace one.

        None for unknown tools and for tools without such an argument.
        """
        policy = self._tools.get(name)
        if policy is None:
            return None
        return next(
            (arg for arg, rule in policy.args.items() if rule.type in _TARGET_ARG_TYPES), None
        )

    def mitigation_args(self, proposal: MitigationProposal) -> dict[str, Any]:
        """Tool arguments for a proposal: the target fills the service/namespace argument.

        e.g. execute_rollback on target "checkout" with params {"to_version": "v2.4.0"}
        -> {"service": "checkout", "to_version": "v2.4.0"}. The target always wins, so
        params cannot point the action at a different service than the proposal names
        (and the safety policy denies a proposal whose params try).
        """
        params = dict(proposal.params)
        target_arg = self.target_arg(proposal.action)
        if target_arg is None:
            return params  # unknown tool (the policy denies it) or nothing to fill
        params.pop(target_arg, None)
        return {target_arg: proposal.target, **params}


def _check_int(arg: str, rule: ArgRule, value: Any) -> str | None:
    # bool is a subclass of int in Python, so True would otherwise pass as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{arg} must be an integer, got {_preview(value)}"
    if (rule.min is not None and value < rule.min) or (rule.max is not None and value > rule.max):
        return f"{arg} must be between {rule.min} and {rule.max}, got {value}"
    return None


def _hint(annotations: dict[str, Any], *keys: str) -> bool | None:
    """A boolean server hint under either naming style (readOnlyHint / read_only_hint)."""
    for key in keys:
        value = annotations.get(key)
        if isinstance(value, bool):
            return value
    return None


def _preview(value: Any, limit: int = 40) -> str:
    """Short repr of a value for error messages (inputs may be long, hostile model text)."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
