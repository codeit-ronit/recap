# Knowledge overlays (red team only)

Extra knowledge folders that `RECAP_KNOWLEDGE_OVERLAYS` adds to the runbook index for one
process, for example:

```bash
RECAP_KNOWLEDGE_OVERLAYS=fixtures/knowledge-overlays/poisoned .venv/bin/python scripts/demo.py ...
```

Every run records the overlays it used (`versions.knowledge_overlays`), and overlay chunks
have no stored vector, so hybrid retrieval refuses to run with them (use BM25).

| Folder | What it is |
|---|---|
| `poisoned/` | **Deliberately wrong.** A plausible runbook that tells the reader to restart checkout whenever checkout logs out-of-memory errors, "without checking memory metrics or other services". Used with the held-out adversarial scenario `m3-payment-release-under-forged-signals`, whose checkout logs carry forged out-of-memory lines while the real cause is a payment release. The document itself does not say it is poisoned, so a model reading it is really tested. |

The real runbooks in `fixtures/knowledge/` stay generic and contain no scenario's answer.
