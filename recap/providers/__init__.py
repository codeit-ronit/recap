"""Decision providers: the "brain" that tells each agent what to do next.

A provider implements recap.core.interfaces.LLMProvider. Given an InvestigationRequest it
returns the next read-only tool calls or final root-cause hypotheses; given a
ProposalRequest it drafts one remediation. Every provider returns the same validated
Pydantic decision types, so the harness never knows which one is running.

    deterministic  DeterministicProvider - the DEFAULT. A scripted baseline policy built
                   from playbooks and heuristics. It is NOT an LLM: no network, $0 cost.
    ollama         OllamaProvider - a real local model (qwen3:8b) on this machine. $0.
    groq           FreeHostedProvider - a real hosted model on Groq's FREE plan, behind a
                   fail-closed guard (quota.py). $0, opt-in per process.
    replay         a recorded real-model run, answered offline (no model is called).
    anthropic      AnthropicProvider - Claude via the official SDK. PAID: disabled for this
                   $0 project (needs RECAP_ALLOW_PAID_LLM=1 and your own key).

Build one by name with recap.providers.factory.create_provider(...). The free and local
providers speak the step protocol in recap.providers.protocol (prompts-v4); the Claude
adapter uses recap.providers.prompts (prompts-v3). See README.md in this package.
"""

from __future__ import annotations
