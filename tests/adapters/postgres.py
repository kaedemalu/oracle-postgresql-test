"""PostgreSQL アダプタ(仕様 6.3)。psycopg 3。"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

import psycopg
from psycopg import sql as psql

from ..core.models import (
    CallResult, Case, ColumnMeta, ErrorInfo, Fire, ResetScope, ResultSet, RowsSpec, Setup, Snapshot,
    SnapshotSpec, TableData, base_type,
)
from .base import AdapterBase

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$")

# 論理型 → 引数のキャスト型(関数の引数型と異なる場合はケースの pg_type で上書き)
PG_CAST = {
    "NUMBER": "numeric", "INTEGER": "integer", "FLOAT": "double precision",
    "VARCHAR": "varchar", "CHAR": "bpchar", "CLOB": "text",
    "DATE": "timestamp", "TIMESTAMP": "timestamp", "TIMESTAMPTZ": "timestamptz",
    "BOOLEAN": "boolean", "BLOB": "bytea", "REFCURSOR": "refcursor",
}

_OID_TYPES = {
    16: "BOOLEAN", 17: "BLOB", 20: "INTEGER", 21: "INTEGER", 23: "INTEGER", 25: "VARCHAR",
    1043: "VARCHAR", 1042: "CHAR", 700: "FLOAT", 701: "FLOAT", 1082: "DATE", 1114: "TIMESTAMP",
    1184: "TIMESTAMPTZ", 1790: "REFCURSOR",
}


def _ident(name: str) -> psql.Composable:
    if not _IDENT.match(name):
        raise ValueError(f"不正な識別子: {name!r}")
    return psql.SQL(".").join(psql.Identifier(p.lower()) for p in name.split("."))


def _logical_from_format(fmt: str) -> str:
    f = fmt.lower()
    if f.startswith("numeric"):
        m = re.match(r"numeric\((\d+),(\d+)\)", f)
        return "INTEGER" if m and m.group(2) == "0" else "NUMBER"
    if f in ("integer", "bigint", "smallint"):
        return "INTEGER"
    if f in ("double precision", "real"):
        return "FLOAT"
    if f.startswith(("character varying", "text", "varchar")):
        return "VARCHAR"
    if f.startswith(("character", "bpchar")):
        return "CHAR"
    if f == "date":
        return "DATE"
    if f.startswith("timestamp"):
        return "TIMESTAMPTZ" if "with time zone" in f else "TIMESTAMP"
    if f == "boolean":
        return "BOOLEAN"
    if f == "bytea":
        return "BLOB"
    return f.upper()


def _desc_type(d) -> str:
    if d.type_code == 1700:
        return "INTEGER" if d.scale == 0 else "NUMBER"
    return _OID_TYPES.get(d.type_code, str(d.type_code))


class PostgresAdapter(AdapterBase):
    dialect = "postgres"
    conn: psycopg.Connection

    # ------------------------------------------------------------ 接続
    def connect(self) -> None:
        c = self.cfg
        self.conn = psycopg.connect(host=c["host"], port=int(c["port"]), dbname=c["dbname"],
                                    user=c["user"], password=c["password"], autocommit=False)
        self._notices: list[str] = []
        self.conn.add_notice_handler(lambda d: self._notices.append(d.message_primary or ""))
        with self.conn.cursor() as cur:
            for k, v in (c.get("session") or {}).items():
                self.log("set_config", (k, v))
                cur.execute("SELECT set_config(%s, %s, false)", (k, str(v)))
        self.conn.commit()

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
            cur.execute("SELECT current_setting('server_version'), current_setting('TimeZone'), "
                        "current_setting('DateStyle'), current_setting('search_path'), "
                        "current_setting('server_encoding'), "
                        "(SELECT datcollate FROM pg_database WHERE datname = current_database())")
            ver, tz, ds, sp, enc, coll = cur.fetchone()
        return {"dialect": "postgres", "version": ver,
                "settings": {"TimeZone": tz, "DateStyle": ds, "search_path": sp,
                             "server_encoding": enc, "lc_collate": coll}}

    # ------------------------------------------------------------ メタ情報
    def column_types(self, table: str) -> list[tuple[str, str]]:
        t = table.upper()
        if t not in self._coltypes:
            _ident(t)
            with self.conn.cursor() as cur:
                cur.execute("SELECT upper(a.attname), format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                            "WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped "
                            "ORDER BY a.attnum", (t.lower(),))
                cols = [(n, _logical_from_format(f)) for n, f in cur.fetchall()]
            if not cols:
                raise KeyError(f"テーブルが見つかりません: {t}")
            self._coltypes[t] = cols
        return self._coltypes[t]

    def primary_key(self, table: str) -> list[str]:
        t = table.upper()
        if t not in self._pk:
            with self.conn.cursor() as cur:
                cur.execute("SELECT upper(a.attname) FROM pg_index i "
                            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                            "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary "
                            "ORDER BY array_position(i.indkey::int2[], a.attnum)", (t.lower(),))
                self._pk[t] = [r[0] for r in cur.fetchall()]
        return self._pk[t]

    # ------------------------------------------------------------ 環境制御
    def _exec(self, cur, stmt: psql.Composable | str, params: Any = None) -> None:
        self.log(stmt.as_string(self.conn) if isinstance(stmt, psql.Composable) else stmt, params)
        cur.execute(stmt, params)

    def reset(self, scope: ResetScope) -> None:
        with self.conn.cursor() as cur:
            for t in scope.tables:
                self._exec(cur, psql.SQL("DELETE FROM {}").format(_ident(t)))
            for s, start in scope.sequences.items():
                self._exec(cur, psql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(_ident(s), psql.Literal(int(start))))
        self.commit()

    def _insert(self, cur, table: str, row: dict) -> int:
        cols, vals = self.coerce_row(table, row)
        stmt = psql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            _ident(table), psql.SQL(", ").join(_ident(c) for c in cols),
            psql.SQL(", ").join(psql.Placeholder() * len(cols)))
        self._exec(cur, stmt, vals)
        return cur.rowcount

    def load(self, setup: Setup, fixture_rows: list[RowsSpec]) -> None:
        with self.conn.cursor() as cur:
            for spec in [*fixture_rows, *setup.rows]:
                for row in spec.data:
                    self._insert(cur, spec.table, row)
            for stmt in setup.sql.postgres:
                self._exec(cur, stmt)

    def set_clock(self, ts: dt.datetime | None) -> None:
        stmt = self.settings.clock_sql("postgres")
        if stmt:
            with self.conn.cursor() as cur:
                self._exec(cur, stmt, {"ts": ts})

    # ------------------------------------------------------------ 実行
    def call(self, case: Case) -> CallResult:
        res = CallResult()
        self._notices.clear()
        with self.conn.cursor() as cur:
            # 6.5: 例外でトランザクションが中断されるため、セーブポイントへ戻してから観測する
            self._exec(cur, "SAVEPOINT dualtest_call")
            try:
                kind = case.target.kind
                if kind in ("function", "procedure"):
                    self._call_subprogram(cur, case, res)
                elif kind == "trigger":
                    res.rows_affected = self._fire(cur, case.fire)
                else:
                    q = case.query if isinstance(case.query, str) else case.query.postgres
                    self._exec(cur, q)
                    res.result_sets["QUERY"] = self._fetch(cur)
                self._exec(cur, "RELEASE SAVEPOINT dualtest_call")
            except psycopg.Error as e:
                res.error = self._error(e)
                self._exec(cur, "ROLLBACK TO SAVEPOINT dualtest_call")
        res.messages = list(self._notices)
        return res

    def _call_subprogram(self, cur, case: Case, res: CallResult) -> None:
        name = self.name(case.target.name)
        parts, params, outs = [], [], []
        for a in case.call.args:
            cast = a.pg_type or PG_CAST[base_type(a.type)]
            parts.append(psql.SQL("{} => %s::{}").format(psql.Identifier(a.name.lower()), psql.SQL(cast)))
            params.append(None if a.mode == "OUT" else self.coerce(a.value, a.type))
            if a.mode != "IN":
                outs.append((a.name.upper(), a.type))
        call_expr = psql.SQL("{}({})").format(_ident(name), psql.SQL(", ").join(parts))

        if case.target.kind == "function":
            rtype = case.call.returns.type
            self._exec(cur, psql.SQL("SELECT {}").format(call_expr), params)
            value = cur.fetchone()[0]
            if base_type(rtype) == "REFCURSOR":
                res.result_sets["RETURN"] = self._fetch_portal(cur, value)
            else:
                res.return_value, res.return_type = value, rtype
            return

        self._exec(cur, psql.SQL("CALL {}").format(call_expr), params)
        if not outs:
            return
        row = cur.fetchone()
        by_name = {d.name.upper(): v for d, v in zip(cur.description, row)}
        for aname, t in outs:
            v = by_name.get(aname)
            if base_type(t) == "REFCURSOR":
                res.result_sets[aname] = self._fetch_portal(cur, v)
            else:
                res.out_args[aname] = (v, t)

    def _fetch_portal(self, cur, portal: str | None) -> ResultSet:
        """refcursor を同一トランザクション内で FETCH ALL する"""
        if portal is None:
            return ResultSet(columns=[], rows=[])
        self._exec(cur, psql.SQL("FETCH ALL FROM {}").format(psql.Identifier(portal)))
        return self._fetch(cur)

    @staticmethod
    def _fetch(cur) -> ResultSet:
        rows = [tuple(r) for r in cur.fetchall()]
        cols = [ColumnMeta(d.name.upper(), _desc_type(d)) for d in cur.description or []]
        return ResultSet(columns=cols, rows=rows)

    def _fire(self, cur, fire: Fire) -> int:
        if fire.op == "insert":
            return sum(self._insert(cur, fire.table, row) for row in fire.data)
        where_cols, where_vals = self.coerce_row(fire.table, fire.where)
        conds, params = [], []
        for c, v in zip(where_cols, where_vals):
            if v is None:
                conds.append(psql.SQL("{} IS NULL").format(_ident(c)))
            else:
                conds.append(psql.SQL("{} = %s").format(_ident(c)))
                params.append(v)
        where = psql.SQL(" WHERE ") + psql.SQL(" AND ").join(conds) if conds else psql.SQL("")
        if fire.op == "update":
            set_cols, set_vals = self.coerce_row(fire.table, fire.set)
            sets = psql.SQL(", ").join(psql.SQL("{} = %s").format(_ident(c)) for c in set_cols)
            stmt = psql.SQL("UPDATE {} SET {}").format(_ident(fire.table), sets) + where
            params = [*set_vals, *params]
        else:
            stmt = psql.SQL("DELETE FROM {}").format(_ident(fire.table)) + where
        self._exec(cur, stmt, params)
        return cur.rowcount

    def _error(self, e: psycopg.Error) -> ErrorInfo:
        sqlstate = e.sqlstate or "?????"
        d = e.diag
        msg = d.message_primary or str(e)
        ctx = {k: v for k, v in {
            "constraint": d.constraint_name.upper() if d.constraint_name else None,
            "table": d.table_name.upper() if d.table_name else None,
            "detail": d.message_detail,
        }.items() if v}
        return ErrorInfo(logical_name=self.errors.postgres(sqlstate, msg), native_code=sqlstate,
                         native_message=msg, message=msg, context=ctx)

    # ------------------------------------------------------------ 観測
    def snapshot(self, spec: SnapshotSpec) -> Snapshot:
        tables = [t.upper() for t in spec.tables]
        with self.conn.cursor() as cur:
            if spec.mode == "schema_wide":
                cur.execute("SELECT upper(tablename) FROM pg_tables WHERE schemaname = current_schema() "
                            "ORDER BY 1")
                tables = [r[0] for r in cur.fetchall()]
            snap = Snapshot()
            for t in tables:
                cols = self.column_types(t)
                stmt = psql.SQL("SELECT {} FROM {}").format(
                    psql.SQL(", ").join(_ident(n) for n, _ in cols), _ident(t))
                self._exec(cur, stmt)
                snap.tables[t] = TableData(columns=[ColumnMeta(n, ty) for n, ty in cols],
                                           rows=[tuple(r) for r in cur.fetchall()], key=self.primary_key(t))
        return snap

    # ------------------------------------------------------------ デプロイ
    def execute_script(self, sql_text: str) -> None:
        with self.conn.cursor() as cur:
            self.log(sql_text[:200])
            cur.execute(sql_text)
        self.commit()
