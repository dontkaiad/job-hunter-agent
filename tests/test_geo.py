"""Location eligibility: facts from the model, decision by a deterministic guard."""
import json
import types

import pytest

from evals import common, geo_facts_eval as gfe
from job_hunter import geo, golden
from job_hunter.profile import example_profile, parse_profile

F = geo.GeoFacts
HOME = geo.Eligibility(home_markets=(geo.HomeMarket(names=("home land", "hl"), office_cities=("home city", "hc")),))


# --- the operator's four rules + the home-market exception -------------------------

@pytest.mark.parametrize("facts,verdict", [
    (F(relocation_offered=True, onsite="office"), geo.ELIGIBLE),                    # relocation offered
    (F(visa_sponsorship="offered", remote_scope="country"), geo.ELIGIBLE),         # visa support offered
    (F(remote_scope="worldwide"), geo.ELIGIBLE),                                   # remote worldwide
    (F(remote_scope="country", remote_locations=["X"]), geo.INELIGIBLE),           # remote (country)
    (F(remote_scope="region", remote_locations=["EMEA"]), geo.UNKNOWN),            # a region: not cut, 'гео?' 
    (F(onsite="office"), geo.INELIGIBLE),                                          # office without relocation
    (F(onsite="hybrid", relocation_offered=False), geo.INELIGIBLE),                # hybrid without relocation
    (F(), geo.UNKNOWN),                                                            # nothing stated: never a reject
    (F(onsite="none"), geo.UNKNOWN),                                               # fully remote, no scope stated
    (F(visa_sponsorship="not_offered", onsite="office"), geo.INELIGIBLE),
])
def test_rules_without_a_home_market(facts, verdict):
    assert geo.decide(facts, geo.Eligibility())[0] == verdict


@pytest.mark.parametrize("facts,verdict", [
    (F(employer_country="Home Land", onsite="none"), geo.ELIGIBLE),                              # remote
    (F(employer_country="home land"), geo.ELIGIBLE),                                             # no on-site requirement stated
    (F(employer_country="HL", onsite="hybrid", role_city="Home City"), geo.ELIGIBLE),            # eligible city
    (F(employer_country="home land", onsite="office", role_city="Other City"), geo.INELIGIBLE),  # other city, no relocation
    (F(employer_country="home land", onsite="office", employer_city="Other City"), geo.INELIGIBLE),
    (F(employer_country="home land", onsite="office"), geo.UNKNOWN),                             # city not stated
    (F(employer_country="home land", onsite="office", role_city="Other City", relocation_offered=True), geo.ELIGIBLE),
    (F(employer_country="elsewhere", onsite="office"), geo.INELIGIBLE),                          # not a home market
    (F(employer_country="elsewhere", remote_scope="worldwide"), geo.ELIGIBLE),
])
def test_home_market_exception(facts, verdict):
    assert geo.decide(facts, HOME)[0] == verdict


def test_reasons_are_reported():
    assert geo.decide(F(remote_scope="region"), geo.Eligibility())[1] == "remote limited to a region or several countries"
    assert geo.decide(F(), geo.Eligibility()) == (geo.UNKNOWN, "no usable location facts")


# --- facts parsing: never guess ----------------------------------------------------

def test_parse_facts_degrades_bad_values_to_not_stated():
    f = geo.parse_facts({"relocation_offered": "yes", "visa_sponsorship": "maybe", "remote_scope": "everywhere",
                         "onsite": "somewhere", "remote_locations": "EU", "role_city": "  ", "employer_country": None})
    assert (f.relocation_offered, f.visa_sponsorship, f.remote_scope, f.onsite) == (None, None, "unspecified", "unspecified")
    assert f.remote_locations == [] and f.role_city is None and f.employer_country is None
    assert geo.parse_facts(None) == geo.GeoFacts()
    g = geo.parse_facts({"relocation_offered": True, "visa_sponsorship": "offered", "remote_scope": "region",
                         "remote_locations": ["EMEA", " "], "onsite": "hybrid", "role_city": "Berlin"})
    assert g.to_dict()["remote_locations"] == ["EMEA"] and g.relocation_offered is True


# --- the private constraints come from the profile ---------------------------------

def test_example_profile_has_no_home_market_and_the_code_has_nothing_personal():
    import pathlib
    assert example_profile().eligibility.home_markets == ()
    root = pathlib.Path(__file__).resolve().parent.parent
    geo_src = (root / "job_hunter" / "geo.py").read_text(encoding="utf-8").lower()
    for needle in ("росси", "russia", "санкт", "petersburg", " рф", "moscow"):
        assert needle not in geo_src, needle
    assert "eligibility" not in (root / "config" / "profile.example.yaml").read_text(encoding="utf-8").lower() or \
        example_profile().eligibility.home_markets == ()


def test_profile_parses_the_eligibility_block_tolerantly():
    p = parse_profile({"eligibility": {"home_markets": [{"names": ["Home Land", "HL"], "office_cities": ["Home City"]},
                                                          {"names": []}, "junk"]}})
    assert p.eligibility.home_markets == (geo.HomeMarket(names=("home land", "hl"), office_cities=("home city",)),)
    assert parse_profile({"eligibility": "nonsense"}).eligibility == geo.Eligibility()


