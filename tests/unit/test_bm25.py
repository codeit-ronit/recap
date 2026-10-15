"""BM25 from scratch: tokenization rules and the scoring math, checked by hand."""

from __future__ import annotations

import math

import pytest

from recap.knowledge.bm25 import BM25Index, tokenize


def test_tokenize_lowercases_and_drops_stopwords_and_one_letter_tokens() -> None:
    assert tokenize("The pod was OOMKilled, p95 > 1.5s!") == ["pod", "oomkilled", "p95", "5s"]


def test_tokenize_keeps_tool_names_whole_and_adds_their_parts() -> None:
    assert tokenize("Recommended action: execute_rollback") == [
        "recommended",
        "action",
        "execute_rollback",
        "execute",
        "rollback",
    ]


def test_scores_match_a_hand_computed_example() -> None:
    # Three documents with 2, 3 and 4 tokens ("the" is a stopword): average length = 3.
    index = BM25Index(
        [
            ("d1", "rollback the release"),
            ("d2", "restart crashed pods"),
            ("d3", "rollback rollback deployment history"),
        ]
    )
    # "rollback" is in 2 of 3 documents: IDF = ln(1 + (3 - 2 + 0.5) / (2 + 0.5)) = ln(1.6).
    idf = math.log(1.6)
    # d1: tf = 1, length 2 -> 1 * 2.5 / (1 + 1.5 * (0.25 + 0.75 * 2 / 3)) = 2.5 / 2.125
    # d3: tf = 2, length 4 -> 2 * 2.5 / (2 + 1.5 * (0.25 + 0.75 * 4 / 3)) = 5 / 3.875
    results = index.search("rollback", k=10)

    assert results == [
        ("d3", pytest.approx(idf * 5 / 3.875)),
        ("d1", pytest.approx(idf * 2.5 / 2.125)),
    ]
    assert results[0][1] == pytest.approx(0.6064563, abs=1e-7)
    assert results[1][1] == pytest.approx(0.5529454, abs=1e-7)
    assert index.idf("rollback") == pytest.approx(idf)


def test_rare_terms_weigh_more_and_idf_stays_positive() -> None:
    index = BM25Index([("a", "service memory"), ("b", "service latency"), ("c", "service errors")])

    assert index.idf("memory") > index.idf("service")
    # A term found in every document still gets a small positive weight (the "1 +" variant).
    assert index.idf("service") == pytest.approx(math.log(1 + 0.5 / 3.5))
    assert index.idf("service") > 0


def test_repeating_a_term_has_diminishing_returns() -> None:
    # Same length (8 tokens) for every document, so only the term frequency differs.
    filler = ["w1", "w2", "w3", "w4", "w5", "w6", "w7"]
    docs = [(f"tf{tf}", " ".join(["oom"] * tf + filler[: 8 - tf])) for tf in (1, 2, 4, 8)]
    index = BM25Index(docs)
    scores = dict(index.search("oom", k=10))

    assert scores["tf1"] < scores["tf2"] < scores["tf4"] < scores["tf8"]
    assert scores["tf2"] < 2 * scores["tf1"]  # twice the mentions, less than twice the score
    # k1 caps the contribution of one term at IDF * (k1 + 1), however often it repeats.
    assert scores["tf8"] < index.idf("oom") * (index.k1 + 1)


def test_longer_documents_are_discounted_unless_b_is_zero() -> None:
    docs = [("short", "oom killed"), ("long", "oom w1 w2 w3 w4 w5 w6 w7 w8 w9")]

    normalized = dict(BM25Index(docs).search("oom", k=10))
    assert normalized["short"] > normalized["long"]

    unnormalized = dict(BM25Index(docs, b=0.0).search("oom", k=10))
    assert unnormalized["short"] == pytest.approx(unnormalized["long"])


def test_zero_score_documents_are_left_out_and_ties_break_by_id() -> None:
    index = BM25Index([("b", "restart pods"), ("a", "restart pods"), ("c", "rollback release")])
    results = index.search("restart", k=10)

    assert [doc_id for doc_id, _ in results] == ["a", "b"]  # "c" shares no word with the query
    assert results[0][1] == results[1][1]  # identical text, identical score, so id decides


def test_k_limits_results_and_empty_inputs_return_nothing() -> None:
    index = BM25Index([("a", "rollback"), ("b", "rollback release"), ("c", "rollback history")])

    assert len(index.search("rollback", k=2)) == 2
    assert index.search("rollback", k=0) == []
    assert index.search("", k=5) == []
    assert index.search("the and of", k=5) == []  # only stopwords
    assert BM25Index([]).search("rollback", k=5) == []


def test_duplicate_document_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        BM25Index([("same", "one"), ("same", "two")])
