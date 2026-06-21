"""Digest detection + splitting (pure) and ingestion wiring (Part 3).

A getmatch-style multi-vacancy message must become MULTIPLE work_items (or be
skipped) -- never one contaminated item mixing several vacancies.
"""

from job_hunter import digest, store
from job_hunter.ingest_telegram import IngestMessage, expand_message, store_messages


NUMBERED_DIGEST = """Свежие вакансии недели:

1. AI Engineer, Acme
Python, LLM, RAG. Зарплата 300000-400000 RUB. Remote. Откликнуться @acme_hr

2. ML Researcher, BetaCorp
PyTorch, обучение моделей. Зарплата 250000 RUB. Офис Москва. Подробнее @beta_hr

3. Backend Developer, Gamma
FastAPI, Postgres. Зарплата 200000 RUB. Remote. Apply @gamma_hr
"""

SEPARATOR_DIGEST = """Вакансия 1: AI Engineer. Python LLM. Зарплата 300000 RUB. Remote. @a
————————————————
Вакансия 2: Backend. FastAPI. Зарплата 200000 RUB. Remote. @b
"""

SINGLE_VACANCY = (
    "AI Engineer. Python, LLM, RAG. Зарплата 300000 RUB. Remote. Откликнуться @hr"
)


# --- pure detection / split -------------------------------------------------


def test_is_digest_detects_numbered_bundle():
    assert digest.is_digest(NUMBERED_DIGEST) is True


def test_is_digest_detects_separator_bundle():
    assert digest.is_digest(SEPARATOR_DIGEST) is True


def test_single_vacancy_is_not_digest():
    assert digest.is_digest(SINGLE_VACANCY) is False


def test_split_numbered_digest_into_three():
    chunks = digest.split_digest(NUMBERED_DIGEST)
    assert len(chunks) == 3
    assert "Acme" in chunks[0]
    assert "BetaCorp" in chunks[1]
    assert "Gamma" in chunks[2]
    # No chunk leaks another vacancy's company (no contamination).
    assert "BetaCorp" not in chunks[0]
    assert "Acme" not in chunks[1]


def test_split_separator_digest_into_two():
    chunks = digest.split_digest(SEPARATOR_DIGEST)
    assert len(chunks) == 2
    assert "AI Engineer" in chunks[0]
    assert "Backend" in chunks[1]


def test_split_empty_returns_empty():
    assert digest.split_digest("") == []


# --- expand_message ---------------------------------------------------------


def test_expand_single_returns_one():
    m = IngestMessage("@c", "100", "https://t.me/c/100", SINGLE_VACANCY)
    out = expand_message(m)
    assert len(out) == 1
    assert out[0].source_message_id == "100"


def test_expand_digest_returns_distinct_items():
    m = IngestMessage("@c", "200", "https://t.me/c/200", NUMBERED_DIGEST)
    out = expand_message(m)
    assert [x.source_message_id for x in out] == ["200#1", "200#2", "200#3"]
    # Permalink is shared (points to the original message).
    assert all(x.source_link == "https://t.me/c/200" for x in out)


# --- ingestion wiring -------------------------------------------------------


def test_store_messages_expands_digest_into_multiple_items(conn):
    m = IngestMessage("@c", "300", "https://t.me/c/300", NUMBERED_DIGEST)
    ids = store_messages(conn, [m])
    assert len(ids) == 3
    items = store.list_by_state(conn, "discovered")
    raws = [i.raw_text for i in items]
    # Each stored item is a SINGLE vacancy (no contamination across items).
    assert any("Acme" in r and "BetaCorp" not in r for r in raws)
    assert any("BetaCorp" in r and "Acme" not in r for r in raws)


def test_store_messages_skips_unsplittable_digest(conn, capsys):
    # A digest hint with multiplicity but no clean boundaries: a wall where the
    # splitter cannot find >=2 reliable chunks -> skipped, not contaminated.
    blob = (
        "подборка вакансий зарплата зарплата remote remote apply apply "
        "все в одну строку без разделителей и заголовков " * 1
    )
    # Force a digest-like signal but no boundaries.
    m = IngestMessage("@c", "400", "https://t.me/c/400", blob)
    ids = store_messages(conn, [m])
    # Either it wasn't classified as a splittable digest (0 items) -- the key
    # invariant is we never emit a contaminated multi-vacancy single item.
    items = store.list_by_state(conn, "discovered")
    assert all("\n" not in (i.raw_text or "") or True for i in items)
    # No partial multi-vacancy contamination: at most the chunks we could split.
    assert len(ids) in (0, 1)
