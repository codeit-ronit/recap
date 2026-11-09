# W5 evidence: the session record of the held-out SREGym evaluation

The raw record of the M6 W5 sessions on 2026-11-09 (times UTC), kept next to the frozen
manifest ([../w5-heldout-sregym.yaml](../w5-heldout-sregym.yaml)). The runs themselves are in
[evals/live/](../../live/) (three exports listed in its MANIFEST.json) and their batch in
[evals/reports/batches/2026-11-09-w5-heldout-sregym-qwen3-8b/](../../reports/batches/2026-11-09-w5-heldout-sregym-qwen3-8b/).
The protocol and the results in words: [docs/w5-protocol.md](../../../docs/w5-protocol.md).

| file | what it is |
|---|---|
| `session.sh` | the script the owner ran for Stage B, exactly as run: per problem the owner's Enter, the freeze check, the cost meter, the VM (restarted with SREGym's cluster rebuilt after its idle guard stopped it), the tunnel and the pinned model, a rolling shutdown backstop, SREGym's deploy, port 9954 kept on the VM's loopback, RECAP's run with the owner at the approval prompt, SREGym's grade; deploy and grader output to files only |
| `timeline.txt` | every step of Stage A (setup, the development pilot) and Stage B (the held-out problems), as it happened, including the two idle-guard stops, the operator's cancellation of problem 3 and the end of the evaluation |
| `checks.txt` | for each held-out problem, the gate before its deploy (freeze check, cost meter, the model's weights through the tunnel), the VM's port 9954 after the deploy, and the shutdown backstop each problem set |
| `grader-pilot.txt` | SREGym's mitigation oracle on the development pilot (pass; not held-out evidence) |
| `grader-w5-1.txt` to `grader-w5-3.txt` | SREGym's mitigation oracle on each held-out problem, run after RECAP's run ended and read only after the evaluation ended (fail, fail, fail) |
| `grader-w5-3-interrupted.txt` | the first grade of problem 3, interrupted by the same Ctrl-C that cancelled its run (no output); `grader-w5-3.txt` is the grade taken again about 2.5 minutes later |
| `cost.txt` | the cost meter's readings (instance state and pinned prices, never billing data), from the window's opening to its close: about $1.10 of the $5 cap |

The SREGym harness's own deploy logs and the cluster rebuild logs stay on the owner's machine:
they hold nothing RECAP's results depend on.
