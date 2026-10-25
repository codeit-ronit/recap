"""Episodic experience memory: what RECAP learned from its own past runs.

Kept separate from the runbook RAG index (generic knowledge). A finished run is stored
as one episode, with its own kind (scripted runs in DEMO, real models in LIVE), unless
memory writes are off (as for a replay), the run crashed, or the scenario is a held-out
test case; for a new incident, similar episodes are retrieved.

    similarity.py     word-overlap score between an incident and a past episode
    store.py          SQLite ExperienceStore, partition rules (MemoryAccess), search, reuse checks
    consolidation.py  Consolidator: turns a finished RunRecord into an ExperienceEpisode

Being similar is not the same as being safe to reuse: see `store.assess_reuse`.
"""
