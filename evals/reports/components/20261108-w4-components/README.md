# Component evaluations

> **SIMULATED incidents; small N; counts, not rates.** Each component is measured on its own and never merged into an end-to-end success rate.

- Report `20261108-w4-components`, created 2026-11-08T12:23:34.855386+00:00, code `46ea1d5f50cc`
- End-to-end batch read: `20261108-w4-scripted`

## Retrieval

Dataset `evals/datasets/retrieval-v1.yaml` (42 chunks); rankers: bm25, bm25+rerank, dense, hybrid; vectors `evals/datasets/query-vectors-v1.json` (ollama:nomic-embed-text, sha256:970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6)

| ranker | queries | partition | N | hit@1 | hit@2 | hit@4 | MRR@4 | remediation@2 | remediation@4 | wrong mechanism at rank 1 |
|---|---|---|---|---|---|---|---|---|---|---|
| bm25 | alert | all | 17 | 0 | 0 | 9 | 0.142 | 0 of 17 | 0 of 17 | 0 |
| bm25 | alert | demo | 6 | 0 | 0 | 4 | 0.181 | 0 of 6 | 0 of 6 | 0 |
| bm25 | alert | test | 8 | 0 | 0 | 3 | 0.094 | 0 of 8 | 0 of 8 | 0 |
| bm25 | alert | train | 3 | 0 | 0 | 2 | 0.194 | 0 of 3 | 0 of 3 | 0 |
| bm25 | proposal | all | 2 | 2 | 2 | 2 | 1.0 | 2 of 2 | 2 of 2 | 0 |
| bm25 | paraphrase | all | 12 | 4 | 7 | 10 | 0.535 | 1 of 8 | 1 of 8 | 0 |
| bm25+rerank | alert | all | 17 | 0 | 0 | 9 | 0.142 | 0 of 17 | 0 of 17 | 0 |
| bm25+rerank | alert | demo | 6 | 0 | 0 | 4 | 0.181 | 0 of 6 | 0 of 6 | 0 |
| bm25+rerank | alert | test | 8 | 0 | 0 | 3 | 0.094 | 0 of 8 | 0 of 8 | 0 |
| bm25+rerank | alert | train | 3 | 0 | 0 | 2 | 0.194 | 0 of 3 | 0 of 3 | 0 |
| bm25+rerank | proposal | all | 2 | 2 | 2 | 2 | 1.0 | 2 of 2 | 2 of 2 | 0 |
| bm25+rerank | paraphrase | all | 12 | 4 | 7 | 10 | 0.542 | 1 of 8 | 1 of 8 | 0 |
| dense | alert | all | 17 | 0 | 2 | 11 | 0.216 | 0 of 17 | 0 of 17 | 0 |
| dense | alert | demo | 6 | 0 | 1 | 4 | 0.222 | 0 of 6 | 0 of 6 | 0 |
| dense | alert | test | 8 | 0 | 0 | 4 | 0.156 | 0 of 8 | 0 of 8 | 0 |
| dense | alert | train | 3 | 0 | 1 | 3 | 0.361 | 0 of 3 | 0 of 3 | 0 |
| dense | proposal | all | 2 | 2 | 2 | 2 | 1.0 | 2 of 2 | 2 of 2 | 0 |
| dense | paraphrase | all | 12 | 6 | 10 | 11 | 0.694 | 3 of 8 | 5 of 8 | 0 |
| hybrid | alert | all | 17 | 0 | 0 | 7 | 0.123 | 0 of 17 | 0 of 17 | 0 |
| hybrid | alert | demo | 6 | 0 | 0 | 2 | 0.097 | 0 of 6 | 0 of 6 | 0 |
| hybrid | alert | test | 8 | 0 | 0 | 3 | 0.104 | 0 of 8 | 0 of 8 | 0 |
| hybrid | alert | train | 3 | 0 | 0 | 2 | 0.222 | 0 of 3 | 0 of 3 | 0 |
| hybrid | proposal | all | 2 | 2 | 2 | 2 | 1.0 | 2 of 2 | 2 of 2 | 0 |
| hybrid | paraphrase | all | 12 | 5 | 9 | 10 | 0.604 | 3 of 8 | 4 of 8 | 0 |
- Poisoned runbook loaded, bm25+rerank, alert queries (top 4): in the results of 13 of 17; displacing the genuine remediation in 0.
- Poisoned runbook loaded, bm25+rerank, proposal queries (top 2): in the results of 1 of 2; displacing the genuine remediation in 0.
- Poisoned runbook loaded, bm25+rerank, paraphrase queries (top 4): in the results of 3 of 12; displacing the genuine remediation in 0.

## Memory

- 58 runs with memory on; 51 found a similar episode; 79 hits, 0 of them never assessed (the run stopped first); assessed: look_alike 65, same_diagnosis 7, twin 7.
- Verdicts right: 79 of 79 labelled; unsafe reuse (a false 'safe'): 0; unlabelled hits judged safe: 0; twins accepted: 7 of 7.
- Fix taken from memory: no_rag/test-cart-memory-leak.
- Leakage (a hit from outside TRAIN): 0

## Routing (`full_adaptive`)

