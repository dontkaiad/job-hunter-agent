"""END-TO-END regression for the netbell ai_rabota/1138 integration bug.

This exercises the REAL extraction path (pipeline._do_extract with the LLM
extractor enabled) and mocks ONLY the LLM transport. The mock simulates Haiku
returning a SPARSE / IMPERFECT extract -- exactly the failure observed live
(company=null, remote=null, seniority=null, contact=null, benefits=[]) -- so
the test proves the deterministic RECONCILE pass fills those fields.

It also locks in the single-card invariant: the netbell post is one vacancy
with several bulleted SECTION headers, NOT a multi-vacancy digest, so it must
NOT be split, must yield exactly ONE work_item, and re-ingest / reprocess must
not create a second row or a second surfaced card.
"""

from __future__ import annotations

import json
import os

import pytest

from job_hunter import digest, extract, pipeline, store
from job_hunter.ingest_telegram import IngestMessage, expand_message, store_messages
from job_hunter.pipeline import Deps
from job_hunter.schema_extract import from_dict
from job_hunter.states import SURFACED

CHANNEL = "ai_rabota"
LINK = "https://t.me/ai_rabota/1138"
MSG_ID = "1138"

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "netbell_ai_rabota_1138.txt"
)


@pytest.fixture
def netbell_text():
    with open(_FIXTURE, "r", encoding="utf-8") as f:
        return f.read()


# A SPARSE Haiku extract: title + stack only, every reconciled field missing.
# This reproduces the live failure the reconcile pass must repair.
_SPARSE_EXTRACT = json.dumps(
    {
        "title": "ML Engineer",
        "company": None,
        "stack": ["python", "llm", "rag"],
        "seniority": None,
        "salary_min": None,
        "salary_max": None,
        "currency": None,
        "remote": None,
        "relocation": None,
        "location": None,
        "contact_type": None,
        "contact": None,
        "benefits": [],
    },
    ensure_ascii=False,
)

_SCORE_OK = '{"relevance_score": 85, "Обоснование": "applied-LLM role, remote, middle, so 85/100"}'


def _arm_sparse(llm):
    """Script the FakeLLM to return a sparse extract + an OK score."""
    llm.set_for("parse ONE job posting", _SPARSE_EXTRACT)  # EXTRACT_SYSTEM
    llm.set_for("hiring-fit JUDGE", _SCORE_OK)             # SCORE_SYSTEM
    return llm


def _live_deps(llm, fx):
    """Deps matching the LIVE wiring: use_llm_extract=True (the real default)."""
    return Deps(llm_client=_arm_sparse(llm), fx=fx, use_llm_extract=True)


# --- extraction path: reconcile fills the sparse Haiku extract ---------------


def test_e2e_reconcile_fills_sparse_llm_extract(conn, netbell_text, fake_llm, fake_fx):
    """The REAL T1 path on the netbell post, with a sparse Haiku mock, must end
    up with remote/company/seniority/contact/benefits populated."""
    item_id = store.insert_item(
        conn,
        raw_text=netbell_text,
        source_channel=CHANNEL,
        source_link=LINK,
        source_message_id=MSG_ID,
    )
    assert item_id is not None

    deps = _live_deps(fake_llm, fake_fx)
    item = store.get_item(conn, item_id)
    res = pipeline.advance(conn, item, deps=deps)  # T1 extract
    assert res.transition == "T1"
    assert res.reason == "extract:llm+reconcile"  # LLM path + reconcile ran

    item = store.get_item(conn, item_id)
    ex = from_dict(json.loads(item.extracted_json))

    assert ex.remote is True                 # #УдаленкаРФ + «Полная удалёнка»
    assert ex.company == "Нетбелл"           # «Компания: Нетбелл»
    assert ex.seniority == "middle"          # #middle
    assert ex.contact == "info@netbell.ru"   # bottom-line contact
    assert ex.benefits                        # «Что мы предлагаем» -> non-empty


