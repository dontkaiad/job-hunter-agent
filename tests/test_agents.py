"""agents.research T10 path: web fetch -> prompt wiring + provenance, with
graceful desk-research fallback. No real network (fetch is monkeypatched)."""

from __future__ import annotations

import pytest

from job_hunter import agents, llm, research_fetch
from job_hunter.schema_extract import ExtractResult


# source_link is the t.me permalink (as in production). The REAL apply URL
# lives in the post body and is what the fetcher must target (#20).
_BODY_URL = "https://jobs.test/v/1"
_RAW = f"raw post body. Apply at {_BODY_URL}"


def _extract():
    return ExtractResult(
        title="AI Engineer",
        company="Acme",
        stack=["python", "llm"],
        source_channel="@chan",
        source_link="https://t.me/chan/777",
    )


def _research_resp(sourced=None):
    import json

    return json.dumps(
        {
            "summary": "ok",
            "talking_points": ["a"],
            "questions": ["q"],
            "sourced_facts": sourced if sourced is not None else [],
        }
    )


# --- SUCCESS: fetched text reaches the prompt, source == web ----------------

def test_research_web_success_text_reaches_prompt(monkeypatch, fake_llm):
    fake = fake_llm
    fake.set_for("research a company/role", _research_resp(["Acme builds RAG tools."]))

    seen = {}

    def fake_fetch(source_link, company=None, **kw):
        # The fetcher must be called with the IN-BODY apply URL (#20), NOT the
        # t.me permalink that lives in extracted.source_link.
        seen["url"] = source_link
        return {
            "pages": [{"url": source_link, "text": "Acme builds RAG tools for banks."}],
            "urls": [source_link],
        }

    monkeypatch.setattr(research_fetch, "fetch_research_context", fake_fetch)

    out = agents.research(fake, _extract(), _RAW, model="claude-haiku-4-5")

    assert seen["url"] == _BODY_URL  # selected the in-body URL, not t.me
    assert out["research_source"] == "web"
    assert out["fetched_urls"] == [_BODY_URL]
    assert out["sourced_facts"] == ["Acme builds RAG tools."]
    # The real fetched page text reached the prompt sent to the LLM.
    rc = [c for c in fake.calls if "research a company/role" in c["system"]][-1]
    assert "FETCHED PAGE TEXT (real, from https://jobs.test/v/1)" in rc["user"]
    assert "Acme builds RAG tools for banks." in rc["user"]
    assert "ORIGINAL POST (candidate-relevant" in rc["user"]
    # research runs on Haiku at max_tokens 900.
    assert rc["model"] == "claude-haiku-4-5"
    assert rc["max_tokens"] == 900


# --- #20: contact-as-link is preferred as the primary target ----------------

def test_research_contact_as_link_preferred_over_body(monkeypatch, fake_llm):
    fake = fake_llm
    fake.set_for("research a company/role", _research_resp(["Acme hires remote."]))

    seen = {}

    def fake_fetch(source_link, company=None, **kw):
        seen["url"] = source_link
        return {
            "pages": [{"url": source_link, "text": "Acme careers page, remote-first."}],
            "urls": [source_link],
        }

    monkeypatch.setattr(research_fetch, "fetch_research_context", fake_fetch)

    ex = ExtractResult(
        title="AI Engineer", company="Acme", source_channel="@chan",
        source_link="https://t.me/chan/777",
        contact_type="link", contact="https://company.example/jobs",
    )
    # raw_text ALSO has a different URL, but the contact-as-link wins.
    out = agents.research(fake, ex, f"see {_BODY_URL}", model="claude-haiku-4-5")

    assert seen["url"] == "https://company.example/jobs"
    assert out["research_source"] == "web"
    assert out["fetched_urls"] == ["https://company.example/jobs"]
    assert out["sourced_facts"] == ["Acme hires remote."]


# --- #20: only a t.me permalink, no in-body URL -> desk_fallback ------------

