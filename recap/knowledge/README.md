# recap/knowledge: static RAG over runbooks

This package gives RECAP its **document knowledge**: a handful of Markdown runbooks that
are split into citable chunks and searched with BM25, or with BM25 and a local embedding
model together (hybrid). It is separate from episodic memory (`recap/memory`), which stores
what happened in past runs.

```
fixtures/knowledge/*.md
   | load_documents()            ingest.py: one chunk per "## " section, with provenance
   v
DocChunk[]  --->  KnowledgeRetriever.search(query, k, query_vector=None)
                   1. BM25 candidates (bm25.py)
                      [hybrid: + cosine ranking over the stored chunk vectors (vectors.py),
                       cut at min_dense_similarity, fused by RRF (embeddings.py)]
                   2. LexicalReranker            (retriever.py)
   v
RetrievedChunk[] (with signals: bm25_rank, dense_rank, cosine, rrf)
   --->  controller (initial and proposal searches)   --->  every agent's context pack
   --->  search_runbooks tool (an agent asks itself)  --->  THAT agent's context pack
   --->  scripted policy reads "Recommended action:" lines (hints.py)
   --->  everything stays UNTRUSTED text: the safety gateway decides what may run
```

| File | What it does |
|---|---|
| `ingest.py` | Markdown files to `DocChunk`s: stable IDs, repo-relative path, title, heading, checksum, provenance |
| `bm25.py` | Tokenizer and an Okapi BM25 index written from scratch (no search library) |
| `retriever.py` | `KnowledgeRetriever` (the `RetrievalIndex`) and `LexicalReranker` (the `Reranker`) |
| `embeddings.py` | The `Embedder` interface (async; query and document sides), cosine similarity, reciprocal rank fusion, an optional sentence-transformers embedder |
| `ollama_embedder.py` | `OllamaEmbedder`: `nomic-embed-text` on loopback through the recordable model transport |
| `vectors.py` | `VectorIndex`: chunk vectors stored once (base64 float32), keyed by a hash of what was embedded |
| `hints.py` | Parses the `Mechanism:` / `Recommended action:` lines in runbooks |

## Usage

```python
from recap.core.config import load_settings
from recap.knowledge.retriever import KnowledgeRetriever

settings = load_settings()
retriever = KnowledgeRetriever.from_directory(
    settings.paths.knowledge_dir,
    root=settings.paths.root,  # store repo-relative paths
    # The stored chunk vectors make hybrid search possible (retriever.hybrid_ready).
    vectors_path=settings.paths.root / settings.retrieval.embedder.vectors,
    min_dense_similarity=settings.retrieval.min_dense_similarity,
)
for hit in retriever.search("bad_deployment remediation", k=3):  # no query vector: BM25
    print(hit.rank, hit.chunk.chunk_id, hit.retriever, hit.reranker)
# 1 runbook-bad-deployment#remediation-1 bm25 lexical-heading-boost
# 2 runbook-bad-deployment#overview-1 bm25 lexical-heading-boost
# 3 runbook-bad-deployment#diagnosis-1 bm25 lexical-heading-boost
```

In a run, the controller embeds the query with the run's `OllamaEmbedder` (its requests
are recorded with the model's and replay offline) and passes the vector in; see
[docs/flow.md](../../docs/flow.md), stage 3.

## From Markdown to chunks

* The first `# ` line is the document title; each `## ` section becomes one chunk. Text
  between the title and the first section becomes an `Overview` chunk.
* Chunk IDs are `<file stem>#<heading slug>-<n>`, for example
  `runbook-bad-deployment#remediation-1`. They are stable, so a citation stays valid
  across runs.
* A section longer than 180 words is split into windows of 180 words that overlap by 30.
  Windows are slices of the original text, so line breaks (and therefore hint lines) survive.
* Every chunk records `checksum` (first 12 hex characters of the file's SHA-256, which
  changes when the file is edited) and `provenance`
  (`source`, `kind: synthetic runbook authored for RECAP V1`, `license`).

## How BM25 scores a chunk

For every query word found in the chunk, BM25 adds
`IDF * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average_length))`:

* **Rare words weigh more.** `IDF = ln(1 + (N - df + 0.5) / (df + 0.5))`, where `N` is the
  number of chunks and `df` how many contain the word. "oomkilled" outweighs "service".
* **Repetition saturates** (`k1 = 1.5`). A word that appears five times helps more than
  once, but far less than five times as much.
* **Long chunks are discounted** (`b = 0.75`), so a long section does not win just by
  containing more words.

Worked example (from `tests/unit/test_bm25.py`): three documents of 2, 3 and 4 tokens, and the
query `rollback`, which appears in two of them. `IDF = ln(1.6) = 0.470`. The document with
`rollback` twice scores `0.470 * 5 / 3.875 = 0.606`; the shorter one with it once scores
`0.470 * 2.5 / 2.125 = 0.553`; the third shares no word and is left out.

