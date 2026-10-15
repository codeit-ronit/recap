"""The deterministic simulated environment: V1's TargetEnvironment adapter.

Everything RECAP "sees" and "repairs" in V1 happens inside this local simulation. Nothing
here talks to a real cluster. See recap/environments/README.md for how it works.
"""

from __future__ import annotations

from recap.environments.simulated.engine import SimulatedEnvironment

__all__ = ["SimulatedEnvironment"]
