"""DB アダプタ共通インターフェース(仕様 6.1)と共通処理。"""
from __future__ import annotations

import base64
import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Protocol

from ..core.errors import ErrorMapper
from ..core.loader import Settings
from ..core.models import CallResult, Case, ResetScope, RowsSpec, Setup, Snapshot, SnapshotSpec, base_type

sql_log = logging.getLogger("dualtest.sql")


class UnsafeHostError(RuntimeError):
    pass


class DbAdapter(Protocol):
    dialect: str

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

    def reset(self, scope: ResetScope) -> None:
        """テスト用データを初期状態に戻す"""

    def load(self, setup: Setup, fixture_rows: list[RowsSpec]) -> None:
        """フィクスチャ・インラインデータ・補助SQLの投入"""

    def set_clock(self, ts: dt.datetime | None) -> None:
        """時刻注入(5.6)"""

    def call(self, case: Case) -> CallResult:
        """対象を実行し統一形式で返す(例外は捕捉して CallResult.error に格納)"""

    def snapshot(self, spec: SnapshotSpec) -> Snapshot:
        """事後テーブル状態の取得(正規化前の生データ)"""

    def version_info(self) -> dict: ...

    def execute_script(self, sql_text: str) -> None:
        """DDL スクリプトの適用(tools/deploy.py 用)"""


class AdapterBase:
    dialect = ""

    def __init__(self, settings: Settings, error_mapper: ErrorMapper):
        self.settings = settings
        self.cfg = settings.db(self.dialect)
        self.errors = error_mapper
        self._coltypes: dict[str, list[tuple[str, str]]] = {}
        self._pk: dict[str, list[str]] = {}
        host = str(self.cfg.get("host"))
        if host not in settings.allowed_hosts:
            raise UnsafeHostError(f"{self.dialect}: 接続先 {host!r} は allowed_hosts に含まれていません(15.4)")

    def log(self, sql: str, binds: Any = None) -> None:
        if sql_log.isEnabledFor(logging.DEBUG):
            sql_log.debug("[%s] %s | binds=%r", self.dialect, " ".join(sql.split()), binds)

    def name(self, logical: str) -> str:
        return self.settings.resolve_name(logical, self.dialect)

    # ------------------------------------------------------------ 値変換(論理型 → ドライバ値)

    def coerce(self, value: Any, logical: str | None) -> Any:
        t = base_type(logical)
        if value is None:
            return None
        if t in ("NUMBER", "FLOAT"):
            return Decimal(str(value))
        if t == "INTEGER":
            return int(value)
        if t in ("DATE", "TIMESTAMP", "TIMESTAMPTZ"):
            if isinstance(value, dt.datetime):
                return value
            if isinstance(value, dt.date):
                return dt.datetime(value.year, value.month, value.day)
            return dt.datetime.fromisoformat(str(value))
        if t == "BOOLEAN":
            b = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "y", "t")
            return self.bool_value(b)
        if t == "BLOB":
            return value if isinstance(value, (bytes, bytearray)) else base64.b64decode(value)
        if t in ("VARCHAR", "CHAR", "CLOB"):
            return self.string_value(str(value))
        return value

    def bool_value(self, b: bool) -> Any:
        return b

    def string_value(self, s: str) -> Any:
        return s

    def coerce_row(self, table: str, row: dict[str, Any]) -> tuple[list[str], list[Any]]:
        types = dict(self.column_types(table))
        cols, vals = [], []
        for k, v in row.items():
            ku = k.upper()
            if ku not in types:
                raise KeyError(f"{table}.{ku} は存在しません")
            cols.append(ku)
            vals.append(self.coerce(v, types[ku]))
        return cols, vals

    def column_types(self, table: str) -> list[tuple[str, str]]:
        raise NotImplementedError

    def primary_key(self, table: str) -> list[str]:
        raise NotImplementedError
