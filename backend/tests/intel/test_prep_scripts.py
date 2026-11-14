"""Prep scripts: draft checks, file -> Mongo shapes -> KB bank hit, topic chunking, eval scoring.
No network, no LLM; synthetic data only."""

import argparse
import json

import pytest

from app.kb.store import SOURCE_TOPIC, KnowledgeBase
from app.models import Claim
from scripts import build_dossier, build_topics, embed_kb, kb_files, run_eval
from tests.intel.test_sync1 import Encoder

URL = "https://example.invalid/factcheck/tariffs"
TEXT = ("Synthetic fact-check for tests. The speaker said China paid billions in tariffs. "
        "Studies found that U.S. importers paid nearly all of the tariff costs. Rating: Misleading.")
SRC = {"url": URL, "title": "Who paid the tariffs", "publisher": "Synthetic Checker", "published": "2024-09-12",
       "reliability": "factchecker", "fetched": "2026-11-14", "text": TEXT}


def bank(quote="Studies found that U.S. importers paid nearly all of the tariff costs."):
    return build_dossier.Bank.model_validate({
        "claims": [{"text": "I took in billions and billions of dollars from China",
                    "decontextualized": "The United States took in billions of dollars in tariffs from China",
                    "entities": ["China"], "numbers": ["billions"], "topic": "trade_tariffs", "verdict_category": "M",
                    "explanation": "Synthetic Checker (2024): importers paid nearly all tariff costs.",
                    "evidence": [{"quote": quote, "url": URL, "title": "x", "publisher": "y", "published": None,
                                  "reliability": "media"}]}],
        "statements": [{"text": "The speaker said China paid billions in tariffs.", "date": "2024-09-10",
                        "source_url": URL, "topic": "trade_tariffs"},
                       {"text": "A statement that is not in the source.", "date": "2024-01-01", "source_url": URL, "topic": None}]})


def test_build_dossier_rejects_unquoted_drafts():
    claims, statements, rejected = build_dossier.check(bank(), SRC, "trump")
    assert len(claims) == 1 and len(statements) == 1 and rejected == 1
    ev = claims[0]["evidence"][0]
    assert claims[0]["reviewed"] is False
    assert (ev["source_id"], ev["title"], ev["publisher"], ev["reliability"]) == (
        kb_files.source_id(URL), SRC["title"], SRC["publisher"], "factchecker")  # source metadata wins
    claims, _, rejected = build_dossier.check(bank(quote="A quote the model invented."), SRC, "trump")
    assert claims == [] and rejected == 2


