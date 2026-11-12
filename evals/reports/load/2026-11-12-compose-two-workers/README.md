# Load test: the Compose stack with two workers (2026-11-12)

The raw output of M6 W6's load test, read in words in
[docs/performance-and-reliability.md](../../../../docs/performance-and-reliability.md).
One laptop (macOS, arm64, 8 CPUs, 16 GiB), Docker in a colima VM (4 CPUs, 3.8 GiB); the Compose
stack of [infra/compose/](../../../../infra/compose/README.md) with Redis, the API, two worker
replicas of two incidents each and Prometheus; the simulated environment and the scripted
baseline (no model). Not a capacity figure for any deployment.

| File | What it is |
|---|---|
| `run-1.json`, `run-2.json` | `scripts/api_load_test.py --url ... --loads 4,16,40 --docker-stats`, before the scenario-loader fix (run 1 has no per-route latency yet) |
| `run-3.json` | the same, after the fix (scenario files read with PyYAML's C safe loader) |
| `prometheus-after-run-1.json` | Prometheus's own answers to the queries in the report, after run 1 |
| `prometheus-targets.txt` | the targets Prometheus found before the load: the API and both workers, all up |

Each run file holds, per offered load, every run's state, duration, queue wait and client time,
the HTTP latencies overall and by route, the 429s and 5xx, the peak CPU and memory of each
container, and the machine it ran on.
