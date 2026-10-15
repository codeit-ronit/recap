"""Decision providers: the "brain" that tells each agent what to do next.

A provider implements recap.core.interfaces.LLMProvider. Given an InvestigationRequest it
returns the next read-only tool calls or final root-cause hypotheses; given a
ProposalRequest it drafts one remediation. Every provider returns the same validated
Pydantic decision types, so the harness never knows which one is running.

    deterministic  DeterministicProvider - the DEFAULT. A scripted baseline policy built
                   from playbooks and heuristics. It is NOT an LLM: no network, $0 cost.
    anthropic      AnthropicProvider - OPTIONAL Claude adapter (official Anthropic SDK).
                   Opt-in only; it uses your own API key and billing.

Build one by name with recap.providers.factory.create_provider(...). The prompts and
output schemas an LLM provider uses live in recap.providers.prompts (versioned).
"""

from __future__ import annotations
