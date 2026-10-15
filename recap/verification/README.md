# Verification: did the system really recover?

After a mitigation, RECAP must answer one question honestly: *is the system healthy now
because of what we did?* The **recovery oracle** (`oracle.py`) answers it from environment
ground truth, never from what an agent claims.

## How it decides

`EnvironmentHealthOracle.probe()` calls `TargetEnvironment.health_check()` once and applies
a fixed rule to every service:

```
healthy(service) = status == "Running"
                   and ready_replicas == replicas
                   and error_rate < max_error_rate      # 0.05, configs/policies.yaml -> oracle
healthy(system)  = every service is healthy (an empty report never counts as healthy)
```

The controller probes **before** and **after** a mitigation. `recovery_result(before, after,
oracle_name)` turns the two probes into a `RecoveryResult`:

```
verified = after.healthy and not before.healthy
```

Only a verified recovery may become a *successful* episode in experience memory. Both
probes' per-service signals are kept in `observed_signals` for the audit trail.

## Why the oracle is independent

1. **Separate channel.** It reads `health_check()`, a trusted method that is never exposed
   as an MCP tool. Agents only see the noisy `observe_*` views through MCP and cannot call,
   change or fake the ground truth.
2. **Separate process.** The MCP tool server runs as a subprocess with its own environment
   instance. The oracle uses the controller's own instance, which reads the same sandbox
   state file directly. A broken or compromised tool server cannot answer the oracle's
   question for it.
3. **No agent input.** The oracle takes no hypothesis, summary or "I fixed it" message as
   input. Its only inputs are the environment and a threshold from trusted config, so no
   text an agent writes (including injected text) can influence the verdict.

## Limits (V1)

The ground truth comes from the local simulator, so a verified recovery means "recovered
in the simulation", and it is labeled that way. A real environment needs its own
ground-truth source (for SREGym, the benchmark's mitigation check where one exists); see
`integrations/sregym/README.md`. The oracle code itself works with any adapter, as the
tests show with `tests/fakes.py`. The controller builds its oracle through
`ControllerDeps.oracle_factory` (default `EnvironmentHealthOracle`), so a benchmark adapter
can bring its own judge behind the `RecoveryOracle` interface.
