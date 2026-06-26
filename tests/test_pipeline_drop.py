"""Unit tests for the MIN_PERSIST_SCORE drop logic.

Uses mock DB connection (no real Postgres required) and patches store.delete_item.

psycopg (v3) is only installed in Docker / on the VPS; tests are skipped when
it is not available (they run in CI where psycopg is present).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed — skip pipeline tests")

from job_hunter.pipeline import Deps, _do_reject_or_surface  # noqa: E402
from job_hunter.schema_extract import ExtractResult, serialize  # noqa: E402
from job_hunter.store import WorkItem  # noqa: E402
from job_hunter.states import SCORED  # noqa: E402


def _make_item(score: float, item_id: int = 1) -> WorkItem:
    """Build a WorkItem in SCORED state with the given relevance_score."""
    extracted = ExtractResult(
        title="Test Job",
        source_channel="tg_test",
        relevance_score=score,
    )
    blob = json.dumps({**json.loads(serialize(extracted))})
    return WorkItem(
        id=item_id,
        state=SCORED,
        source_channel="tg_test",
        source_link="https://example.com/job/1",
        source_message_id="msg1",
        raw_text="Raw job text",
        extracted_json=blob,
        relevance_score=score,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _deps(min_persist: int) -> Deps:
    return Deps(min_persist_score=min_persist)


def _mock_conn():
    conn = MagicMock()
    conn.execute.return_value = conn
    return conn


# ---------------------------------------------------------------------------
# Drop tests
# ---------------------------------------------------------------------------


def test_score_below_threshold_drops_item():
    """Item with score < min_persist_score is deleted from DB."""
    item = _make_item(score=10.0)
    conn = _mock_conn()
    deps = _deps(min_persist=25)

    with patch("job_hunter.pipeline.store.delete_item") as mock_delete, \
         patch("job_hunter.pipeline.store.update_state") as mock_update:
        result = _do_reject_or_surface(conn, item, deps)

    mock_delete.assert_called_once_with(conn, item.id)
    mock_update.assert_not_called()
    assert result.status == "noop"
    assert "dropped" in (result.reason or "")


def test_score_at_threshold_not_dropped():
    """Item with score == min_persist_score is NOT dropped (goes to normal flow)."""
    item = _make_item(score=25.0)
    conn = _mock_conn()
    deps = _deps(min_persist=25)

    with patch("job_hunter.pipeline.store.delete_item") as mock_delete, \
         patch("job_hunter.pipeline.store.update_state") as mock_update:
        _do_reject_or_surface(conn, item, deps)

    mock_delete.assert_not_called()


def test_score_above_threshold_not_dropped():
    """Item with score well above min_persist goes through normal routing."""
    item = _make_item(score=70.0)
    conn = _mock_conn()
    deps = _deps(min_persist=25)

    with patch("job_hunter.pipeline.store.delete_item") as mock_delete, \
         patch("job_hunter.pipeline.store.update_state"):
        _do_reject_or_surface(conn, item, deps)

    mock_delete.assert_not_called()


def test_zero_threshold_disables_drop():
    """min_persist_score=0 disables the drop (keep everything)."""
    item = _make_item(score=1.0)  # very low score
    conn = _mock_conn()
    deps = _deps(min_persist=0)

    with patch("job_hunter.pipeline.store.delete_item") as mock_delete, \
         patch("job_hunter.pipeline.store.update_state"):
        _do_reject_or_surface(conn, item, deps)

    mock_delete.assert_not_called()


def test_no_deps_does_not_drop():
    """deps=None (test/deterministic-only run) never drops items."""
    item = _make_item(score=5.0)
    conn = _mock_conn()

    with patch("job_hunter.pipeline.store.delete_item") as mock_delete, \
         patch("job_hunter.pipeline.store.update_state"):
        _do_reject_or_surface(conn, item, None)

    mock_delete.assert_not_called()


def test_drop_score_24_threshold_25():
    """Boundary: score=24 with threshold=25 → dropped."""
    item = _make_item(score=24.0)
    conn = _mock_conn()
    deps = _deps(min_persist=25)

    with patch("job_hunter.pipeline.store.delete_item") as mock_delete, \
         patch("job_hunter.pipeline.store.update_state"):
        result = _do_reject_or_surface(conn, item, deps)

    mock_delete.assert_called_once()
    assert result.status == "noop"