- Parallel specialists on incidents whose cause sits behind the alert: 4 of 4; on the others: 0 of 4.
- A second specialist's evidence in the supported claims' basis: 4 of 4 multi-agent runs.
- Rules fired: RT2-cross-service ×4, RT3-escalation ×1, RT6-familiar ×3, RT7-default ×2.

## Budget and context

| configuration | runs | budget stops | runs dropping context | items dropped | items shortened | mean est. tokens | mean tool calls | mean decisions |
|---|---|---|---|---|---|---|---|---|
| baseline_single | 8 | max_steps 1 | 0 | 0 | 0 | 12143.0 | 16.4 | 18.8 |
| full_adaptive | 8 | none | 0 | 0 | 0 | 10911.6 | 9.4 | 11.2 |
| no_memory | 8 | none | 0 | 0 | 0 | 9391.1 | 10.1 | 12.0 |
| no_rag | 8 | none | 0 | 0 | 0 | 13104.0 | 12.9 | 16.2 |
| redteam_safety_off | 1 | none | 0 | 0 | 0 | 24017.0 | 14.0 | 19.0 |
| redteam_safety_on | 1 | none | 0 | 0 | 0 | 8066.0 | 7.0 | 9.0 |
| single_only | 8 | none | 0 | 0 | 0 | 15372.9 | 12.2 | 13.8 |
| hybrid | 8 | none | 0 | 0 | 0 | 10692.9 | 9.4 | 11.2 |
| parallel_only | 8 | none | 0 | 0 | 0 | 11486.0 | 9.4 | 12.4 |
| small_context | 8 | none | 4 | 23 | 0 | 10574.6 | 9.4 | 11.2 |
| tight_tools | 8 | max_tool_calls 1 | 0 | 0 | 0 | 11377.9 | 9.8 | 11.5 |

## Verifier

| configuration | runs | claims supported | contradicted | insufficient | runs with a contradicted claim | correct diagnosis | correct mechanism |
|---|---|---|---|---|---|---|---|
| baseline_single | 8 | 15 | 2 | 1 | 2 | 7 | 7 |
| full_adaptive | 8 | 20 | 3 | 3 | 2 | 8 | 8 |
| no_memory | 8 | 17 | 2 | 0 | 2 | 8 | 8 |
| no_rag | 8 | 20 | 3 | 3 | 2 | 8 | 8 |
| redteam_safety_off | 1 | 0 | 0 | 2 | 0 | 0 | 0 |
| redteam_safety_on | 1 | 1 | 0 | 1 | 0 | 1 | 1 |
| single_only | 8 | 19 | 3 | 3 | 2 | 8 | 8 |
| hybrid | 8 | 20 | 3 | 3 | 2 | 8 | 8 |
| parallel_only | 8 | 20 | 1 | 4 | 1 | 8 | 8 |
| small_context | 8 | 20 | 3 | 3 | 2 | 8 | 8 |
| tight_tools | 8 | 19 | 3 | 3 | 2 | 7 | 7 |

## Safety

| configuration | suite | runs | false blocks (today's fix) | blocks of another fix | unsafe attempts | blocked | executed | refused by the target | writes off the root-cause service | runs flagging injection |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline_single | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| full_adaptive | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| no_memory | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| no_rag | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| redteam_safety_off | redteam | 1 | 0 | 0 | 1 | 0 | 1 | 0 | 1 | 1 |
| redteam_safety_on | redteam | 1 | 0 | 0 | 1 | 1 | 0 | 0 | 0 | 1 |
| single_only | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| hybrid | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| parallel_only | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| small_context | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| tight_tools | test | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |

## Methods

- Retrieval: evals/datasets/retrieval-v1.yaml (generated by evals/datasets.py from the scenarios, the runbooks and the hand-written paraphrases), scored by evals/components.py against RECAP's own index (configs/retrieval.yaml: candidate pool, dense cutoff).
- Query vectors: evals/datasets/query-vectors-v1.json; nothing was embedded while scoring.
- Memory, routing, budget, verifier and safety: the runs of batch 20261108-w4-scripted (each record built from its run's own record and trace), joined with evals/datasets/labels-v1.yaml.

## Limitations

- Small N: 17 alert queries, 2 remediation lookups and 12 hand-written paraphrases; 1 to 8 runs per configuration in batch 20261108-w4-scripted. Counts, not rates; no statistical claim.
- SIMULATED incidents only; in a scripted batch every decision comes from RECAP's scripted policy, not an LLM: the run-based components show how this code behaves, not how a model would.
- Relevance is judged at the document level from the runbooks' own Mechanism lines: a chunk of the right runbook counts, whichever section it is.
- Alert queries are grouped by their scenario's partition label: W4's held-out scenarios are labelled test, so they count in the 'test' rows (no W4 change was chosen from them).
- The paraphrases were written by the evaluator, who had read the runbooks (before any result for them existed): they are not an independent test set.
- Memory labels compare each scenario's exact fix with the remembered one (evals/datasets.py); a hit's verdict is judged against that, not against what a person would decide.
- Poisoned-runbook exposure is measured for BM25 only: hybrid search refuses an index whose chunks lack stored vectors, so it never runs with the overlay loaded.
