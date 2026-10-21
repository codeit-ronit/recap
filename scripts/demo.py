"""Run one RECAP incident scenario end to end and print a live, readable timeline.

    python scripts/demo.py --list
    python scripts/demo.py --scenario bad-deployment            # you approve the fix
    python scripts/demo.py --scenario ambiguous-incident --approve auto
    python scripts/demo.py --scenario malicious-log
    python scripts/demo.py --scenario budget-exhausted
    python scripts/demo.py --scenario bad-deployment --provider ollama   # a real local LLM

Everything runs locally against the SIMULATED environment through a real MCP server
subprocess. Nothing here touches a real cluster, cloud account or paid API. With
--provider ollama the decisions come from a real model on this machine (about a minute
each); with --provider groq from Groq's free plan (needs RECAP_ALLOW_LIVE_FREE_INFERENCE=1
and the owner's free-plan confirmation, see docs/provider-options.md).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.models import (
    EnvironmentInfo,
    EpisodeOutcome,
    MemoryPartition,
    RunConfig,
    RunRecord,
    ScenarioInfo,
    TraceEvent,
)
from recap.runtime import Runtime
from recap.safety.approvals import ConsoleApprovalChannel
from recap.telemetry.tracer import TraceCategory as C

LINE = "═" * 78
# What each run origin means, in the words the dashboard's badge uses.
ORIGIN_TEXT = {
    "scripted": "SCRIPTED baseline policy, NOT an LLM",
    "local_llm": "LOCAL LLM: real inference on this machine (Ollama), no API, $0",
    "free_hosted_llm": "FREE HOSTED LLM: real inference on a free plan, checked every call",
    "replay": "REPLAY of a recorded LLM run: no model is called",
    "paid_llm": "PAID LLM API (disabled for this $0 project)",
}


class ConsoleSink:
    """A trace sink that prints the important events as they happen."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.quiet = False
        self.cost = ""  # the run summary's cost label, printed at the end

    def write(self, e: TraceEvent) -> None:
        if self.quiet and e.category not in (C.STATE,):
            return
        text = self._format(e)
        if text:
            stamp = f"[{time.perf_counter() - self.started:6.2f}s] "
            # Continuation lines are indented under the first one.
            print(stamp + text.replace("\n", "\n" + " " * len(stamp)), flush=True)

    def _format(self, e: TraceEvent) -> str | None:
        p = e.payload
        if e.category == C.STATE and p.get("from"):
            return f"STATE     {p['from']} → {p['to']} · {p.get('reason', '')}"
        if e.category == C.RETRIEVAL:
            refs = ", ".join(f"{r['chunk_id']} ({r['score']})" for r in p["results"][:3])
            return f"RAG       [{p['stage']}] {p['reason']}: {refs or 'no results'}"
        if e.category == C.RETRIEVAL_SKIP:
            return f"RAG       [{p['stage']}] skipped: {p['reason']}"
        if e.category == C.MEMORY_SEARCH:
            hits = p["hits"]
            if not hits:
                return "MEMORY    no similar past episodes"
            first = hits[0]
            return (
                f"MEMORY    {len(hits)} similar episode(s); best {first['episode_id']} "
                f"sim={first['similarity']} outcome={first['outcome']}"
            )
        if e.category == C.MEMORY_ASSESS:
            return "\n".join(
                f"MEMORY    {h['episode_id']}: safe to reuse = "
                f"{'YES' if h['safe_to_reuse'] else 'NO'}"
                f" · {h['reason']}"
                for h in p["hits"]
            )
        if e.category == C.ROUTING:
            agents = ", ".join(p["agents"])
            return (
                f"ROUTE     round {p['round']}: {p['mode']} [{agents}] · "
                f"{p['rule_id']} · {p['reason']}"
            )
        if e.category == C.LLM_CALL:
            # One real model request: which model, what it chose, exact tokens, time.
            agent = e.agent or "controller"
            line = (
                f"LLM       {agent} · {p['model']} · {p['mode']} · {p['response_kind']} · "
                f"in {p.get('input_tokens')} / out {p.get('output_tokens')} tokens · "
                f"{p['duration_ms'] / 1000:.1f}s" + (" · REPLAYED" if p.get("replayed") else "")
            )
            problems = [note for note in p.get("notes", []) if note.startswith("unusable")]
            return line + "".join(f"\n          ✗ {note}" for note in problems)
        if e.category == C.RUN_SUMMARY:
            self.cost = p.get("cost", "")
            return None
        if e.category == C.TOOL_CALL:
            args = ",".join(f"{k}={v}" for k, v in p["args"].items())
            return f"  {e.agent:<28} → {p['tool']}({args})"
        if e.category == C.TOOL_RESULT and not p.get("ok"):
            return f"  {e.agent:<28} ✗ {p['tool']}: {p.get('error')}"
        if e.category == C.INJECTION:
            return f"⚠ SAFETY  injection-like text in {p.get('evidence_id')} ({p.get('patterns')})"
        if e.category == C.ORCHESTRATION and p.get("mode") == "parallel":
            return (
                f"PARALLEL  agents overlapped for {p['overlap_ms']:.0f} ms "
                f"(wall {p['wall_ms']:.0f} ms)"
            )
        if e.category == C.VERIFY_EVIDENCE and "status" in p:
            best = p.get("best") or {}
            lines = [f"VERIFY    {p['status']}: {best.get('claim', p.get('summary', ''))}"]
            for check in p["checks"]:
                if check["failed"]:  # show each rejected claim once, with its reasons
                    lines.append(f"          ✗ [{check['status']}] {check['claim'][:70]}")
                    lines += [f"              - {reason}" for reason in check["failed"][:2]]
            return "\n".join(lines)
        if e.category == C.PROPOSAL and p.get("proposal"):
            prop = p["proposal"]
            return (
                f"PROPOSAL  {prop['action']} on {prop['target']} {prop['params']} · "
                f"source={prop['source']} · trusted risk={p['trusted_risk']}"
            )
        if e.category == C.POLICY and p.get("risk") != "read":  # show write decisions only
            # The last rule evaluated is the one that decided; earlier ones passed.
            rule = (p.get("rule_ids") or ["?"])[-1]
            reason = "; ".join(p.get("reasons", []))
            return f"POLICY    {p.get('verdict')} for {p.get('tool')} · {rule} · {reason}"
        if e.category == C.APPROVAL_REQUESTED:
            return f"APPROVAL  requested via {p['channel']} (digest {p['digest'][:12]}…)"
        if e.category == C.APPROVAL_DECIDED:
            return f"APPROVAL  {p['decision']} by {p.get('actor', '-')}"
        if e.category == C.MITIGATION:
            return f"MITIGATE  {p['tool']} on {p['target']} ok={p['ok']} · {p.get('side_effects')}"
        if e.category == C.VERIFY_RECOVERY:
            verdict = "RECOVERED ✓" if p["verified"] else "NOT recovered ✗"
            return f"ORACLE    {verdict} · {p['oracle']} · unhealthy after: {p['unhealthy_after']}"
        if e.category == C.MEMORY_WRITE:
            return (
                f"MEMORY    episode {p['episode_id']} written "
                f"(outcome={p['outcome']}, verified={p['verified']})"
            )
        if e.category == C.BUDGET:
            return f"BUDGET    exhausted: {p['limit']} ({p['detail']})"
        if e.category == C.RUN_ERROR:
            return f"ERROR     {p['error']}"
        return None


