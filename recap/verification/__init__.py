"""Verification: checking results against independent evidence instead of trusting claims.

The recovery oracle (oracle.py) decides whether a mitigation actually recovered the
environment, using ground-truth health that agents can neither see nor influence.
See README.md in this folder.
"""

from __future__ import annotations

from recap.verification.oracle import EnvironmentHealthOracle, recovery_result

__all__ = ["EnvironmentHealthOracle", "recovery_result"]
