"""Context engineering: what a provider is allowed to see, and how much of it.

`evidence.py` keeps the original observations of a run, `tokens.py` estimates their size,
and `packer.py` turns them (plus runbook chunks and past episodes) into bounded, ranked
context packs that always point back to the originals.
"""
