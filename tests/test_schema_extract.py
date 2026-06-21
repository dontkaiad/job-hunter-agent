"""Validation of the §4 Extract contract (pure)."""

from job_hunter import schema_extract as SE
from job_hunter.schema_extract import ExtractResult


def _valid():
    return ExtractResult(title="AI Engineer", source_channel="@jobs")


def test_minimal_is_valid():
    assert SE.is_valid(_valid())


def test_stack_and_reasons_default_to_lists():
    r = _valid()
    assert r.stack == [] and r.reasons == []


def test_salary_order_warning_not_hard():
    r = _valid()
    r.salary_min = 300000
    r.salary_max = 150000
    warnings = SE.validate(r)
    assert any("salary_min > salary_max" in w for w in warnings)


def test_contact_type_enum():
    r = _valid()
    r.contact_type = "email"  # not in dm|form|link
    assert any("contact_type" in w for w in warnings_of(r))
    r.contact_type = "dm"
    assert not any("contact_type" in w for w in warnings_of(r))
    r.contact_type = None
    assert not any("contact_type" in w for w in warnings_of(r))


def warnings_of(r):
    return SE.validate(r)


def test_required_title_must_not_be_null():
    r = _valid()
    r.title = None  # type: ignore[assignment]
    assert any("title must not be null" in w for w in SE.validate(r))


def test_numeric_field_rejects_bool():
    r = _valid()
    r.salary_min = True  # type: ignore[assignment]
    assert any("salary_min" in w for w in SE.validate(r))


def test_roundtrip_json():
    r = _valid()
    r.stack = ["python", "llm"]
    r.salary_min = 200000
    r.currency = "RUB"
    text = SE.serialize(r)
    back = SE.parse(text)
    assert back.stack == ["python", "llm"]
    assert back.salary_min == 200000
    assert back.currency == "RUB"


def test_from_dict_tolerates_unknown_and_missing():
    r = SE.from_dict({"title": "X", "source_channel": "@c", "bogus": 1, "stack": "python"})
    assert r.title == "X"
    assert r.stack == ["python"]  # string coerced to list
    assert r.reasons == []
