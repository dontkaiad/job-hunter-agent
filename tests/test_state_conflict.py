"""Optimistic lock on state moves: the harvest runs on a worker thread, a human may act on the
same vacancy at the same moment. The earlier move wins; the later one is dropped, never forced."""
from __future__ import annotations

import pytest

from job_hunter import pipeline, store
from job_hunter.pipeline import Deps
from job_hunter.states import DECISION_SKIP, SKIPPED, SCORED, SURFACED, REJECTED

POST = ("Remote Senior Python engineer building LLM RAG agents with Claude API and FastAPI. "
        "Salary 250000-350000 RUB. Contact @hr_acme")


def _item(conn, state):
    i = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    conn.execute("UPDATE work_items SET state=%s WHERE id=%s", (state, i))
    conn.commit()
    return i


def _other(pg_dsn):
    c = store.connect(pg_dsn)
    return c


def test_update_state_conflicts_when_the_row_moved_meanwhile(conn, pg_dsn):
    i = _item(conn, "surfaced")
    base = conn.execute("SELECT count(*) n FROM state_transitions WHERE item_id=%s", (i,)).fetchone()["n"]
    human = _other(pg_dsn)
    store.update_state(human, i, "skipped", from_state="surfaced", kind="hitl", actor="human", channel="dashboard")
    with pytest.raises(store.StateConflict) as e:
        store.update_state(conn, i, "rejected", from_state="surfaced", kind="deterministic", actor="system")
    assert (e.value.expected, e.value.actual) == ("surfaced", "skipped")
    assert store.get_item(conn, i).state == "skipped"
    n = conn.execute("SELECT count(*) n FROM state_transitions WHERE item_id=%s", (i,)).fetchone()["n"]
    assert n == base + 1, "only the human move left an audit row; the dropped one wrote none"
    human.close()


def test_advance_drops_the_later_move_instead_of_raising(conn, pg_dsn, fake_llm, fake_fx):
    i = _item(conn, "surfaced")
    stale = store.get_item(conn, i)                     # the harvest / a button read it as 'surfaced'
    human = _other(pg_dsn)
    store.update_state(human, i, "skipped", from_state="surfaced", kind="hitl", actor="human", channel="bot")
    res = pipeline.advance(conn, stale, decision=DECISION_SKIP, deps=Deps(llm_client=fake_llm, fx=fake_fx))
    assert res.status == "noop" and res.extra.get("conflict") is True
    assert store.get_item(conn, i).state == SKIPPED
    human.close()


def test_scoring_in_flight_does_not_overwrite_a_human_skip(conn, pg_dsn, fake_llm, fake_fx, monkeypatch):
    """Harvest scores a 'scored' item; while the model call is out, the operator skips it."""
    deps = Deps(llm_client=fake_llm, fx=fake_fx, use_llm_extract=False)
    fake_llm.set_for("hiring-fit JUDGE", '{"relevance_score": 85, "Обоснование": "good"}')
    i = store.insert_item(conn, raw_text=POST, source_channel="@c", source_message_id="1")
    pipeline.advance_by_id(conn, i, deps=deps)          # discovered -> extracted
    human = _other(pg_dsn)
    real = fake_llm.complete

    def complete_then_human_acts(*a, **k):
        out = real(*a, **k)
        # the human skips the vacancy while the (slow) model call was in flight
        human.execute("UPDATE work_items SET state='skipped' WHERE id=%s", (i,))
        human.commit()
        return out

    monkeypatch.setattr(fake_llm, "complete", complete_then_human_acts)
    res = pipeline.advance_by_id(conn, i, deps=deps)       # extracted -> scored (must not clobber)
    assert res.status == "noop" and res.extra.get("conflict") is True
    assert store.get_item(conn, i).state == SKIPPED
    human.close()
