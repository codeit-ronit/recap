"""Agents: who investigates an incident, and the trusted code that checks their claims.

- roles.py     the investigation roles and their specs from configs/agents.yaml
- verifier.py  EvidenceVerifier: rule-based checks of hypotheses and fixes (plain code, not an LLM)
- merger.py    how several agents' claims are merged, chosen between and disagreements recorded

The observe -> reason -> act loop every agent runs lives in recap/harness/loop.py, and what an
agent decides comes from a provider (recap/providers). See README.md here for the details.
"""
