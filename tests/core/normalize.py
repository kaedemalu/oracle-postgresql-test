"""正規化(仕様 9章)。ゴールデン生成時も比較時も必ずここを通す。

出力(observed)は JSON にそのまま書ける構造で、値は「文字列化した数値」「ISO8601 日時」等の
型付き文字列表現に統一する。
"""
from __future__ import annotations

import base64
import datetime as dt
import fnmatch
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import (
    CallResult, Case, ColumnMeta, ErrorInfo, Snapshot, Warning_, base_type,
)

_GUID = re.compile(r"^(\{)?[0-9A-Fa-f]{8}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{12}(?(1)\})$")
_MASK_TOKENS = {"SEQ": "<SEQ>", "NOW": "<NOW>", "GUID": "<GUID>"}


def norm_decimal(d: Decimal) -> str:
    """末尾ゼロ除去・指数表記→通常表記・-0→0"""
    if d.is_nan() or d.is_infinite():
        return str(d)
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


class Normalizer:
    def __init__(self, rules: dict, strict_empty: bool = False):
        self.rules = rules or {}
        s = self.rules.get("string", {})
        self.nfc = s.get("nfc", True)
        self.unify_newlines = s.get("unify_newlines", True)
        self.rstrip_char = s.get("rstrip_char", True)
        self.char_map = str.maketrans(s.get("char_map") or {})
        self.empty_as_null = self.rules.get("empty_string_as_null", True) and not strict_empty
        dtr = self.rules.get("datetime", {})
        self.dt_precision = dtr.get("precision", "second")
        b = self.rules.get("boolean", {})
        self.true_values = {str(v).upper() for v in b.get("true_values", ["1", "Y", "T", "TRUE"])}
        self.false_values = {str(v).upper() for v in b.get("false_values", ["0", "N", "F", "FALSE"])}
        self.float_digits = self.rules.get("float", {}).get("digits", 15)
        self.column_rules = self.rules.get("columns", []) or []
        self.warnings: list[Warning_] = []

    # ------------------------------------------------------------ 値

    def value(self, v: Any, logical: str | None, path: str) -> Any:
        t = base_type(logical)
        if v is None:
            return None
        if t == "BOOLEAN":
            return self._boolean(v, path)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, Decimal):
            if t == "INTEGER" and v == v.to_integral_value():
                return str(int(v))
            return norm_decimal(v)
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                return str(v)
            return norm_decimal(Decimal(format(v, f".{self.float_digits}g")))
        if isinstance(v, dt.datetime):
            if v.tzinfo is not None:
                v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
            if self.dt_precision == "second":
                v = v.replace(microsecond=0)
            return v.isoformat()
        if isinstance(v, dt.date):
            return dt.datetime(v.year, v.month, v.day).isoformat()
        if isinstance(v, (bytes, bytearray, memoryview)):
            return base64.b64encode(bytes(v)).decode("ascii")
        if hasattr(v, "read"):  # LOB
            return self.value(v.read(), logical, path)
        if isinstance(v, str):
            return self._string(v, t, path)
        return str(v)

    def _string(self, s: str, t: str | None, path: str) -> str | None:
        if self.nfc:
            s = unicodedata.normalize("NFC", s)
        if self.char_map:
            s = s.translate(self.char_map)
        if self.unify_newlines:
            s = s.replace("\r\n", "\n").replace("\r", "\n")
        if t == "CHAR" and self.rstrip_char:
            s = s.rstrip(" ")
        if s == "":
            # Oracle は '' を返さないため、ここに来るのは PostgreSQL 側(5.7)
            self.warnings.append(Warning_("empty_string_vs_null", path,
                                          "'' が返された" + ("(NULL と同一視)" if self.empty_as_null else "")))
            return None if self.empty_as_null else ""
        return s

    def _boolean(self, v: Any, path: str) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        key = norm_decimal(v) if isinstance(v, Decimal) else str(v).strip().upper()
        if key in self.true_values:
            return "true"
        if key in self.false_values:
            return "false"
        self.warnings.append(Warning_("boolean_unmapped", path, f"value={v!r}"))
        return f"<BOOLEAN?{v}>"

    # ------------------------------------------------------------ 列ルール・マスク

    def _column_rule(self, table: str, column: str) -> dict:
        rule: dict = {}
        for r in self.column_rules:
            if fnmatch.fnmatchcase(table, str(r.get("table", "*")).upper()) and \
               fnmatch.fnmatchcase(column, str(r.get("column", "*")).upper()):
                rule.update(r)
        return rule

    def _mask(self, v: Any, mask: str, path: str) -> Any:
        if v is None:
            return None
        token = _MASK_TOKENS.get(mask.upper(), mask)
        if mask.upper() == "GUID":
            raw = v.hex() if isinstance(v, (bytes, bytearray)) else str(v)
            if not (_GUID.match(raw) or re.fullmatch(r"[0-9A-Fa-f]{32}", raw)):
                return "<GUID:INVALID_FORMAT>"
        return token

    def rows(self, table: str, columns: list[ColumnMeta], rows: list[tuple], *,
             ignore: list[str] = (), case_masks: dict[str, str] | None = None,
             path_prefix: str = "") -> tuple[list[dict], list[list]]:
        ignore_u = {c.upper() for c in ignore}
        keep = [(i, c) for i, c in enumerate(columns) if c.name.upper() not in ignore_u]
        out_cols, plans = [], []
        for i, c in keep:
            name = c.name.upper()
            rule = self._column_rule(table, name)
            logical = rule.get("type") or c.type
            mask = (case_masks or {}).get(f"{table}.{name}") or rule.get("mask")
            out_cols.append({"name": name, "type": base_type(logical)})
            plans.append((i, name, logical, mask))
        out_rows = []
        for r_idx, row in enumerate(rows):
            vals = []
            for i, name, logical, mask in plans:
                p = f"{path_prefix}[#{r_idx}].{name}"
                v = self.value(row[i], logical, p)
                vals.append(self._mask(v, mask, p) if mask else v)
            out_rows.append(vals)
        return out_cols, out_rows


