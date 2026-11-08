"""Candidate eligibility by location: FACTS from the model, DECISION by a deterministic guard.

The model only reports what a posting states about where the work can be done (relocation,
visa support, remote scope, office/hybrid, employer location). It never judges whether
anybody is eligible. The decision is `decide(facts, eligibility)`, a pure function of those
facts and of the candidate eligibility constraints loaded from the PRIVATE profile
(`eligibility:` block of config/profile.local.yaml; the committed example has none).

Verdicts: ELIGIBLE, INELIGIBLE, UNKNOWN. UNKNOWN is never a reject: the card is flagged
"гео?" and goes on to scoring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

ELIGIBLE = "ELIGIBLE"
INELIGIBLE = "INELIGIBLE"
UNKNOWN = "UNKNOWN"

REMOTE_SCOPES = ("worldwide", "region", "country", "unspecified")
ONSITE = ("office", "hybrid", "none", "unspecified")
VISA = ("offered", "not_offered")
# When relocation / visa support is offered but conditional: the facts say how.
RELOCATION_LIMITS = ("citizens_or_permit_only", "within_employer_country_only")


# --- the candidate's constraints (private data, loaded from the profile) -------

@dataclass(frozen=True)
class HomeMarket:
    """A jurisdiction where an employer is acceptable without relocation.

    ``names`` are lower-case aliases the extractor may report for the employer country.
    ``office_cities`` are the only cities where an office/hybrid role there is acceptable
    (remote work for such an employer is always acceptable)."""

    names: Tuple[str, ...] = ()
    office_cities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Eligibility:
    """Candidate eligibility constraints. Empty = no home market (nothing is special)."""

    home_markets: Tuple[HomeMarket, ...] = ()


def _lower_tuple(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple)) else [value]
    return tuple(str(v).strip().lower() for v in items if str(v).strip())


def parse_eligibility(data: Optional[Mapping[str, Any]]) -> Eligibility:
    """Turn the profile's ``eligibility`` mapping into an Eligibility. PURE; tolerant."""
    if not isinstance(data, Mapping):
        return Eligibility()
    markets = []
    for m in data.get("home_markets") or []:
        if isinstance(m, Mapping):
            names = _lower_tuple(m.get("names"))
            if names:
                markets.append(HomeMarket(names=names, office_cities=_lower_tuple(m.get("office_cities"))))
    return Eligibility(home_markets=tuple(markets))


# --- facts ----------------------------------------------------------------------

@dataclass
class GeoFacts:
    relocation_offered: Optional[bool] = None        # explicit mention of relocation help / visa support
    visa_sponsorship: Optional[str] = None           # offered | not_offered | None
    remote_scope: str = "unspecified"                # worldwide | region | country | unspecified
    remote_locations: List[str] = field(default_factory=list)
    onsite: str = "unspecified"                      # office | hybrid | none (fully remote) | unspecified
    role_city: Optional[str] = None
    employer_country: Optional[str] = None
    employer_city: Optional[str] = None
    # Set only when relocation/visa support is offered AND restricted: to citizens of a bloc
    # or to people who already hold a work permit / residency (citizens_or_permit_only), or to
    # moves inside the employer's own country (within_employer_country_only).
    relocation_restriction: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relocation_offered": self.relocation_offered, "visa_sponsorship": self.visa_sponsorship,
            "remote_scope": self.remote_scope, "remote_locations": list(self.remote_locations),
            "onsite": self.onsite, "role_city": self.role_city,
            "employer_country": self.employer_country, "employer_city": self.employer_city,
            "relocation_restriction": self.relocation_restriction,
        }


def _opt_str(v: Any) -> Optional[str]:
    s = str(v).strip() if v is not None else ""
    return s or None


def parse_facts(data: Optional[Mapping[str, Any]]) -> GeoFacts:
    """Validate a model's tool output into GeoFacts. PURE; unknown values degrade to
    'not stated' (never to a guess)."""
    d = data or {}
    reloc = d.get("relocation_offered")
    visa = _opt_str(d.get("visa_sponsorship"))
    scope = _opt_str(d.get("remote_scope")) or "unspecified"
    onsite = _opt_str(d.get("onsite")) or "unspecified"
    locs = d.get("remote_locations")
    return GeoFacts(
        relocation_offered=reloc if isinstance(reloc, bool) else None,
        visa_sponsorship=visa if visa in VISA else None,
        remote_scope=scope if scope in REMOTE_SCOPES else "unspecified",
        remote_locations=[str(x).strip() for x in locs if str(x).strip()] if isinstance(locs, list) else [],
        onsite=onsite if onsite in ONSITE else "unspecified",
        role_city=_opt_str(d.get("role_city")),
        employer_country=_opt_str(d.get("employer_country")),
        employer_city=_opt_str(d.get("employer_city")),
        relocation_restriction=(_opt_str(d.get("relocation_restriction"))
                                if _opt_str(d.get("relocation_restriction")) in RELOCATION_LIMITS else None),
    )