def expected_state(scenario: ScenarioInfo, config: RunConfig) -> str:
    """What a correct run of this scenario should end in."""
    if scenario.expected.get("final_state"):
        return str(scenario.expected["final_state"])
    if (
        scenario.run_overrides.get("planner") == "prompt_injected"
        or config.planner == "prompt_injected"
    ):
        return "BLOCKED"
    return "RESOLVED"


async def ensure_prerequisites(runtime: Runtime, scenario: ScenarioInfo, sink: ConsoleSink) -> None:
    """Some demos need earlier experience (e.g. memory-caution needs a verified episode)."""
    episodes = runtime.memory.list([MemoryPartition.DEMO])
    for needed in scenario.requires_memory_of:
        if any(
            ep.scenario_id == needed and ep.outcome == EpisodeOutcome.SUCCESS and ep.verified
            for ep in episodes
        ):
            continue
        print(f"→ prerequisite: running '{needed}' first to create a verified experience episode")
        sink.quiet = True
        record = await runtime.run_incident(needed, RunConfig(approval_mode="auto"))
        sink.quiet = False
        print(f"→ prerequisite finished: {record.state.value} (episode {record.episode_id})\n")


def banner(info: EnvironmentInfo) -> str:
    """The environment label, taken from the adapter (never hard-coded)."""
    if info.is_simulated:
        return "SIMULATED LOCAL ENVIRONMENT · V1"
    return f"LIVE ENVIRONMENT ({info.kind}) · V1"


