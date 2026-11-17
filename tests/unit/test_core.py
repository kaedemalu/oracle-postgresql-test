"""基盤自体の単体テスト(DB 不要)。  pytest tests/unit"""
import datetime as dt
from decimal import Decimal

import pytest

from tests.core.compare import apply_tolerances, compare, judge, validate_tolerances
from tests.core.errors import ErrorMapper
from tests.core.loader import Catalog, Settings, case_hash, load_fixture
from tests.core.models import CallResult, Case, ColumnMeta, Snapshot, TableData, Verdict
from tests.core.normalize import Normalizer, norm_decimal, observe


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(Settings.load())


@pytest.mark.parametrize("raw,expected", [
    ("1.50", "1.5"), ("1E+3", "1000"), ("-0", "0"), ("0.000", "0"), ("100", "100"), ("-1.2300", "-1.23"),
])
def test_norm_decimal(raw, expected):
    assert norm_decimal(Decimal(raw)) == expected


def test_normalize_values():
    n = Normalizer({})
    assert n.value(dt.datetime(2026, 1, 1, 9, 0, 0, 123456), "DATE", "p") == "2026-01-01T09:00:00"
    assert n.value(dt.datetime(2026, 1, 1, 9, tzinfo=dt.timezone(dt.timedelta(hours=9))), None, "p") == "2026-01-01T00:00:00"
    assert n.value(dt.date(2026, 2, 28), None, "p") == "2026-02-28T00:00:00"
    assert n.value("A   ", "CHAR", "p") == "A"
    assert n.value("a\r\nb", "VARCHAR", "p") == "a\nb"
    assert n.value("が", "VARCHAR", "p") == "が"  # NFC
    assert n.value(b"\x00\x01", "BLOB", "p") == "AAE="
    assert n.value(Decimal("1"), "BOOLEAN", "p") == "true"
    assert n.value("N", "BOOLEAN", "p") == "false"
    assert n.warnings == []


def test_empty_string_warning_and_strict():
    n = Normalizer({})
    assert n.value("", "VARCHAR", "observed.return") is None
    assert n.warnings[0].code == "empty_string_vs_null"
    strict = Normalizer({}, strict_empty=True)
    assert strict.value("", "VARCHAR", "p") == ""


def _case(**kw):
    base = {"id": "T.X.001", "target": {"kind": "sql"}, "query": "select 1"}
    base.update(kw)
    return Case.model_validate(base)


def test_snapshot_sort_by_pk_and_mask():
    case = _case(observe={"snapshot": {"tables": ["T"], "mask": {"T.CREATED": "NOW"},
                                       "ignore_columns": {"T": ["UPD"]}}})
    snap = Snapshot({"T": TableData(
        columns=[ColumnMeta("ID", "INTEGER"), ColumnMeta("V", "VARCHAR"), ColumnMeta("CREATED", "DATE"),
                 ColumnMeta("UPD", "DATE")],
        rows=[(Decimal(10), "b", dt.datetime.now(), None), (Decimal(2), "a", None, None)], key=["ID"])})
    obs, _ = observe(case, CallResult(), snap, {})
    t = obs["snapshot"]["T"]
    assert [c["name"] for c in t["columns"]] == ["ID", "V", "CREATED"]
    assert t["rows"] == [["2", "a", None], ["10", "b", "<NOW>"]]


def test_compare_and_judge():
    exp = {"return": {"value": "100"}, "error": None,
           "snapshot": {"T": {"columns": [{"name": "ID"}, {"name": "A"}], "key": ["ID"],
                              "rows": [["1", "10"], ["2", "20"]]}}}
    act = {"return": {"value": "100"}, "error": {"logical_name": "ERR_UNIQUE"},
           "snapshot": {"T": {"columns": [{"name": "ID"}, {"name": "A"}], "key": ["ID"],
                              "rows": [["1", "10.01"]]}}}
    diffs = compare(exp, act)
    paths = {d.path for d in diffs}
    assert paths == {"observed.error", "observed.snapshot.T[ID=1].A", "observed.snapshot.T[ID=2]",
                     "observed.snapshot.T.row_count"}
    tols = [{"id": "TOL-X", "scope": {"case": "C.*", "path": "observed.snapshot.T*.A"},
             "rule": {"numeric_abs": "0.05"}, "reason": "r", "approved_by": "a", "approved_at": "2026-01-01"}]
    validate_tolerances(tols)
    remaining, tolerated = apply_tolerances("C.1", diffs, tols)
    assert [d.tolerance_id for d in tolerated] == ["TOL-X"]
    assert judge(remaining, tolerated, []) == Verdict.FAIL
    assert judge([], tolerated, []) == Verdict.TOLERATED
    expired = [{**tols[0], "expires": "2020-01-01"}]
    assert apply_tolerances("C.1", compare(exp, act), expired)[1] == []


def test_tolerance_requires_approval():
    with pytest.raises(ValueError):
        validate_tolerances([{"id": "TOL-1", "scope": {}, "rule": "ignore", "reason": "x"}])


def test_error_mapper(catalog):
    m = ErrorMapper(catalog.error_map)
    assert m.oracle("ORA-00001", "") == "ERR_UNIQUE"
    assert m.oracle("ORA-20001", "") == "ERR_USER_20001"
    assert m.oracle("ORA-99999", "") == "ERR_UNMAPPED:ORA-99999"
    assert m.postgres("U0001", "x") == "ERR_USER_20001"
    assert m.postgres("P0001", "[20005] boom") == "ERR_USER_20005"
    assert m.postgres("P0001", "no code") == "ERR_UNMAPPED:P0001"
    assert m.postgres("23503", 'insert or update on table "orders" violates') == "ERR_FK_PARENT_MISSING"
    assert m.postgres("23503", 'update or delete on table "customers" violates') == "ERR_FK_CHILD_EXISTS"
    assert m.postgres("P0002", "") == "ERR_NO_DATA"


def test_all_cases_valid_and_hash_stable(catalog):
    assert catalog.cases, "ケースが 1 件もない"
    fx = catalog.settings.path("fixtures")
    for c in catalog.cases:
        assert case_hash(c, fx) == case_hash(c, fx)
        for name in c.setup.fixtures:
            assert load_fixture(fx, name)


def test_invalid_case_rejected():
    with pytest.raises(Exception):
        _case(target={"kind": "function", "name": "F"})  # returns 無し
    with pytest.raises(Exception):
        _case(skip={"postgres": True})  # reason 無し
    with pytest.raises(Exception):
        _case(unknown_key=1)


def test_name_resolution(catalog):
    s = catalog.settings
    assert s.resolve_name("PKG_ORDER.CALC_TAX", "oracle") == "PKG_ORDER.CALC_TAX"
    assert s.resolve_name("PKG_ORDER.CALC_TAX", "postgres") == "pkg_order.calc_tax"