# --- the decision -----------------------------------------------------------------

def _names_in(text: str, names: Tuple[str, ...]) -> bool:
    """True when the text IS one of the names or contains one as a whole word/phrase.
    (A name never matches inside a longer word: 'no' must not match 'norway'.) PURE."""
    import re

    t = (text or "").strip().lower()
    if not t:
        return False
    return any(t == n or re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", t) for n in names if n)


def _match_market(country: Optional[str], elig: Eligibility) -> Optional[HomeMarket]:
    for m in elig.home_markets:
        if _names_in(country or "", m.names):
            return m
    return None


def _city_ok(facts: GeoFacts, market: HomeMarket) -> Optional[bool]:
    """True/False when the role's city is known, None when it is not stated.
    The city text may list several places; it is eligible when it names an eligible one."""
    city = (facts.role_city or facts.employer_city or "").strip().lower()
    if not city:
        return None
    return _names_in(city, market.office_cities)


_GLOBAL_WORDS = ("worldwide", "world-wide", "global", "anywhere", "any country", "any location",
                 "весь мир", "по всему миру", "любая страна", "из любой")


def _is_global_entry(entry: str) -> bool:
    e = (entry or "").strip().lower()
    return any(w in e for w in _GLOBAL_WORDS)


def effective_scope(facts: GeoFacts) -> str:
    """The remote scope the facts actually support. PURE.

    'worldwide' means NO restriction: if the model also lists restrictive places
    (UK, Europe, USA ...) the scope is a restricted one, 'country' for one named place,
    'region' for several."""
    restrictive = [x for x in facts.remote_locations if not _is_global_entry(x)]
    if facts.remote_scope == "worldwide":
        if not restrictive:
            return "worldwide"
        return "country" if len(restrictive) == 1 else "region"
    if facts.remote_scope == "country" and len(restrictive) >= 2:
        return "region"          # a list of countries limits remote work like a region does
    return facts.remote_scope


def _locations_include_home(facts: GeoFacts, elig: Eligibility) -> bool:
    for loc in facts.remote_locations:
        if _match_market(loc, elig) is not None:
            return True
    return False


def relocation_counts(facts: GeoFacts) -> bool:
    """Relocation / visa support counts as offered on ANY explicit mention, except when it is
    restricted to citizens of a bloc / people who already hold a work permit or residency, or to
    moves inside the employer's own country. PURE."""
    offered = facts.relocation_offered is True or facts.visa_sponsorship == "offered"
    return offered and facts.relocation_restriction is None


def decide(facts: GeoFacts, elig: Eligibility) -> Tuple[str, str]:
    """(verdict, reason) from the facts and the candidate's constraints. PURE.

    1. relocation or visa support explicitly offered (and not restricted to citizens / permit
       holders / moves inside the employer's country)             -> ELIGIBLE
    2. remote worldwide (no restrictive place listed)             -> ELIGIBLE
    3. employer in a home market:
         remote / no on-site requirement                          -> ELIGIBLE
         office or hybrid in an eligible city                     -> ELIGIBLE
         office or hybrid elsewhere                               -> INELIGIBLE
         office or hybrid, city not stated                        -> UNKNOWN
    4. remote limited by place:
         the allowed places include a home market (residency is met) -> ELIGIBLE
         a region (Europe, EMEA ...) or a list of countries           -> UNKNOWN ('гео?', not cut)
         a single country outside the home markets                   -> INELIGIBLE
    5. office / hybrid without relocation                         -> INELIGIBLE
    6. anything else (nothing usable stated)                      -> UNKNOWN (never a reject)
    """
    if relocation_counts(facts):
        return ELIGIBLE, "relocation or visa support offered"
    scope = effective_scope(facts)
    if scope == "worldwide":
        return ELIGIBLE, "remote worldwide"
    market = _match_market(facts.employer_country, elig)
    if market is not None:
        if facts.onsite in ("office", "hybrid"):
            ok = _city_ok(facts, market)
            if ok is True:
                return ELIGIBLE, "home-market employer, office/hybrid in an eligible city"
            if ok is False:
                return INELIGIBLE, "home-market employer, office/hybrid in a non-eligible city"
            return UNKNOWN, "home-market employer, office/hybrid city not stated"
        return ELIGIBLE, "home-market employer, remote"
    if scope in ("country", "region"):
        if _locations_include_home(facts, elig):
            return ELIGIBLE, "remote limited to places that include a home market"
        if scope == "region":
            return UNKNOWN, "remote limited to a region or several countries"
        return INELIGIBLE, "remote tied to a single country outside the home markets"
    if facts.onsite in ("office", "hybrid"):
        return INELIGIBLE, "office/hybrid without relocation"
    return UNKNOWN, "no usable location facts"


# --- the extraction (facts only) --------------------------------------------------

GEO_SYSTEM = (
    "You extract FACTS about where a job can be done, from a job posting (and the text of "
    "the page it links to, when given). You do NOT judge whether anyone is eligible or the "
    "job is suitable. Report only what the text states; when it does not say, use null / "
    "'unspecified'. Never guess.\n"
    "- relocation_offered: true when the text explicitly mentions relocation help, a "
    "relocation package, OR visa support / sponsorship as available (any explicit mention "
    "counts); false only when it explicitly says there is none; otherwise null.\n"
    "- visa_sponsorship: 'offered' / 'not_offered' when stated, otherwise null.\n"
    "- remote_scope: 'worldwide' only when the text explicitly allows working from any "
    "country / anywhere; 'region' when remote work is limited to a region (EMEA, Europe, EU, "
    "LATAM, North America, a time-zone band); 'country' when limited to named countries or "
    "it requires residency / work authorisation in a country; 'unspecified' when not stated "
    "(also for on-site jobs).\n"
    "- remote_locations: the countries / regions named as allowed, as written.\n"
    "- relocation_restriction: only when relocation / visa support IS offered but conditional: "
    "'citizens_or_permit_only' when it is limited to citizens of a bloc (e.g. EU citizens) or to "
    "people who already have a work permit / residency; 'within_employer_country_only' when it "
    "covers moves inside the employer's own country only; otherwise null.\n"
    "- onsite: 'office' or 'hybrid' when the text requires presence in an office, 'none' "
    "when the job is fully remote, 'unspecified' when not stated.\n"
    "- role_city: the city of the office when the role is office/hybrid and it is stated.\n"
    "- employer_country / employer_city: where the hiring company is based, only when the "
    "text states it or makes it explicit (an office address, a named legal entity); "
    "otherwise null.\n"
    "Answer by calling the `submit_geo_facts` tool."
).replace("\\\n", "\n")

GEO_TOOL: Dict[str, Any] = {
    "name": "submit_geo_facts",
    "description": "Submit the location facts stated by the job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relocation_offered": {"type": ["boolean", "null"]},
            "visa_sponsorship": {"type": ["string", "null"], "enum": ["offered", "not_offered", None]},
            "remote_scope": {"type": "string", "enum": list(REMOTE_SCOPES)},
            "remote_locations": {"type": "array", "items": {"type": "string"}},
            "onsite": {"type": "string", "enum": list(ONSITE)},
            "role_city": {"type": ["string", "null"]},
            "employer_country": {"type": ["string", "null"]},
            "employer_city": {"type": ["string", "null"]},
            "relocation_restriction": {"type": ["string", "null"], "enum": ["citizens_or_permit_only", "within_employer_country_only", None]},
        },
        "required": ["relocation_offered", "visa_sponsorship", "remote_scope", "remote_locations",
                     "onsite", "role_city", "employer_country", "employer_city", "relocation_restriction"],
    },
}

GEO_MAX_TOKENS = 400
GEO_TEXT_CAP = 12000


def build_geo_prompt(raw_text: str, fetched_text: Optional[str] = None) -> str:
    text = (raw_text or "")[:GEO_TEXT_CAP]
    page = (fetched_text or "")[:GEO_TEXT_CAP]
    return "POSTING:\n" + text + (("\n\nLINKED PAGE TEXT:\n" + page) if page else "")


def llm_geo_facts(client: Any, raw_text: str, fetched_text: Optional[str] = None,
                  model: Optional[str] = None) -> GeoFacts:
    """One forced tool call at temperature 0 -> GeoFacts. Raises on transport/shape errors."""
    from . import llm

    with llm.usage_step("geo_facts"):
        data = client.complete_tool(
            GEO_SYSTEM, build_geo_prompt(raw_text, fetched_text), GEO_TOOL,
            max_tokens=GEO_MAX_TOKENS, model=model, cache_system=False, temperature=0.0,
        )
    return parse_facts(data)
