"""Export real example JSON for RECAP's core contracts (tests/fixtures/contracts/).

    python scripts/export_contract_examples.py

Runs the bad-deployment scenario once (simulated, auto-approved, temporary data dir) and
saves one real instance of each core contract. tests/unit/test_contract_examples.py checks
that every example still validates and round-trips, so an accidental rename of a field
breaks a test. Re-run this script only when a contract changes on purpose.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import load_settings
from recap.core.models import RunConfig
from recap.runtime import Runtime

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "contracts"


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings(data_dir=Path(tmp))
        options = dict(settings.environment.options, tool_latency_ms=0, write_latency_ms=0)
        env = settings.environment.model_copy(update={"options": options})
        runtime = Runtime(settings.model_copy(update={"environment": env}))
        record = await runtime.run_incident("bad-deployment", RunConfig(approval_mode="auto"))
        evidence = runtime.runs.evidence(record.run_id)
        events = runtime.events.list_events(record.run_id)
        episode = runtime.memory.get(record.episode_id or "")

    examples = {
        "incident": record.incident,
        "evidence": evidence[0],
        "strategy_decision": record.strategies[0],
        "agent_finding": record.findings[0],
        "verification_report": record.verification,
        "mitigation_proposal": record.proposals[0],
        "safety_decision": record.safety_decisions[0],
        "approval": record.approvals[0],
        "recovery_result": record.recovery,
        "experience_episode": episode,
        "trace_event": next(e for e in events if e.category == "tool.call"),
        "run_record": record,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    for name, model in examples.items():
        assert model is not None, f"the example run produced no {name}"
        data = json.loads(model.model_dump_json())
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(examples)} examples to {OUT} (run ended {record.state.value})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