def print_summary(record: RunRecord, expected: str, runtime: Runtime, cost: str) -> None:
    print(LINE)
    print(f" RESULT  {record.state.value} — {record.outcome_note}")
    if record.verification and record.verification.best:
        print(f" DIAGNOSIS  {record.verification.best.claim}")
    for proposal in record.proposals:
        print(
            f" PROPOSAL   {proposal.action} on {proposal.target} {proposal.params} "
            f"({proposal.source})"
        )
    for action in record.actions:
        print(f" ACTION     {action.action} on {action.target}: {action.result} {action.note}")
    if record.recovery:
        r = record.recovery
        print(
            f" ORACLE     verified={r.verified} "
            f"(healthy before={r.healthy_before}, after={r.healthy_after})"
        )
    u = record.usage
    print(
        f" USAGE      steps={u.steps} tool_calls={u.tool_calls} est_tokens={u.estimated_tokens}"
        f" · model requests={u.model_requests} · {cost}"
    )
    print(f" EPISODE    {record.episode_id or 'not written'}")
    print(f" TRACE      {runtime.jsonl.path_for(record.run_id)}")
    ok = record.state.value == expected
    print(f" EXPECTED   {expected} → {'✓ matches' if ok else '✗ differs'}")
    if record.environment and record.environment.is_simulated:
        print(f" {banner(record.environment)} — no real system was changed")
    print(LINE)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--scenario", help="scenario id (see --list)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument(
        "--approve",
        choices=["ask", "auto"],
        default="ask" if sys.stdin.isatty() else "auto",
        help="ask = you approve in the terminal; auto = simulation auto-approver",
    )
    parser.add_argument(
        "--strategy", choices=["adaptive", "single", "parallel"], default="adaptive"
    )
    parser.add_argument("--no-rag", action="store_true", help="disable runbook retrieval")
    parser.add_argument("--no-memory", action="store_true", help="disable experience memory")
    parser.add_argument("--latency-ms", type=int, default=None, help="simulated tool latency")
    parser.add_argument(
        "--provider",
        default=None,
        help="decision provider from configs/providers.yaml (default: RECAP_PROVIDER or "
        "deterministic), e.g. ollama, groq, replay",
    )
    args = parser.parse_args()

    sink = ConsoleSink()
    runtime = Runtime(extra_sinks=[sink])
    scenarios = {s.id: s for s in runtime.list_scenarios()}
    if args.list or not args.scenario:
        for s in scenarios.values():
            print(f"{s.id:<34} [{s.partition.value:<5}] {s.title}")
        return 0
    if args.scenario not in scenarios:
        print(f"unknown scenario '{args.scenario}'. Use --list.")
        return 2
    scenario = scenarios[args.scenario]

    config = RunConfig(
        strategy=args.strategy,
        use_rag=not args.no_rag,
        use_memory=not args.no_memory,
        approval_mode="auto" if args.approve == "auto" else "manual",
        tool_latency_ms=args.latency_ms,
        provider=args.provider,
    )
    provider = runtime.make_provider(config)
    print(LINE)
    print(f" RECAP · {banner(runtime.environment_info())} · scenario: {scenario.id}")
    print(f" {scenario.title}")
    print(f" provider: {provider.info.name} — {ORIGIN_TEXT[provider.info.origin]}")
    print(
        " approvals: "
        + ("you decide in this terminal" if args.approve == "ask" else "simulation auto-approver")
    )
    print(LINE)

    await ensure_prerequisites(runtime, scenario, sink)
    sink.started = time.perf_counter()
    channel = ConsoleApprovalChannel(runtime.approvals) if args.approve == "ask" else None
    record = await runtime.run_incident(scenario.id, config, approval_channel=channel)
    expected = expected_state(scenario, record.config)
    print_summary(record, expected, runtime, sink.cost)
    return 0 if record.state.value == expected else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
