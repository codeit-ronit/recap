# ADR 0022: Hybrid retrieval with a local embedding model, and agents that search the runbooks themselves

**In one sentence:** runbook search can fuse BM25 with a dense ranking from `nomic-embed-text`
on this machine, whose requests are recorded and replayed like the model's, and agents may
call a `search_runbooks` tool that RECAP answers from its own index, within a budget, with
any free text a model wrote kept out of the trusted part of every prompt.

## Status

Accepted (M3, 2026-10-21). Owner decision: Ollama-hosted embeddings first, behind the
pluggable `Embedder` interface. Extends [ADR 0016](0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md)
(prompts-v4.4) and [ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md)
(embedding exchanges are recorded too).

## Context

- Retrieval was BM25 only, run once at the start and once before proposing. A query in other
  words than the runbook's ("heap exhaustion" for "out of memory") found nothing.
- The optional hybrid path needed `sentence-transformers` and a model download, embedded
  every chunk at each start, and had no way to replay a run.
- Agents could not look anything up: what they read was what the controller had fetched.

## Decision

1. **A local embedder** ([recap/knowledge/ollama_embedder.py](../../recap/knowledge/ollama_embedder.py)):
   `nomic-embed-text` (137M parameters, F16, 274 MB, installed by the owner) through Ollama's
   `/api/embed` on loopback, with the chat provider's rules: no proxy, no cloud model, and
   no download by RECAP. It speaks through the same `ModelTransport` as the model, so a
   run's embedding requests land in the run's recording (trimmed to vectors, model and
   token count) and a replay answers them offline. Task prefixes: `search_document: ` for
   chunks, `search_query: ` for queries. The `Embedder` interface (async, query and
   document sides) keeps the model replaceable.
2. **Stored chunk vectors** ([recap/knowledge/vectors.py](../../recap/knowledge/vectors.py)):
   built once by `scripts/build_vector_index.py` (42 chunks, 173 KB, base64 float32) and
   committed, keyed by a hash of exactly what was embedded. CI and replays need no model;
   an edited runbook section has no vector, `hybrid_ready` turns False, and a run that asks
   for hybrid fails with a pointer to the script instead of using stale vectors. Vectors
   made by another model, with other dimensions, another document prefix or other weights
   are refused the same way (`KnowledgeRetriever.vector_mismatch`, checked when the run's
   embedder is made and again after it embeds a query, once its server has reported the
   weights); `scripts/build_vector_index.py --check` compares the same things. (Added in the
   M3 review: the hash key does not name the model, so a run searched with such vectors,
   and `--check` compared only the model's name.)
3. **Hybrid search** ([recap/knowledge/retriever.py](../../recap/knowledge/retriever.py)):
   synchronous and pure; the caller passes the query's vector. Dense candidates under a
   cosine of 0.55 are dropped, then reciprocal rank fusion (k = 60) and the lexical
   reranker. Each result records its signals (`bm25_rank`, `dense_rank`, `cosine`, `rrf`).
4. **BM25 stays the default** (`configs/retrieval.yaml`); a run picks
   `RunConfig.retrieval = "hybrid"` (or the owner sets `RECAP_ENABLE_EMBEDDINGS=1`). The
   run record's `retrieval` says which mode ran, the embedder, its weights file, and how
   many embedding requests and tokens it used. Each retrieval event's `retriever` names
   what that search ranked with (`retriever_for`: hybrid only with a query vector and a
   vector for every chunk); `GET /system` reports what a run that does not choose would use
   (`retriever`) and whether hybrid could run (`hybrid_ready`). (Corrected in the M3
   review: both used to read the index's name, which said hybrid whenever the vector file
   was complete.) The V1 evaluation pins BM25 over the shipped runbooks whatever
   `RECAP_ENABLE_EMBEDDINGS` and `RECAP_KNOWLEDGE_OVERLAYS` say, and its report records
   `retrieval_mode` and `knowledge_overlays`.
5. **Agent-initiated search**: `search_runbooks(query, k)` is a read tool with
   `source: local` in `configs/policies.yaml`. It takes the gateway's read path (policy,
   argument checks, timeout, redaction, bounds, injection flags, audit), but a handler in
   the controller answers it, it does not use the environment's tool-call budget (it has
   its own: 6 searches per run, 3 sections each), and its receipt is DOC evidence, which
   the verifier never counts. The sections it found join only the searching agent's
   context (so parallel views stay independent) and become that agent's citations. A
   server offering a tool of that name is reported and never called, and only a local
   tool's result may carry documents.
6. **Free text stays data.** A search query is the first tool argument a model writes
   freely. Source refs (shown as trusted "already observed" text) replace with `#` and 8
   hex characters of its hash every value of an argument the registry leaves free (a
   `string`, such as the query), whatever it looks like, and any other value that is not a
   plain identifier. An earlier call with such an argument is shown inside
   `<untrusted_data>`. Refs of checked arguments (service, enum, version, number) are
   unchanged. An identical search is dropped as a repeat. (Corrected in the M3 review,
   prompts-v4.6: a one-word query such as `SYSTEM:approved` has the shape of a plain value,
   so it used to be shown as trusted text.)

## Consequences

### Good

- Questions in other words find the runbook they mean (measured below), and agents can
  look things up mid-investigation with every section cited.
- A hybrid run replays offline: the e2e test records one and replays it with the same
  retrieved sections and decisions.
- The scripted policy never calls the tool, and its 45 baseline runs end exactly as before.

### Bad

- Hybrid is not better everywhere. Measured on 2026-10-21: on three paraphrased queries it
  ranks the right runbook first where BM25 did not; for "which team must approve a risky
  change" the dense ranking's best chunk drops out of the fused top 3 (RRF favours chunks
  both rankings found); on the scenarios' keyword-rich alert queries the diagnosed
  mechanism's runbook reached the top 4 for 9 of 15 scenarios with BM25 and 7 with hybrid.
  Hence BM25 by default. These are small samples, not a benchmark.
- The 0.55 cutoff has a small margin on this corpus (unrelated queries reached 0.536).
- Embedding a query adds a local request per search (fast once the model is loaded; the
  first load took about 20 s on this machine).
- prompts-v4.4 changed every request fingerprint again; the CI fixture was re-recorded
  (local qwen3:8b, 6 requests, $0). That model was offered `search_runbooks` and did not
  use it.
- `scripts/build_vector_index.py --check` now asks the local Ollama server for metadata
  (nothing is embedded) to compare the weights; without a server it compares everything
  else and says the weights were not compared.
- The evaluation ignores `RECAP_ENABLE_EMBEDDINGS` and `RECAP_KNOWLEDGE_OVERLAYS` without a
  console note; its report records what ran.

## Alternatives considered

- **Keep sentence-transformers.** A Python dependency of several hundred MB and a model
  download, against the project's no-large-downloads rule; Ollama already runs locally.
- **Tune the fusion (weights, k) until the seven queries look good.** That fits the test
  queries, not retrieval; the limitation is documented and pinned instead.
- **Embed every chunk at startup.** Slow, needs the model in CI, and makes a replay depend
  on a live model.
- **Let the MCP server answer searches.** The environment would then control what agents
  read as reference knowledge; RECAP's own index is the trusted source.
