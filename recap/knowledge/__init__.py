"""Static RAG over runbooks: the "documents" half of RECAP's context.

The controller searches these Markdown runbooks during the RETRIEVING stage, and the
cited chunks enter the context pack as UNTRUSTED data next to tool evidence and memory.

    ingest.py      Markdown files -> DocChunk (stable IDs, checksum, provenance)
    bm25.py        BM25 keyword ranking, implemented from scratch
    embeddings.py  optional dense vectors + reciprocal rank fusion (feature flag)
    retriever.py   KnowledgeRetriever (two-stage search) and LexicalReranker
    hints.py       parses "Mechanism: ..." / "Recommended action: ..." lines

Episodic memory of past incidents is a separate store (recap/memory). See README.md here.
"""
