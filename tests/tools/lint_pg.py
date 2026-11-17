"""plpgsql_check による PL/pgSQL 静的解析(3.3 補助ツール。本基盤とは独立に運用)。

  python -m tests.tools.lint_pg
"""
from __future__ import annotations

import sys

import psycopg

from tests.core.loader import Settings

QUERY = """
SELECT p.oid::regprocedure::text AS func, r.*
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  JOIN pg_language l ON l.oid = p.prolang AND l.lanname = 'plpgsql'
  CROSS JOIN LATERAL plpgsql_check_function_tb(p.oid,
        relid => COALESCE((SELECT t.tgrelid FROM pg_trigger t WHERE t.tgfoid = p.oid LIMIT 1), 0)) r
 WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'public')
 ORDER BY 1
"""


def main() -> int:
    c = Settings.load().db("postgres")
    with psycopg.connect(host=c["host"], port=int(c["port"]), dbname=c["dbname"],
                         user=c["user"], password=c["password"]) as conn:
        rows = conn.execute(QUERY).fetchall()
        cols = [d.name for d in conn.execute(QUERY).description]
    errors = 0
    for r in rows:
        rec = dict(zip(cols, r))
        print(f"{rec['level']:<12} {rec['func']} line {rec['lineno']}: {rec['message']}")
        errors += rec["level"] == "error"
    print(f"plpgsql_check: {len(rows)} issue(s), {errors} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
