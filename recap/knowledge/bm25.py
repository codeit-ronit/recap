"""BM25 keyword search, implemented from scratch (no search library).

This is the first stage of RECAP's static RAG: `KnowledgeRetriever` indexes every
runbook chunk here and asks for the chunks that best match a query. BM25 ("Best
Matching 25", the classic Okapi ranking function) is the default ranking of search
engines such as Lucene and Elasticsearch. It needs no model, no network and no GPU,
and every score can be explained by hand.

For each query term that appears in a document, BM25 adds:

    IDF(term) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_length / avg_doc_length))

In plain words:
  * Rare terms weigh more (IDF). "oomkilled" says far more than "service".
  * Repeating a term helps less and less (k1 saturation). Five mentions are better
    than one, but nowhere near five times better.
  * Long documents are discounted (b length normalization), so a long page does not
    win just because it contains more words.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence

# A small English stopword list: words so common they say nothing about the topic.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can do does for from has have how if in into is
    it its no not of on or so such than that the their then there these they this to
    was were what when where which while who will with within without you your
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase words for indexing; 'execute_rollback' also yields 'execute' and 'rollback'."""
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        word = raw.strip("_")
        # Tool and mechanism names (e.g. bad_deployment) stay whole so an exact name
        # matches strongly, and their parts are added so plain words match too.
        parts = [word, *word.split("_")] if "_" in word else [word]
        tokens.extend(part for part in parts if len(part) > 1 and part not in STOPWORDS)
    return tokens


class BM25Index:
    """An in-memory Okapi BM25 index over (doc_id, text) pairs."""

    def __init__(
        self, documents: Sequence[tuple[str, str]], k1: float = 1.5, b: float = 0.75
    ) -> None:
        self.k1 = k1  # how quickly repeated terms stop adding score
        self.b = b  # how strongly long documents are discounted (0 = not at all)
        self._ids = [doc_id for doc_id, _ in documents]
        if len(set(self._ids)) != len(self._ids):
            raise ValueError("BM25Index document ids must be unique")

        tokenized = [tokenize(text) for _, text in documents]
        self._lengths = [len(tokens) for tokens in tokenized]
        self._avg_length = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        # Inverted index: term -> {document position: how often the term appears there}.
        # Scoring only visits documents that contain a query term, which is why a
        # document sharing no term with the query never gets a score at all.
        self._postings: dict[str, dict[int, int]] = defaultdict(dict)
        for position, tokens in enumerate(tokenized):
            for term, count in Counter(tokens).items():
                self._postings[term][position] = count

    def idf(self, term: str) -> float:
        """Inverse document frequency: high for rare terms, low (but positive) for common ones."""
        n_docs = len(self._ids)
        df = len(self._postings.get(term, {}))  # number of documents containing the term
        # The "1 +" inside the log (Lucene's variant) keeps IDF positive even for a term
        # found in every document, so a matching term can never lower a score.
        return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        """Top-k (doc_id, score) pairs, best first; ties by id; zero-score docs left out."""
        scores: dict[int, float] = defaultdict(float)
        # Each distinct query term counts once, so repeating a word in the query does not
        # double its weight.
        for term in dict.fromkeys(tokenize(query)):
            postings = self._postings.get(term)
            if not postings:
                continue
            weight = self.idf(term)
            for position, tf in postings.items():
                scores[position] += weight * self._term_score(tf, self._lengths[position])

        ranked = [(self._ids[position], score) for position, score in scores.items() if score > 0]
        ranked.sort(key=lambda item: (-item[1], item[0]))  # sort by id on ties: deterministic
        return ranked[: max(k, 0)]

    def _term_score(self, tf: int, doc_length: int) -> float:
        """Saturated, length-normalized term frequency (the right half of the formula)."""
        # Only called for documents that contain the term, so the average length is > 0.
        length_ratio = doc_length / self._avg_length
        return tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length_ratio))