def test_research_telegram_only_desk_fallback(monkeypatch, fake_llm):
    fake = fake_llm
    fake.set_for("research a company/role", _research_resp([{"fact": "invented"}]))

    seen = {}

    def fake_fetch(source_link, company=None, **kw):
        # select_primary_url found nothing usable -> None is passed through.
        seen["url"] = source_link
        return {"pages": [], "urls": []}

    monkeypatch.setattr(research_fetch, "fetch_research_context", fake_fetch)

    ex = ExtractResult(
        title="AI Engineer", company="Acme", source_channel="@chan",
        source_link="https://t.me/chan/777",
    )
    out = agents.research(fake, ex, "Откликнуться в личку, без ссылок",
                          model="claude-haiku-4-5")

    assert seen["url"] is None  # no usable in-body URL selected
    assert out["research_source"] == "desk_fallback"
    assert out["fetched_urls"] == []
    assert out["sourced_facts"] == []  # honesty: invented facts cleared


# --- FETCH FAILURE: no raise, desk_fallback, prompt == today's desk prompt ---

@pytest.mark.parametrize(
    "fetcher",
    [
        lambda *a, **k: {"pages": [], "urls": []},          # empty
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError),  # raises internally
    ],
)
def test_research_fetch_failure_falls_back_to_desk(monkeypatch, fetcher, fake_llm):
    fake = fake_llm
    fake.set_for("research a company/role", _research_resp([]))
    monkeypatch.setattr(research_fetch, "fetch_research_context", fetcher)

    ex = _extract()
    out = agents.research(fake, ex, "raw post body", model="claude-haiku-4-5")

    assert out["research_source"] == "desk_fallback"
    assert out["fetched_urls"] == []
    assert out["sourced_facts"] == []
    rc = [c for c in fake.calls if "research a company/role" in c["system"]][-1]
    # No fetched section; equals the byte-identical desk-only prompt.
    assert "FETCHED PAGE TEXT" not in rc["user"]
    assert rc["user"] == llm.build_research_prompt(ex, "raw post body")


# --- EMPTY/IRRELEVANT page -> desk_fallback; honesty guard present ----------

def test_empty_page_yields_desk_fallback_and_preserves_empty_sourced(monkeypatch, fake_llm):
    fake = fake_llm
    fake.set_for("research a company/role", _research_resp([]))
    # empty pages (the fetcher already discarded a sub-threshold page)
    monkeypatch.setattr(
        research_fetch, "fetch_research_context",
        lambda *a, **k: {"pages": [], "urls": []},
    )
    out = agents.research(fake, _extract(), "raw", model="claude-haiku-4-5")
    assert out["research_source"] == "desk_fallback"
    # our code does NOT inject facts when the model returns none
    assert out["sourced_facts"] == []


def test_research_system_carries_no_fabrication_rule():
    s = llm.RESEARCH_SYSTEM
    assert "sourced_facts" in s
    assert "NEVER invent company facts" in s or "never fabricate" in s.lower()
    assert "информация о компании со страницы недоступна" in s


# --- research NEVER raises out of agents.research ---------------------------

def test_research_never_raises_even_if_llm_returns_garbage(monkeypatch, fake_llm):
    fake = fake_llm
    # parseable empty object -> parser fills defaults
    fake.set_for("research a company/role", "{}")
    monkeypatch.setattr(
        research_fetch, "fetch_research_context",
        lambda *a, **k: {"pages": [], "urls": []},
    )
    out = agents.research(fake, _extract(), "raw", model="claude-haiku-4-5")
    assert out["research_source"] == "desk_fallback"
    assert out["summary"] == ""
    assert out["sourced_facts"] == []


# --- desk prompt is byte-identical to the historical prompt -----------------

def test_desk_prompt_byte_identical_without_fetch():
    ex = _extract()
    legacy = (
        "Job context:\n"
        + __import__("json").dumps(
            {"title": ex.title, "company": ex.company, "stack": ex.stack, "location": ex.location},
            ensure_ascii=False,
        )
        + "\n\nOriginal post:\n" + "raw body"
    )
    assert llm.build_research_prompt(ex, "raw body") == legacy
    assert llm.build_research_prompt(ex, "raw body", {"pages": []}) == legacy