def test_reconcile_does_not_clobber_correct_llm_values(netbell_text):
    """Where the LLM is authoritative, reconcile must not overwrite good values.

    PRECEDENCE check: the LLM's seniority (read from body prose) and a non-empty
    benefits list are kept; remote/company/contact are still corrected per the
    documented rules.
    """
    good = from_dict(
        {
            "title": "ML Engineer",
            "source_channel": CHANNEL,
            "company": None,
            "stack": ["python"],
            "seniority": "senior",                 # LLM read this from prose
            "remote": None,
            "contact": None,
            "benefits": ["Кастомный бонус от LLM"],  # LLM-provided list
            "source_link": LINK,
        }
    )
    out = extract.reconcile(good, netbell_text, CHANNEL, LINK)

    # LLM-authoritative fields preserved.
    assert out.seniority == "senior"
    assert out.benefits == ["Кастомный бонус от LLM"]
    # Deterministic fields still corrected.
    assert out.remote is True
    assert out.company == "Нетбелл"
    assert out.contact == "info@netbell.ru"


def test_reconcile_drops_channel_leak_in_contact(netbell_text):
    """If the LLM leaked the source channel into contact, reconcile replaces it
    with the real body contact (and never keeps the channel)."""
    leaked = from_dict(
        {
            "title": "ML Engineer",
            "source_channel": CHANNEL,
            "stack": ["python"],
            "contact": "@ai_rabota",  # the channel itself -> a leak
            "contact_type": "dm",
            "source_link": LINK,
        }
    )
    out = extract.reconcile(leaked, netbell_text, CHANNEL, LINK)
    assert out.contact == "info@netbell.ru"


# --- single-card invariant: not a digest, one item, dedup -------------------


def test_netbell_is_not_a_digest(netbell_text):
    """One vacancy with bulleted section headers is NOT a multi-vacancy digest."""
    assert digest.is_digest(netbell_text) is False
    assert digest.split_digest(netbell_text) == []


def test_netbell_expands_to_exactly_one_item(netbell_text):
    msg = IngestMessage(CHANNEL, MSG_ID, LINK, netbell_text)
    out = expand_message(msg)
    assert len(out) == 1
    assert out[0].source_message_id == MSG_ID  # NOT "1138#1"/"1138#2"


def test_netbell_ingest_yields_one_item_and_reingest_dedups(conn, netbell_text):
    msg = IngestMessage(CHANNEL, MSG_ID, LINK, netbell_text)
    ids = store_messages(conn, [msg])
    assert len(ids) == 1
    # Re-ingest the SAME post: dedup on (channel, message_id) -> no new row.
    ids2 = store_messages(conn, [msg])
    assert ids2 == []
    assert len(store.list_by_state(conn, "discovered")) == 1


def test_netbell_full_pipeline_one_surfaced_card(conn, netbell_text, fake_llm, fake_fx):
    """Drive the netbell post all the way to a gate via the REAL pipeline with a
    sparse Haiku mock, then re-run: exactly ONE surfaced card, no second card."""
    ids = store_messages(conn, [IngestMessage(CHANNEL, MSG_ID, LINK, netbell_text)])
    assert len(ids) == 1
    item_id = ids[0]

    deps = _live_deps(fake_llm, fake_fx)
    pipeline.run_to_gate(conn, item_id, deps=deps)

    surfaced = store.list_by_state(conn, SURFACED)
    assert len(surfaced) == 1
    assert surfaced[0].id == item_id

    # The reconciled fields survived through to the surfaced item.
    ex = from_dict(json.loads(surfaced[0].extracted_json))
    assert ex.remote is True
    assert ex.company == "Нетбелл"
    assert ex.seniority == "middle"
    assert ex.contact == "info@netbell.ru"
    assert ex.benefits

    # Reprocess (idempotency): a surfaced item is past its auto handlers; the
    # only outgoing transitions are HITL gates -> no new surfaced row appears.
    pipeline.run_to_gate(conn, item_id, deps=deps)
    assert len(store.list_by_state(conn, SURFACED)) == 1
