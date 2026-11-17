"""比較・差分生成・許容差適用・判定(仕様 10.3 / 11章)。"""
from __future__ import annotations

import datetime as dt
import fnmatch
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import Diff, Verdict, Warning_

ABSENT = "<absent>"


# ---------------------------------------------------------------- 平坦化

def _row_label(key: list[str], cols: list[str], row: list, idx: int) -> str:
    if key:
        return "[" + ",".join(f"{k}={row[cols.index(k)]}" for k in key) + "]"
    return f"[#{idx}]"


def _flatten_rows(prefix: str, block: dict, out: dict[str, Any]) -> None:
    cols = [c["name"] for c in block["columns"]]
    key = block.get("key") or []
    out[f"{prefix}.columns"] = ",".join(sorted(cols))
    labels: dict[str, int] = {}
    for i, row in enumerate(block["rows"]):
        label = _row_label(key, cols, row, i)
        if label in labels:  # 主キーが重複する(=キーが当てにならない)場合は連番で区別
            label = f"{label}#{i}"
        labels[label] = i
        for c, v in zip(cols, row):
            out[f"{prefix}{label}.{c}"] = v


def flatten(observed: dict, error_compare: str = "name") -> dict[str, Any]:
    """observed → {パス: 値}。比較対象の抽出もここで行う(observe 設定は observed 生成時に反映済み)"""
    out: dict[str, Any] = {}
    if "return" in observed:
        out["observed.return"] = observed["return"]["value"]
    for name, v in (observed.get("out_args") or {}).items():
        out[f"observed.out_args.{name}"] = v["value"]
    for name, rs in (observed.get("result_sets") or {}).items():
        _flatten_rows(f"observed.result_sets.{name}", rs, out)
    err = observed.get("error")
    if error_compare == "any":
        out["observed.error"] = "<raised>" if err else None
    else:
        out["observed.error"] = err["logical_name"] if err else None
        if error_compare == "name_and_message" and err:
            out["observed.error.message"] = err.get("message")
    for i, m in enumerate(observed.get("messages") or []):
        out[f"observed.messages[{i}]"] = m
    if "messages" in observed:
        out["observed.messages.count"] = len(observed["messages"])
    for table, td in (observed.get("snapshot") or {}).items():
        _flatten_rows(f"observed.snapshot.{table}", td, out)
        out[f"observed.snapshot.{table}.row_count"] = len(td["rows"])
    return out


def _collapse_missing_rows(diffs: list[Diff]) -> list[Diff]:
    """片側にしか無い行は、列ごとではなく行単位の 1 差分にまとめる"""
    out: list[Diff] = []
    seen_rows: set[str] = set()
    for d in diffs:
        if (d.expected is ABSENT or d.actual is ABSENT) and "]" in d.path:
            row_path = d.path[: d.path.rindex("]") + 1]
            if row_path in seen_rows:
                continue
            seen_rows.add(row_path)
            out.append(Diff(row_path,
                            "<row present>" if d.actual is ABSENT else ABSENT,
                            "<row present>" if d.expected is ABSENT else ABSENT))
        else:
            out.append(d)
    return out


def compare(expected: dict, actual: dict, error_compare: str = "name") -> list[Diff]:
    e, a = flatten(expected, error_compare), flatten(actual, error_compare)
    diffs = []
    for path in list(e.keys()) + [k for k in a.keys() if k not in e]:
        ev, av = e.get(path, ABSENT), a.get(path, ABSENT)
        if ev != av:
            diffs.append(Diff(path, ev, av))
    return _collapse_missing_rows(diffs)


# ---------------------------------------------------------------- 許容差

REQUIRED_TOL_FIELDS = ("id", "scope", "rule", "reason", "approved_by", "approved_at")


def validate_tolerances(tols: list[dict]) -> None:
    ids = set()
    for t in tols:
        missing = [f for f in REQUIRED_TOL_FIELDS if not t.get(f)]
        if missing:
            raise ValueError(f"tolerances.yaml: {t.get('id', '?')} に必須項目がありません: {missing}")
        if t["id"] in ids:
            raise ValueError(f"tolerances.yaml: id 重複 {t['id']}")
        ids.add(t["id"])


def _as_decimal(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _rule_matches(rule: Any, d: Diff) -> bool:
    if rule == "ignore":
        return True
    if isinstance(rule, dict):
        ev, av = _as_decimal(d.expected), _as_decimal(d.actual)
        if ev is None or av is None:
            return False
        if "numeric_abs" in rule:
            return abs(ev - av) <= Decimal(str(rule["numeric_abs"]))
        if "numeric_rel" in rule:
            base = max(abs(ev), abs(av))
            return base == 0 or abs(ev - av) / base <= Decimal(str(rule["numeric_rel"]))
    return False


def _active(t: dict, today: dt.date) -> bool:
    exp = t.get("expires")
    if not exp:
        return True
    exp = exp if isinstance(exp, dt.date) else dt.date.fromisoformat(str(exp))
    return today <= exp


def apply_tolerances(case_id: str, diffs: list[Diff], tols: list[dict],
                     today: dt.date | None = None) -> tuple[list[Diff], list[Diff]]:
    """(未承認差分, 許容された差分) を返す"""
    today = today or dt.date.today()
    remaining, tolerated = [], []
    for d in diffs:
        hit = None
        for t in tols:
            scope = t.get("scope") or {}
            if not _active(t, today):
                continue
            if fnmatch.fnmatchcase(case_id, scope.get("case", "*")) and \
               fnmatch.fnmatchcase(d.path, scope.get("path", "*")) and _rule_matches(t["rule"], d):
                hit = t["id"]
                break
        if hit:
            d.tolerance_id = hit
            tolerated.append(d)
        else:
            remaining.append(d)
    return remaining, tolerated


def judge(diffs: list[Diff], tolerated: list[Diff], warnings: list[Warning_]) -> Verdict:
    if diffs:
        return Verdict.FAIL
    if tolerated:
        return Verdict.TOLERATED
    if warnings:
        return Verdict.PASS_WITH_WARNING
    return Verdict.PASS


def unmapped_errors(observed: dict) -> str | None:
    err = observed.get("error")
    if err and err["logical_name"].startswith("ERR_UNMAPPED"):
        return err["logical_name"]
    return None
