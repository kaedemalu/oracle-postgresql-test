"""Oracle アダプタ(仕様 6.2)。python-oracledb Thin モード。"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

import oracledb

from ..core.models import (
    CallResult, Case, ColumnMeta, ErrorInfo, Fire, ResetScope, ResultSet, RowsSpec, Setup, Snapshot,
    SnapshotSpec, TableData, base_type,
)
from .base import AdapterBase

oracledb.defaults.fetch_decimals = True   # NUMBER は Decimal で受ける(精度保持)
oracledb.defaults.fetch_lobs = False      # CLOB/BLOB は str/bytes で受ける

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*(\.[A-Za-z][A-Za-z0-9_$#]*)*$")
_PLSQL_HEAD = re.compile(
    r"^\s*(CREATE\s+(OR\s+REPLACE\s+)?(EDITIONABLE\s+|NONEDITIONABLE\s+)?"
    r"(PACKAGE|FUNCTION|PROCEDURE|TRIGGER|TYPE)\b|BEGIN\b|DECLARE\b)", re.I)


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"不正な識別子: {name!r}")
    return name.upper()


def _logical_from_dict(data_type: str, precision: int | None, scale: int | None) -> str:
    dt_ = data_type.upper()
    if dt_ == "NUMBER":
        return "INTEGER" if scale == 0 else "NUMBER"
    if dt_ in ("FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"):
        return "FLOAT"
    if dt_ in ("VARCHAR2", "NVARCHAR2", "LONG"):
        return "VARCHAR"
    if dt_ in ("CHAR", "NCHAR"):
        return "CHAR"
    if dt_ == "DATE":
        return "DATE"
    if dt_.startswith("TIMESTAMP"):
        return "TIMESTAMPTZ" if "TIME ZONE" in dt_ else "TIMESTAMP"
    if dt_ in ("CLOB", "NCLOB"):
        return "CLOB"
    if dt_ in ("BLOB", "RAW", "LONG RAW"):
        return "BLOB"
    if dt_ == "BOOLEAN":
        return "BOOLEAN"
    return dt_


_DESC_TYPES = {
    oracledb.DB_TYPE_BINARY_DOUBLE: "FLOAT", oracledb.DB_TYPE_BINARY_FLOAT: "FLOAT",
    oracledb.DB_TYPE_VARCHAR: "VARCHAR", oracledb.DB_TYPE_NVARCHAR: "VARCHAR", oracledb.DB_TYPE_LONG: "VARCHAR",
    oracledb.DB_TYPE_CHAR: "CHAR", oracledb.DB_TYPE_NCHAR: "CHAR",
    oracledb.DB_TYPE_DATE: "DATE", oracledb.DB_TYPE_TIMESTAMP: "TIMESTAMP",
    oracledb.DB_TYPE_TIMESTAMP_TZ: "TIMESTAMPTZ", oracledb.DB_TYPE_TIMESTAMP_LTZ: "TIMESTAMPTZ",
    oracledb.DB_TYPE_CLOB: "CLOB", oracledb.DB_TYPE_NCLOB: "CLOB",
    oracledb.DB_TYPE_BLOB: "BLOB", oracledb.DB_TYPE_RAW: "BLOB",
    oracledb.DB_TYPE_BOOLEAN: "BOOLEAN", oracledb.DB_TYPE_CURSOR: "REFCURSOR",
}


def _desc_type(d) -> str:
    if d.type_code is oracledb.DB_TYPE_NUMBER:
        return "INTEGER" if d.scale == 0 and (d.precision or 0) > 0 else "NUMBER"
    return _DESC_TYPES.get(d.type_code, str(d.type_code))


def _arglist(args: list[tuple[str, str, str]], bool_locals: bool = False) -> str:
    parts = [f"{_ident(n)} => {'l_' if bool_locals and base_type(t) == 'BOOLEAN' else ':'}b{i}"
             for i, (n, _, t) in enumerate(args)]
    return f"({', '.join(parts)})" if parts else ""


def build_plsql_call(name: str, args: list[tuple[str, str, str]], ret_type: str | None = None) -> str:
    """サブプログラム呼び出しの PL/SQL ブロックを生成する。引数は (名前, モード, 論理型)、バインド名は :b<i> / :ret。

    PL/SQL の BOOLEAN はクライアントからバインドできない(11g)ため、BOOLEAN を含む場合は
    ローカル変数 l_b<i> / l_ret を経由し、バインドは 1 / 0 / NULL の NUMBER でやり取りする。
    """
    bool_idx = [i for i, (_, _, t) in enumerate(args) if base_type(t) == "BOOLEAN"]
    ret_bool = ret_type is not None and base_type(ret_type) == "BOOLEAN"
    if not bool_idx and not ret_bool:
        call = f"{name}{_arglist(args)}"
        return f"BEGIN :ret := {call}; END;" if ret_type is not None else f"BEGIN {call}; END;"

    def to_num(var: str) -> str:
        return f"CASE WHEN {var} THEN 1 WHEN NOT {var} THEN 0 END"

    decl = [f"l_b{i} BOOLEAN;" for i in bool_idx] + (["l_ret BOOLEAN;"] if ret_bool else [])
    body = [f"l_b{i} := CASE :b{i} WHEN 1 THEN TRUE WHEN 0 THEN FALSE END;"
            for i in bool_idx if args[i][1] in ("IN", "INOUT")]
    call = f"{name}{_arglist(args, bool_locals=True)}"
    if ret_type is None:
        body.append(f"{call};")
    else:
        body.append(f"{'l_ret' if ret_bool else ':ret'} := {call};")
    body += [f":b{i} := {to_num(f'l_b{i}')};" for i in bool_idx if args[i][1] in ("OUT", "INOUT")]
    if ret_bool:
        body.append(f":ret := {to_num('l_ret')};")
    return f"DECLARE {' '.join(decl)} BEGIN {' '.join(body)} END;"


class OracleAdapter(AdapterBase):
    dialect = "oracle"
    conn: oracledb.Connection

    # ------------------------------------------------------------ 接続
    def connect(self) -> None:
        c = self.cfg
        self.conn = oracledb.connect(user=c["user"], password=c["password"], host=c["host"],
                                     port=int(c["port"]), service_name=c["service"])
        with self.conn.cursor() as cur:
            for k, v in (c.get("session") or {}).items():
                stmt = f"ALTER SESSION SET {_ident(k)} = '{str(v).replace(chr(39), '')}'"
                self.log(stmt)
                cur.execute(stmt)

    def close(self) -> None:
        try:
            self.conn.rollback()
        finally:
            self.conn.close()

    def commit(self) -> None:
        self.log("COMMIT")
        self.conn.commit()

    def rollback(self) -> None:
        self.log("ROLLBACK")
        self.conn.rollback()

    def version_info(self) -> dict:
        with self.conn.cursor() as cur:
            cur.execute("SELECT parameter, value FROM nls_session_parameters "
                        "WHERE parameter IN ('NLS_DATE_FORMAT','NLS_TIMESTAMP_FORMAT',"
                        "'NLS_NUMERIC_CHARACTERS','NLS_SORT','NLS_COMP')")
            nls = dict(cur.fetchall())
            cur.execute("SELECT SESSIONTIMEZONE, (SELECT value FROM nls_database_parameters "
                        "WHERE parameter = 'NLS_CHARACTERSET') FROM DUAL")
            tz, charset = cur.fetchone()
        return {"dialect": "oracle", "version": self.conn.version,
                "nls": {**nls, "TIME_ZONE": tz, "NLS_CHARACTERSET": charset}}

    # ------------------------------------------------------------ 値
    def bool_value(self, b: bool) -> Any:
        return 1 if b else 0

    def string_value(self, s: str) -> Any:
        return None if s == "" else s   # 5.7: Oracle には空文字を NULL として渡す

    # ------------------------------------------------------------ メタ情報
    def column_types(self, table: str) -> list[tuple[str, str]]:
        t = _ident(table)
        if t not in self._coltypes:
            with self.conn.cursor() as cur:
                cur.execute("SELECT column_name, data_type, data_precision, data_scale FROM user_tab_columns "
                            "WHERE table_name = :t ORDER BY column_id", t=t)
                cols = [(n, _logical_from_dict(d, p, s)) for n, d, p, s in cur.fetchall()]
            if not cols:
                raise KeyError(f"テーブルが見つかりません: {t}")
            self._coltypes[t] = cols
        return self._coltypes[t]

    def primary_key(self, table: str) -> list[str]:
        t = _ident(table)
        if t not in self._pk:
            with self.conn.cursor() as cur:
                cur.execute("SELECT cc.column_name FROM user_constraints c "
                            "JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name "
                            "WHERE c.table_name = :t AND c.constraint_type = 'P' ORDER BY cc.position", t=t)
                self._pk[t] = [r[0] for r in cur.fetchall()]
        return self._pk[t]

    # ------------------------------------------------------------ 環境制御
    def reset(self, scope: ResetScope) -> None:
        with self.conn.cursor() as cur:
            for t in scope.tables:
                stmt = f"DELETE FROM {_ident(t)}"
                self.log(stmt)
                cur.execute(stmt)
            for s, start in scope.sequences.items():
                stmt = f"ALTER SEQUENCE {_ident(s)} RESTART START WITH {int(start)}"
                self.log(stmt)
                cur.execute(stmt)
        self.commit()

    def _insert(self, cur, table: str, row: dict) -> int:
        cols, vals = self.coerce_row(table, row)
        stmt = (f"INSERT INTO {_ident(table)} ({', '.join(cols)}) "
                f"VALUES ({', '.join(f':{i + 1}' for i in range(len(cols)))})")
        self.log(stmt, vals)
        cur.execute(stmt, vals)
        return cur.rowcount

    def load(self, setup: Setup, fixture_rows: list[RowsSpec]) -> None:
        with self.conn.cursor() as cur:
            for spec in [*fixture_rows, *setup.rows]:
                for row in spec.data:
                    self._insert(cur, spec.table, row)
            for stmt in setup.sql.oracle:
                self.log(stmt)
                cur.execute(stmt)

    def set_clock(self, ts: dt.datetime | None) -> None:
        stmt = self.settings.clock_sql("oracle")
        if stmt:
            self.log(stmt, ts)
            with self.conn.cursor() as cur:
                cur.execute(stmt, ts=ts)

    # ------------------------------------------------------------ 実行
    def call(self, case: Case) -> CallResult:
        res = CallResult()
        cur = self.conn.cursor()
        if case.observe.messages:
            cur.callproc("DBMS_OUTPUT.ENABLE", [None])
        try:
            kind = case.target.kind
            if kind in ("function", "procedure"):
                self._call_subprogram(cur, case, res)
            elif kind == "trigger":
                res.rows_affected = self._fire(cur, case.fire)
            else:
                q = case.query if isinstance(case.query, str) else case.query.oracle
                self.log(q)
                cur.execute(q)
                res.result_sets["QUERY"] = self._fetch(cur)
        except oracledb.DatabaseError as e:
            res.error = self._error(e)
        if case.observe.messages:
            res.messages = self._dbms_output(cur)
        cur.close()
        return res

    def _var(self, cur, logical: str):
        t = base_type(logical)
        if t in ("NUMBER", "INTEGER", "FLOAT", "BOOLEAN"):
            return cur.var(oracledb.DB_TYPE_NUMBER, outconverter=lambda v: v if v is None else Decimal(str(v)))
        if t in ("VARCHAR", "CHAR"):
            return cur.var(str, 32767)
        return cur.var({
            "DATE": oracledb.DB_TYPE_DATE, "TIMESTAMP": oracledb.DB_TYPE_TIMESTAMP,
            "TIMESTAMPTZ": oracledb.DB_TYPE_TIMESTAMP_TZ, "CLOB": oracledb.DB_TYPE_CLOB,
            "BLOB": oracledb.DB_TYPE_BLOB, "REFCURSOR": oracledb.DB_TYPE_CURSOR,
        }[t])

    def _call_subprogram(self, cur, case: Case, res: CallResult) -> None:
        name = self.name(case.target.name)
        _ident(name)
        binds: dict[str, Any] = {}
        outs = []
        for i, a in enumerate(case.call.args):
            b = f"b{i}"
            if a.mode == "IN":
                binds[b] = self.coerce(a.value, a.type)
            else:
                var = self._var(cur, a.type)
                if a.mode == "INOUT":
                    var.setvalue(0, self.coerce(a.value, a.type))
                binds[b] = var
                outs.append((a.name.upper(), var, a.type))
        args = [(a.name, a.mode, a.type) for a in case.call.args]

        if case.target.kind == "function":
            rtype = case.call.returns.type
            if case.target.invoke_style == "select":
                if any(base_type(t) == "BOOLEAN" for t in (rtype, *(t for _, _, t in args))):
                    raise ValueError("BOOLEAN の引数・戻り値を持つ関数は SQL から呼べない(invoke_style: block を使う)")
                stmt = f"SELECT {name}{_arglist(args)} FROM DUAL"
                self.log(stmt, binds)
                cur.execute(stmt, binds)
                value = cur.fetchone()[0]
            else:
                rv = self._var(cur, rtype)
                binds["ret"] = rv
                stmt = build_plsql_call(name, args, rtype)
                self.log(stmt, {k: v for k, v in binds.items() if k != "ret"})
                cur.execute(stmt, binds)
                value = rv.getvalue()
            if base_type(rtype) == "REFCURSOR":
                res.result_sets["RETURN"] = self._fetch(value)
            else:
                res.return_value, res.return_type = self._read(value), rtype
        else:
            stmt = build_plsql_call(name, args)
            self.log(stmt, binds)
            cur.execute(stmt, binds)

        for aname, var, t in outs:
            v = var.getvalue()
            if base_type(t) == "REFCURSOR":
                res.result_sets[aname] = self._fetch(v)
            else:
                res.out_args[aname] = (self._read(v), t)

    @staticmethod
    def _read(v: Any) -> Any:
        return v.read() if hasattr(v, "read") else v

    def _fetch(self, c) -> ResultSet:
        if c is None:
            return ResultSet(columns=[], rows=[])
        rows = [tuple(self._read(v) for v in r) for r in c.fetchall()]
        cols = [ColumnMeta(d.name.upper(), _desc_type(d)) for d in c.description]
        return ResultSet(columns=cols, rows=rows)

    def _fire(self, cur, fire: Fire) -> int:
        table = _ident(fire.table)
        if fire.op == "insert":
            return sum(self._insert(cur, table, row) for row in fire.data)
        where_cols, where_vals = self.coerce_row(table, fire.where)
        conds = [f"{c} = :w{i}" if v is not None else f"{c} IS NULL"
                 for i, (c, v) in enumerate(zip(where_cols, where_vals))]
        binds = {f"w{i}": v for i, v in enumerate(where_vals) if v is not None}
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        if fire.op == "update":
            set_cols, set_vals = self.coerce_row(table, fire.set)
            sets = ", ".join(f"{c} = :s{i}" for i, c in enumerate(set_cols))
            binds.update({f"s{i}": v for i, v in enumerate(set_vals)})
            stmt = f"UPDATE {table} SET {sets}{where}"
        else:
            stmt = f"DELETE FROM {table}{where}"
        self.log(stmt, binds)
        cur.execute(stmt, binds)
        return cur.rowcount

    def _error(self, e: oracledb.DatabaseError) -> ErrorInfo:
        err = e.args[0]
        code = getattr(err, "full_code", None) or f"ORA-{err.code:05d}"
        native = str(err.message)
        first = native.splitlines()[0] if native else ""
        body = re.sub(r"^ORA-\d{5}:\s*", "", first)
        ctx: dict[str, Any] = {}
        m = re.search(r"\(([A-Z0-9_$#\"]+)\.([A-Z0-9_$#\"]+)\)", first)
        if m:
            ctx["constraint"] = m.group(2).strip('"')
        return ErrorInfo(logical_name=self.errors.oracle(code, native), native_code=code,
                         native_message=native, message=body, context=ctx)

    def _dbms_output(self, cur) -> list[str]:
        line, status = cur.var(str, 32767), cur.var(int)
        lines = []
        while True:
            cur.callproc("DBMS_OUTPUT.GET_LINE", (line, status))
            if status.getvalue() != 0:
                return lines
            lines.append(line.getvalue() or "")

    # ------------------------------------------------------------ 観測
    def snapshot(self, spec: SnapshotSpec) -> Snapshot:
        tables = [t.upper() for t in spec.tables]
        with self.conn.cursor() as cur:
            if spec.mode == "schema_wide":
                cur.execute("SELECT table_name FROM user_tables ORDER BY table_name")
                tables = [r[0] for r in cur.fetchall()]
            snap = Snapshot()
            for t in tables:
                cols = self.column_types(t)
                stmt = f"SELECT {', '.join(n for n, _ in cols)} FROM {_ident(t)}"
                self.log(stmt)
                cur.execute(stmt)
                rows = [tuple(self._read(v) for v in r) for r in cur.fetchall()]
                snap.tables[t] = TableData(columns=[ColumnMeta(n, ty) for n, ty in cols],
                                           rows=rows, key=self.primary_key(t))
        return snap

    # ------------------------------------------------------------ デプロイ
    def execute_script(self, sql_text: str) -> None:
        """`/` 単独行で区切られたスクリプトを順に実行する。コンパイルエラーは例外にする。"""
        chunks = re.split(r"^\s*/\s*$", sql_text, flags=re.M)
        with self.conn.cursor() as cur:
            for chunk in chunks:
                stmt = chunk.strip()
                body = re.sub(r"^\s*--.*$", "", stmt, flags=re.M).strip()
                if not body:
                    continue
                if not _PLSQL_HEAD.match(body):
                    stmt = body.rstrip(";")
                self.log(stmt)
                cur.execute(stmt)
                if cur.warning:
                    raise RuntimeError(f"コンパイルエラー: {cur.warning}\n{self._compile_errors()}\n--\n{stmt[:200]}")
        self.commit()

    def _compile_errors(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT name, type, line, position, text FROM user_errors ORDER BY name, sequence")
            return "\n".join(f"{n} {t} {l}:{p} {x}" for n, t, l, p, x in cur.fetchall())