# --- the extraction call ------------------------------------------------------------

class FakeClient:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def complete_tool(self, system, user, tool, **kw):
        self.calls.append((system, user, tool, kw))
        return self.payload


def test_llm_geo_facts_is_a_forced_tool_call_at_temperature_zero():
    c = FakeClient({"relocation_offered": True, "visa_sponsorship": None, "remote_scope": "worldwide",
                    "remote_locations": [], "onsite": "none", "role_city": None,
                    "employer_country": "Somewhere", "employer_city": None})
    f = geo.llm_geo_facts(c, "Remote role, relocation help, anywhere", "page text", model="m")
    assert f.remote_scope == "worldwide" and f.employer_country == "Somewhere"
    system, user, tool, kw = c.calls[0]
    assert tool["name"] == "submit_geo_facts" and kw["temperature"] == 0.0 and kw["model"] == "m"
    assert "do not judge" in system.lower() and "LINKED PAGE TEXT" in user
    assert set(tool["input_schema"]["required"]) == set(tool["input_schema"]["properties"])
    assert "\\n" not in geo.GEO_SYSTEM                                 # real newlines, not escaped ones


def test_prompt_caps_a_runaway_text():
    assert len(geo.build_geo_prompt("x" * 50_000)) < geo.GEO_TEXT_CAP + 50


# --- the eval guards -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean():
    common._GATE.clear()
    yield
    common._GATE.clear()


def test_offline_eval_passes():
    assert all(gfe.run_offline().values())


def test_extract_refuses_without_max_usd(capsys):
    with pytest.raises(SystemExit) as e:
        gfe.main(["extract"])
    assert e.value.code == 2 and "--max-usd" in capsys.readouterr().err


def test_held_out_needs_final_and_is_looked_at_once(conn):
    with pytest.raises(SystemExit, match="--final"):
        import evals.geo_facts_eval as m
        m.db_ctx = lambda: (types.SimpleNamespace(database_url="x"), conn)
        m.run_report("held-out", False, False)
    gfe.one_look(conn, "held-out")
    with pytest.raises(SystemExit, match="already looked at 1"):
        gfe.one_look(conn, "held-out")
    gfe.one_look(conn, "held-out", allow_repeat=True)             # explicit override is possible and recorded
    assert conn.execute("select count(*) n from eval_looks where split='held-out'").fetchone()["n"] == 2
    gfe.one_look(conn, "dev")                                        # a different split has its own count


def test_the_report_and_offline_paths_never_use_the_api():
    import inspect
    for fn in (gfe.run_report, gfe.metrics, gfe.run_offline):
        src = inspect.getsource(fn)
        assert "make_client" not in src and "AnthropicClient" not in src


def test_metrics_counts():
    rows = [{"label": "no", "chips": "geo,fit", "verdict": geo.INELIGIBLE, "reason": "r1"},
            {"label": "no", "chips": "geo", "verdict": geo.ELIGIBLE, "reason": "r2"},
            {"label": "yes", "chips": "", "verdict": geo.INELIGIBLE, "reason": "r3"},
            {"label": "yes", "chips": "", "verdict": geo.UNKNOWN, "reason": "r4"},
            {"label": "no", "chips": "stale", "verdict": geo.INELIGIBLE, "reason": "r5"}]
    m = gfe.metrics(rows)
    assert m["geo_chip"] == {"n": 2, "cut": 1, "eligible": 1, "unknown_geo?": 0}
    assert m["yes"] == {"n": 2, "wrongly_cut": 1, "eligible": 0, "unknown_geo?": 1}
    assert m["other_no"]["cut"] == 1 and m["geo_unknown_share_overall"] == 0.2


def test_split_flag_matches_the_hash_rule():
    assert gfe.split_of({"held_out": True}) == "held-out" and gfe.split_of({"held_out": False}) == "dev"
    assert sum(golden.held_out(i) for i in range(1, 1001)) in range(250, 350)


# --- consistency fixes found on the dev split ---------------------------------------

def test_worldwide_with_restrictive_places_is_not_worldwide():
    assert geo.effective_scope(F(remote_scope="worldwide", remote_locations=["UK", "Europe"])) == "region"
    assert geo.effective_scope(F(remote_scope="worldwide", remote_locations=["USA"])) == "country"
    assert geo.effective_scope(F(remote_scope="worldwide", remote_locations=["Worldwide", "весь мир"])) == "worldwide"
    assert geo.effective_scope(F(remote_scope="worldwide")) == "worldwide"
    assert geo.effective_scope(F(remote_scope="region")) == "region"
    assert geo.decide(F(remote_scope="worldwide", remote_locations=["UK", "Europe"]), geo.Eligibility())[0] == geo.UNKNOWN
    assert geo.decide(F(remote_scope="worldwide", remote_locations=["USA"]), geo.Eligibility())[0] == geo.INELIGIBLE
    assert geo.decide(F(remote_scope="worldwide", remote_locations=["Anywhere"]), geo.Eligibility())[0] == geo.ELIGIBLE


