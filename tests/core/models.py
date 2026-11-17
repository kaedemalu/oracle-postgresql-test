"""Case / Result / Snapshot 型定義(仕様 5章・8章)。

ケース定義は pydantic で検証し、不正な定義は実行前(収集時)にエラーとする。
実行結果は DB 非依存の dataclass で表現する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LOGICAL_TYPES = {
    "NUMBER", "INTEGER", "FLOAT", "VARCHAR", "CHAR", "DATE", "TIMESTAMP", "TIMESTAMPTZ",
    "BOOLEAN", "CLOB", "BLOB", "REFCURSOR",
}


def base_type(logical: str | None) -> str | None:
    """`NUMBER(12,2)` / `CHAR(1)` → `NUMBER` / `CHAR`"""
    if logical is None:
        return None
    return logical.split("(", 1)[0].strip().upper()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- ケース定義

class Arg(_Strict):
    name: str
    type: str = "VARCHAR"
    value: Any = None
    mode: Literal["IN", "OUT", "INOUT"] = "IN"
    pg_type: str | None = None  # PostgreSQL 側のキャスト型を明示する場合

    @model_validator(mode="after")
    def _check(self) -> "Arg":
        if base_type(self.type) not in LOGICAL_TYPES:
            raise ValueError(f"unknown logical type: {self.type}")
        if base_type(self.type) == "REFCURSOR" and self.mode != "OUT":
            raise ValueError("REFCURSOR は OUT 専用")
        return self


class Returns(_Strict):
    type: str

    @model_validator(mode="after")
    def _check(self) -> "Returns":
        if base_type(self.type) not in LOGICAL_TYPES:
            raise ValueError(f"unknown logical type: {self.type}")
        return self


class Target(_Strict):
    kind: Literal["function", "procedure", "trigger", "sql"]
    name: str | None = None
    invoke_style: Literal["block", "select"] = "block"


class RowsSpec(_Strict):
    table: str
    data: list[dict[str, Any]] = []


class DialectSql(_Strict):
    oracle: list[str] = []
    postgres: list[str] = []


class Setup(_Strict):
    fixtures: list[str] = []
    rows: list[RowsSpec] = []
    sql: DialectSql = DialectSql()


class Call(_Strict):
    args: list[Arg] = []
    returns: Returns | None = None


class Fire(_Strict):
    """trigger: 発火させる DML"""
    table: str
    op: Literal["insert", "update", "delete"]
    data: list[dict[str, Any]] = []   # insert 行
    set: dict[str, Any] = {}          # update の SET
    where: dict[str, Any] = {}        # update / delete の条件(等価 AND)


class DialectQuery(_Strict):
    oracle: str
    postgres: str


class SnapshotSpec(_Strict):
    tables: list[str] = []
    mode: Literal["scoped", "schema_wide"] = "scoped"
    ignore_columns: dict[str, list[str]] = {}
    mask: dict[str, str] = {}  # "TABLE.COLUMN": SEQ / NOW / GUID / 任意トークン


class Observe(_Strict):
    return_: bool = Field(True, alias="return")
    out_args: bool = True
    result_sets: bool = True
    messages: bool = False
    ordered: bool = False      # 結果セットの順序を比較対象にする(ORDER BY が仕様の場合)
    snapshot: SnapshotSpec | None = None


class ErrorCompare(_Strict):
    compare: Literal["name", "name_and_message", "any"] = "name"


class Transaction(_Strict):
    commit_after_call: bool = False
    observe_from: Literal["same_session", "other_session"] = "same_session"


class Skip(_Strict):
    oracle: bool = False
    postgres: bool = False
    reason: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Skip":
        if (self.oracle or self.postgres) and not self.reason.strip():
            raise ValueError("skip には reason が必須")
        return self


class Case(_Strict):
    id: str
    title: str = ""
    tags: list[str] = []
    priority: Literal["high", "medium", "low"] = "medium"
    target: Target
    setup: Setup = Setup()
    clock: str | None = None
    call: Call = Call()
    fire: Fire | None = None
    query: str | DialectQuery | None = None
    observe: Observe = Observe()
    error: ErrorCompare = ErrorCompare()
    transaction: Transaction = Transaction()
    strict_empty: bool = False
    expect: dict[str, Any] = {}
    skip: Skip = Skip()
    # loader が付与
    suite: str = ""
    source_file: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Case":
        k = self.target.kind
        if k in ("function", "procedure") and not self.target.name:
            raise ValueError(f"{self.id}: target.name が必要")
        if k == "function" and self.call.returns is None:
            raise ValueError(f"{self.id}: function には call.returns が必要")
        if k == "function" and any(a.mode != "IN" for a in self.call.args) and self.target.invoke_style == "select":
            raise ValueError(f"{self.id}: invoke_style=select では OUT 引数を使えない")
        if k == "trigger" and self.fire is None:
            raise ValueError(f"{self.id}: trigger には fire が必要")
        if k == "sql" and self.query is None:
            raise ValueError(f"{self.id}: sql には query が必要")
        names = [a.name.upper() for a in self.call.args]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.id}: 引数名が重複している")
        return self


class SuiteFile(_Strict):
    suite: str
    defaults: dict[str, Any] = {}
    cases: list[dict[str, Any]]


# ---------------------------------------------------------------- 実行結果

@dataclass
class ColumnMeta:
    name: str
    type: str | None = None


@dataclass
class ResultSet:
    columns: list[ColumnMeta]
    rows: list[tuple]


@dataclass
class ErrorInfo:
    logical_name: str
    native_code: str
    native_message: str
    message: str = ""        # 比較用のメッセージ本文(コード接頭辞・スタックを除いたもの)
    context: dict = field(default_factory=dict)


@dataclass
class CallResult:
    return_value: Any | None = None
    return_type: str | None = None
    out_args: dict[str, tuple[Any, str | None]] = field(default_factory=dict)  # 名前 → (値, 論理型)
    result_sets: dict[str, ResultSet] = field(default_factory=dict)
    error: ErrorInfo | None = None
    messages: list[str] = field(default_factory=list)
    rows_affected: int | None = None


@dataclass
class TableData:
    columns: list[ColumnMeta]
    rows: list[tuple]
    key: list[str] = field(default_factory=list)  # 主キー列(大文字)


@dataclass
class Snapshot:
    tables: dict[str, TableData] = field(default_factory=dict)


@dataclass
class ResetScope:
    tables: list[str]
    sequences: dict[str, int]


# ---------------------------------------------------------------- 判定

class Verdict(str, Enum):
    PASS = "PASS"
    PASS_WITH_WARNING = "PASS_WITH_WARNING"
    TOLERATED = "TOLERATED"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIP = "SKIP"


@dataclass
class Diff:
    path: str
    expected: Any
    actual: Any
    tolerance_id: str | None = None

    def render(self) -> str:
        tag = f"TOLERATED({self.tolerance_id})" if self.tolerance_id else "FAIL"
        return f"{self.path}: expected={_show(self.expected)} actual={_show(self.actual)} -> {tag}"


def _show(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, str):
        return v
    return repr(v)


@dataclass
class Warning_:
    code: str
    path: str
    detail: str = ""

    def render(self) -> str:
        return f"[{self.code}] {self.path} {self.detail}".rstrip()


@dataclass
class CaseOutcome:
    case_id: str
    suite: str
    tags: list[str]
    priority: str
    mode: str
    target: str
    verdict: Verdict
    diffs: list[Diff] = field(default_factory=list)
    tolerated: list[Diff] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)
    message: str = ""
    duration_sec: float = 0.0

    def render(self) -> str:
        lines = [f"case: {self.case_id} [{self.verdict.value}] ({self.mode}/{self.target})"]
        if self.message:
            lines.append(f"  {self.message}")
        lines += [f"  {d.render()}" for d in self.diffs]
        lines += [f"  {d.render()}" for d in self.tolerated]
        lines += [f"  WARN {w.render()}" for w in self.warnings]
        return "\n".join(lines)