Tokenizing lowercases text, drops a small stopword list and one-letter tokens, and keeps
names like `execute_rollback` whole while also adding `execute` and `rollback`, so both an
exact tool or mechanism name and plain words match. Ties are broken by chunk ID, so the
same query always returns the same order.

## Reranking and hybrid retrieval

* `LexicalReranker` multiplies each candidate's score by
  `1 + 0.25 * (share of query words found in the chunk's heading or document title)`.
  It is a simple lexical heuristic, **not** a neural reranker. The `Reranker` interface in
  `recap/core/interfaces.py` is where a cross-encoder could plug in later.
* The first stage hands up to 20 candidates (or `k`, if larger) to the reranker, the usual
  retrieve-then-rerank pattern.
* **Hybrid** (`retrieval: hybrid` in a run, or `RECAP_ENABLE_EMBEDDINGS=1`): chunks are also
  ranked by cosine similarity between the query's `nomic-embed-text` vector and the stored
  chunk vectors (`fixtures/knowledge/vectors/nomic-embed-text.json`, built by
  `scripts/build_vector_index.py` with the local model). Dense candidates below
  `min_dense_similarity` (0.55) are dropped, and the two rankings are merged with
  reciprocal rank fusion: each chunk earns `1 / (60 + rank)` per ranking.
* nomic-embed-text expects task prefixes: chunks were embedded as `search_document: ...`,
  queries are embedded as `search_query: ...`.
* Each result's `signals` say why it ranked where it did: `bm25_rank`, `bm25`,
  `dense_rank`, `cosine`, `rrf` (only the rankings that search used).
* A vector is keyed by a hash of exactly what was embedded, so an edited runbook section
  has no vector: `hybrid_ready` turns False, and a run that asks for hybrid fails with
  "run scripts/build_vector_index.py" instead of searching with stale vectors.
  `tests/unit/test_hybrid_retrieval.py` fails while the stored file is stale.

### What hybrid search does better, and what it does not (measured 2026-10-21)

On seven queries with stored real query vectors (`tests/fixtures/retrieval/`):

* paraphrases with few shared words: for "heap exhaustion makes the container restart over
  and over", "errors started right after we shipped a new build" and "a downstream service
  is slow so every caller times out", hybrid puts the runbook the query means first, where
  BM25 put a guide or the wrong runbook first;
* for "which team must approve a risky change" the dense ranking's best chunk (the change
  policy) is missing from hybrid's top 3: RRF favours chunks found by both rankings, and
  weak keyword matches outnumber it. This is pinned by a test, not fixed by tuning;
* unrelated queries: nothing for "how do I bake sourdough bread"; only keyword matches
  for "quarterly sales report for the marketing department" (the cutoff drops every dense
  candidate).

On the 15 scenarios' own alert queries (service plus symptom lines, keyword-rich), the
diagnosed mechanism's runbook was in the top 4 for 9 scenarios with BM25 and 7 with
hybrid. So BM25 stays the default, and hybrid is a per-run choice: it helps most for
questions written in sentences, such as an agent's own `search_runbooks` queries.

## Runbook convention

Remediation sections carry two plain lines that the scripted (non-LLM) policy parses:

```
Mechanism: bad_deployment
Recommended action: execute_rollback
```

| Mechanism | Hint | Where |
|---|---|---|
| `bad_deployment` | `execute_rollback` | `runbook-bad-deployment.md` |
| `resource_pressure` | `restart_service` | `runbook-memory-pressure.md` |
| `dependency_failure` | none on purpose: fix the dependency, not the caller | `guide-cascading-failures.md` |

The docs are generic operational knowledge, not answer keys: tests check that they contain
no release versions or scenario-specific names, and that only `shop-architecture.md`
names services.

## Retrieved text is untrusted

Retrieval ranks by relevance, not by trust. `tests/adversarial/test_poisoned_runbook.py`
adds a poisoned runbook that recommends `delete_namespace` and says "Ignore previous
instructions...". It is retrieved, sometimes above the real runbook, and this package
returns it unchanged. Safety comes from the layers after retrieval:
`recap/safety/injection.py` flags the text as a suspected injection, the scripted policy
ignores hints whose action is not in the trusted remediation catalog
(`configs/policies.yaml`), and the safety gateway always denies destructive tools.

## Limitations

* No stemming: "restart" and "restarts" are different words to BM25.
* Lexical matching misses synonyms ("heap exhaustion" and "out of memory") unless hybrid
  retrieval is turned on, and hybrid's fusion can still bury a chunk only the dense
  ranking finds (see above).
* The 0.55 similarity cutoff was measured on 10 queries over this corpus: relevant chunks
  scored 0.62 to 0.79, unrelated queries at most 0.536. A small margin; a larger corpus
  would need it measured again.
* The knowledge base is small and synthetic: 7 documents and 42 chunks written for
  ShopSim, the simulated shop.
* The BM25 index is rebuilt in memory at startup, and cosine similarity is computed in
  plain Python over all 42 vectors. That is fine at this size; a large corpus would need a
  vector database.
