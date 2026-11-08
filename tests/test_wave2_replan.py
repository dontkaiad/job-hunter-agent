"""Re-planning wave 2 against the database: a card that was already labelled survives."""
import types

import pytest

from evals import golden_eval as ge
from job_hunter import config, golden as g, store

SIZES = {"<25": 20, "25-39": 80, "40-49": 20, "50-59": 60, "60-69": 15, "70-79": 20}


def _seed(conn):
    """Work items + a frame, a wave-1 sample (3 cards) and an OLD wave 2 (6 cards, 1 labelled)."""
    frame, ids = [], {}
    for band, n in SIZES.items():
        for _ in range(n):
            iid = store.insert_item(conn, raw_text=f"vacancy {len(frame)}", source_channel="@c",
                                    source_message_id=str(len(frame)))
            frame.append({"item_id": iid, "stratum": "core", "band": band, "band_source": "stored"})
            ids.setdefault(band, []).append(iid)
    def row(item, wave, no, band):
        return conn.execute(
            "INSERT INTO eval_sample (item_id, wave, order_no, stratum, band, band_source, is_control, held_out, created_at) "
            "VALUES (%s,%s,%s,'core',%s,'stored',false,false,'2026-11-08T00:00:00+00:00') RETURNING id",
            (item, wave, no, band)).fetchone()["id"]
    for no, item in enumerate(ids["70-79"][:3], 1):
        row(item, 1, no, "70-79")
    labelled_item = ids["25-39"][0]
    labelled = row(labelled_item, 2, 1, "25-39")
    for no, item in enumerate(ids["40-49"][:5], 2):
        row(item, 2, no, "40-49")                              # old plan cards, unlabelled
    conn.execute("INSERT INTO eval_labels (sample_id, label, chips, labeled_by, labeled_at, updated_at) "
                 "VALUES (%s,'yes','','1','2026-11-08T16:13:00+00:00','2026-11-08T16:13:00+00:00')", (labelled,))
    conn.commit()
    return frame, labelled, labelled_item


@pytest.fixture
def env(conn, monkeypatch):
    frame, labelled, labelled_item = _seed(conn)
    cfg = config.Config(database_url="postgresql://unused")
    monkeypatch.setattr(ge, "db_ctx", lambda: (cfg, conn))
    monkeypatch.setattr(ge, "_frame_with_scores", lambda c, u: (conn, {"excluded": {}}, frame))
    monkeypatch.setattr(store, "connect", lambda dsn: conn)
    return frame, labelled, labelled_item


def test_dry_run_changes_nothing_and_reports_the_plan(conn, env):
    before = conn.execute("select id, order_no, item_id from eval_sample where wave=2 order by id").fetchall()
    out = ge.run_wave2(None, apply=False)
    assert out["applied"] is False and out["kept_already_labelled"] == 1
    assert conn.execute("select id, order_no, item_id from eval_sample where wave=2 order by id").fetchall() == before


def test_apply_replaces_unlabelled_cards_and_keeps_the_labelled_one(conn, env):
    frame, labelled, labelled_item = env
    out = ge.run_wave2(None, apply=True)
    rows = conn.execute("select id, order_no, item_id, band, is_control from eval_sample where wave=2 order by order_no").fetchall()
    assert [r["order_no"] for r in rows] == list(range(1, len(rows) + 1))          # contiguous, UNIQUE intact
    me = [r for r in rows if r["id"] == labelled]
    assert len(me) == 1 and me[0]["item_id"] == labelled_item and me[0]["order_no"] == 1   # same row, same place
    assert conn.execute("select label from eval_labels where sample_id=%s", (labelled,)).fetchone()["label"] == "yes"
    assert conn.execute("select count(*) n from eval_labels").fetchone()["n"] == 1
    plain = [r for r in rows if not r["is_control"]]
    from collections import Counter
    assert Counter(r["band"] for r in plain) == Counter(g.WAVE2_PLAN)             # the kept card counts toward 25-39
    assert sum(r["is_control"] for r in rows) == 5 and len(rows) == 65
    wave1_items = {r["item_id"] for r in conn.execute("select item_id from eval_sample where wave=1").fetchall()}
    assert not (wave1_items & {r["item_id"] for r in rows})                        # nothing from wave 1
    assert sum(1 for r in rows if r["item_id"] == labelled_item and not r["is_control"]) == 1
    assert out["kept_already_labelled"] == 1 and out["replaced_unlabelled_old_wave2_cards"] == 5


def test_a_labelled_control_card_is_refused(conn, env):
    frame, labelled, _ = env
    conn.execute("UPDATE eval_sample SET is_control = true WHERE id = %s", (labelled,)); conn.commit()
    with pytest.raises(SystemExit, match="control"):
        ge.run_wave2(None, apply=True)


def test_wave2_command_never_touches_the_api():
    import inspect
    src = inspect.getsource(ge.run_wave2)
    assert "make_client" not in src and "AnthropicClient" not in src and "gate(" not in src