def test_remote_limited_to_a_place_that_includes_the_home_market_is_eligible():
    f = F(remote_scope="country", remote_locations=["Home Land"])                    # employer not identified
    assert geo.decide(f, HOME) == (geo.ELIGIBLE, "remote limited to places that include a home market")
    assert geo.decide(F(remote_scope="region", remote_locations=["Europe", "HL"]), HOME)[0] == geo.ELIGIBLE
    assert geo.decide(F(remote_scope="country", remote_locations=["Elsewhere"]), HOME)[0] == geo.INELIGIBLE
    assert geo.decide(f, geo.Eligibility())[0] == geo.INELIGIBLE                      # one country, no home market


# --- matching is by whole word, never by substring ---------------------------------

def test_a_short_code_never_matches_inside_a_longer_name():
    e = geo.Eligibility(home_markets=(geo.HomeMarket(names=("russia", "russian federation"), office_cities=("st petersburg",)),))
    assert geo._match_market("US", e) is None and geo._match_market("USA", e) is None
    assert geo._match_market("Russia", e) is not None and geo._match_market("Russian Federation", e) is not None
    assert geo._match_market("Southern Russia", e) is not None                      # name as a whole word
    assert geo._match_market("Prussia", e) is None and geo._match_market("", e) is None
    assert geo.decide(F(remote_scope="country", remote_locations=["US"]), e)[0] == geo.INELIGIBLE


def test_city_text_may_list_several_places():
    e = geo.Eligibility(home_markets=(geo.HomeMarket(names=("home land",), office_cities=("home city",)),))
    f = F(employer_country="home land", onsite="hybrid", role_city="Other City, Home City")
    assert geo.decide(f, e)[0] == geo.ELIGIBLE
    f2 = F(employer_country="home land", onsite="hybrid", role_city="Other City, Third City")
    assert geo.decide(f2, e)[0] == geo.INELIGIBLE
    assert geo._city_ok(F(role_city="Nearly Home Cityville"), e.home_markets[0]) is False


# --- rule changes agreed before the held-out look ----------------------------------

@pytest.mark.parametrize("restriction", ["citizens_or_permit_only", "within_employer_country_only"])
def test_restricted_relocation_does_not_count_as_offered(restriction):
    f = F(relocation_offered=True, relocation_restriction=restriction, onsite="office")
    assert geo.relocation_counts(f) is False
    assert geo.decide(f, geo.Eligibility()) == (geo.INELIGIBLE, "office/hybrid without relocation")
    g = F(visa_sponsorship="offered", relocation_restriction=restriction, remote_scope="country", remote_locations=["X"])
    assert geo.decide(g, geo.Eligibility())[0] == geo.INELIGIBLE


def test_any_other_explicit_relocation_mention_counts():
    assert geo.relocation_counts(F(relocation_offered=True)) is True
    assert geo.relocation_counts(F(visa_sponsorship="offered")) is True
    assert geo.relocation_counts(F(relocation_offered=False)) is False and geo.relocation_counts(F()) is False


def test_a_list_of_countries_is_treated_like_a_region_and_not_cut():
    f = F(remote_scope="country", remote_locations=["Poland", "Spain", "Portugal"])
    assert geo.effective_scope(f) == "region" and geo.decide(f, geo.Eligibility())[0] == geo.UNKNOWN
    assert geo.decide(F(remote_scope="country", remote_locations=["Poland"]), geo.Eligibility())[0] == geo.INELIGIBLE
    assert geo.decide(F(remote_scope="country", remote_locations=["Poland", "Home Land"]), HOME)[0] == geo.ELIGIBLE


def test_relocation_restriction_is_parsed_strictly():
    assert geo.parse_facts({"relocation_restriction": "citizens_or_permit_only"}).relocation_restriction == "citizens_or_permit_only"
    assert geo.parse_facts({"relocation_restriction": "whatever"}).relocation_restriction is None
    assert "relocation_restriction" in geo.GEO_TOOL["input_schema"]["required"]


def test_metrics_variants_drop_only_the_acknowledged_label_errors():
    from evals import geo_facts_eval as gfe
    rows = [{"label": "yes", "chips": "", "verdict": geo.INELIGIBLE, "reason": gfe.LABEL_ERROR_REASON},
            {"label": "yes", "chips": "", "verdict": geo.INELIGIBLE, "reason": "remote tied to a single country outside the home markets"},
            {"label": "no", "chips": "geo", "verdict": geo.INELIGIBLE, "reason": gfe.LABEL_ERROR_REASON}]
    v = gfe.metrics_variants(rows)
    assert v["acknowledged_label_errors_excluded"] == 1
    assert v["as_labelled"]["yes"]["wrongly_cut"] == 2 and v["without_acknowledged_label_errors"]["yes"]["wrongly_cut"] == 1
    assert v["without_acknowledged_label_errors"]["geo_chip"]["n"] == 1             # a 'no' card with that reason stays