def test_reviewed_files_load_as_bank_hits(tmp_path, monkeypatch):
    claims, statements, _ = build_dossier.check(bank(), SRC, "trump")
    d = tmp_path / "dossiers" / "trump"
    d.mkdir(parents=True)
    (d / "digest.json").write_text(json.dumps({"speaker_id": "trump", "name": "Synthetic T"}))
    unreviewed = json.loads(json.dumps(claims[0]))
    unreviewed["claim"]["text"] = "Another claim"
    kb_files.write_jsonl(d / "claims.jsonl", [claims[0] | {"reviewed": True}, unreviewed])
    kb_files.write_jsonl(d / "statements.jsonl", [statements[0] | {"reviewed": True}])
    sources = tmp_path / "sources" / "sources.jsonl"
    kb_files.write_jsonl(sources, [build_dossier.source_meta(SRC, bank()) | {"text": TEXT}])
    topic = tmp_path / "topics" / "trade_tariffs"
    kb_files.write_jsonl(topic / "passages.jsonl", [
        {"reviewed": True, "passage_id": "t:1", "text": "Synthetic. Tariff revenue was 77 billion dollars in 2019.", "topic": "trade_tariffs",
         "source": {"source_id": "s#0", "title": "t", "url": "https://example.invalid/t", "publisher": "p",
                    "reliability": "primary", "quote": "q", "published": None, "retrieved_at": "2026-11-14"}},
        {"reviewed": False, "passage_id": "t:2", "text": "Unreviewed.", "topic": "trade_tariffs",
         "source": {"source_id": "s#1", "title": "t", "url": "https://example.invalid/t", "publisher": "p",
                    "reliability": "primary", "quote": "q", "published": None, "retrieved_at": "2026-11-14"}}])
    for name, path in {"DOSSIERS": tmp_path / "dossiers", "TOPICS": tmp_path / "topics", "SOURCES": sources}.items():
        monkeypatch.setattr(embed_kb, name, path)

    out, errors = embed_kb.collect()
    assert errors == [] and len(out["claims"]) == 1 and len(out["statements"]) == 1
    assert [p.topic for p in out["passages"]] == [SOURCE_TOPIC, "trade_tariffs"]

    kb = KnowledgeBase(encoder=Encoder(), claims=out["claims"], passages=out["passages"],
                       statements=out["statements"], digests=out["speaker_digests"])
    kb._prepare()
    hit = kb.match_bank("trump", Claim(**claims[0]["claim"]))
    assert hit is not None and hit.verdict_category == "M"
    # Full source texts back provenance only; retrieval returns topic passages.
    assert [p.passage_id for p in kb.search_passages("tariffs", None, 5)] == ["t:1"]
    assert kb.topic_for("tariffs") == "trade_tariffs"

    # Edited quote no longer verbatim in the source: reported and skipped.
    broken = claims[0] | {"reviewed": True, "evidence": [claims[0]["evidence"][0] | {"quote": "Edited by hand."}]}
    kb_files.write_jsonl(d / "claims.jsonl", [broken])
    out, errors = embed_kb.collect()
    assert out["claims"] == [] and "not verbatim" in errors[0]


def test_invalid_topic_is_rejected(capsys, tmp_path):
    parser = build_topics.argument_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["../x"])
    assert caught.value.code == 2
    assert "../x" in capsys.readouterr().err

    with pytest.raises(SystemExit, match="resolves outside"):
        build_topics.passages_path("../x")

    ok = parser.parse_args(["economy_inflation", "--url", "https://example.invalid/cpi"])
    assert ok.topic == "economy_inflation"
    out = build_topics.passages_path(ok.topic)
    root = build_topics.TOPICS.resolve()
    assert out == (root / "economy_inflation" / "passages.jsonl").resolve()
    assert out.is_relative_to(root)

    missing = tmp_path / "missing.txt"
    with pytest.raises(argparse.ArgumentTypeError, match="not an existing regular file"):
        build_topics.urls_file(str(missing))
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.invalid/a\n")
    assert build_topics.urls_file(str(tmp_path / ".." / tmp_path.name / "urls.txt")) == urls.resolve()


def test_topic_chunking_keeps_short_passages_with_figures():
    text = ("Menu Home About. " * 3 + "Consumer prices rose 2.9 percent over the year. The index for food increased "
            "2.1 percent. Energy fell 4.0 percent over the same period. Officials will publish again next month, "
            "the agency said in a statement on its website today. ") * 2
    found = build_topics.passages_from(text, keep_all=False)
    assert found and all(build_topics.MIN_CHARS <= len(p) <= build_topics.MAX_CHARS for p in found)
    assert all(any(ch.isdigit() for ch in p) for p in found)


def test_eval_scoring():
    per = run_eval.score([("F", {"F"}), ("F", set()), (None, {"F"}), ("L", {"L", "U"}), (None, set())])
    assert per["F"] == {"precision": 0.5, "recall": 0.5, "n": 2, "tp": 1, "fp": 1, "fn": 1}
    assert per["L"]["recall"] == 1.0 and per["U"] == {"precision": 0.0, "recall": None, "n": 0, "tp": 0, "fp": 1, "fn": 0}
    assert "V" not in per