# ---------------------------------------------------------------- 並べ替え

def _sort_key(row: list, idx: list[int]) -> tuple:
    def k(v: Any) -> tuple:
        if v is None:
            return (2, 0, "")              # NULL は末尾
        try:
            return (0, Decimal(v), "")    # 数値は数値順
        except (InvalidOperation, TypeError, ValueError):
            return (1, 0, str(v))
    return tuple(k(row[i]) for i in idx)


def sort_rows(columns: list[dict], rows: list[list], key: list[str]) -> list[list]:
    """主キー → 全列の順でソート(9.2)"""
    names = [c["name"] for c in columns]
    idx = [names.index(k) for k in key if k in names]
    idx += [i for i in range(len(names)) if i not in idx]
    return sorted(rows, key=lambda r: _sort_key(r, idx))


# ---------------------------------------------------------------- observed 生成

def observe(case: Case, result: CallResult, snapshot: Snapshot | None, rules: dict) -> tuple[dict, list[Warning_]]:
    """CallResult / Snapshot を正規化し、ゴールデンと同形式の observed を返す"""
    n = Normalizer(rules, strict_empty=case.strict_empty)
    ob = case.observe
    observed: dict[str, Any] = {}

    if ob.return_ and case.target.kind == "function" and base_type(result.return_type) != "REFCURSOR":
        observed["return"] = {"type": base_type(result.return_type),
                              "value": n.value(result.return_value, result.return_type, "observed.return")}
    if ob.out_args:
        observed["out_args"] = {
            name: {"type": base_type(t), "value": n.value(v, t, f"observed.out_args.{name}")}
            for name, (v, t) in sorted(result.out_args.items())
        }
    if ob.result_sets:
        rs_out = {}
        for name, rs in sorted(result.result_sets.items()):
            prefix = f"observed.result_sets.{name}"
            cols, rows = n.rows(name, rs.columns, rs.rows, path_prefix=prefix)
            if not ob.ordered:
                rows = sort_rows(cols, rows, [])
            rs_out[name] = {"columns": cols, "ordered": ob.ordered, "rows": rows}
        observed["result_sets"] = rs_out
    observed["error"] = _error(result.error, n)
    if ob.messages:
        observed["messages"] = [n.value(m, "VARCHAR", f"observed.messages[{i}]") for i, m in enumerate(result.messages)]
    if snapshot is not None and ob.snapshot is not None:
        snap_out = {}
        for table, td in sorted(snapshot.tables.items()):
            cols, rows = n.rows(table, td.columns, td.rows,
                                ignore=ob.snapshot.ignore_columns.get(table, []),
                                case_masks={k.upper(): v for k, v in ob.snapshot.mask.items()},
                                path_prefix=f"observed.snapshot.{table}")
            key = [k for k in td.key if k in {c["name"] for c in cols}]
            snap_out[table] = {"columns": cols, "key": key, "rows": sort_rows(cols, rows, key)}
        observed["snapshot"] = snap_out
    return observed, n.warnings


def _error(e: ErrorInfo | None, n: Normalizer) -> dict | None:
    if e is None:
        return None
    return {
        "logical_name": e.logical_name,
        "message": n.value(e.message, "VARCHAR", "observed.error.message"),
        "native_code": e.native_code,
        "native_message": e.native_message,
        "context": e.context,
    }

