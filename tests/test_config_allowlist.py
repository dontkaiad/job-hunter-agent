"""Tests for the ALLOWED_USER_IDS allowlist parsing and fail-closed fallback.

These exercise pure config logic only (no env mutation): load_config accepts an
explicit mapping, so we never touch os.environ or the real process env.
"""

from __future__ import annotations

from job_hunter.config import _split_int_set, load_config


# --- Pure parser -----------------------------------------------------------


def test_split_int_set_parses_and_ignores_blanks():
    assert _split_int_set("1, 2 ,3") == {1, 2, 3}
    # Blanks / whitespace-only tokens ignored.
    assert _split_int_set(" , ,  ") == set()
    assert _split_int_set("") == set()
    # Duplicates collapse; non-integers dropped.
    assert _split_int_set("5,5, x ,7") == {5, 7}


# --- load_config: explicit allowlist wins ----------------------------------


def test_explicit_allowed_user_ids_wins_over_notify():
    cfg = load_config({"ALLOWED_USER_IDS": "10,20", "NOTIFY_CHAT_ID": "999"})
    assert cfg.allowed_user_ids == {10, 20}
    # notify is NOT auto-added when an explicit list is given.
    assert 999 not in cfg.allowed_user_ids


# --- Fallback: unset ALLOWED_USER_IDS -> {NOTIFY_CHAT_ID} -------------------


def test_fallback_to_notify_chat_id_when_allowlist_unset():
    cfg = load_config({"NOTIFY_CHAT_ID": "555"})
    assert cfg.allowed_user_ids == {555}


def test_blank_allowlist_falls_back_to_notify():
    cfg = load_config({"ALLOWED_USER_IDS": "  , ", "NOTIFY_CHAT_ID": "555"})
    assert cfg.allowed_user_ids == {555}


# --- Fail closed: both unset -> empty set (bot ignores everyone) -----------


def test_fail_closed_when_both_unset():
    cfg = load_config({})
    assert cfg.allowed_user_ids == set()


def test_fail_closed_when_both_blank():
    cfg = load_config({"ALLOWED_USER_IDS": "", "NOTIFY_CHAT_ID": ""})
    assert cfg.allowed_user_ids == set()
