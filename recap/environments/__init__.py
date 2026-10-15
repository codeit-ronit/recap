"""Environment adapters: the only code that knows what the target infrastructure is.

RECAP reaches infrastructure through one interface, `recap.core.interfaces.TargetEnvironment`.
An adapter translates its infrastructure into the shared observation models in
`recap/core/observations.py`, so agents, RAG, memory, routing, guardrails and the harness
never depend on a concrete environment.

    recap/environments/simulated/   V1: a deterministic local simulation (the default)
    integrations/sregym/            planned: SREGym adapter, interface only (not integrated)

configs/environment.yaml chooses the adapter, so switching infrastructure is a config
change plus one new adapter class (with matching log conventions, allowlists and runbooks).
See README.md in this folder.
"""
