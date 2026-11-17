"""スキーマ・パッケージ・関数を DB に適用する(15.2 手順1)。

  python -m tests.tools.deploy [oracle|postgres|all]

settings.yaml の deploy.<dialect> ディレクトリ配下の *.sql を名前順に実行する。
"""
from __future__ import annotations

import sys
import time

from tests.core.loader import Catalog, Settings
from tests.core.runner import make_adapter


def deploy(dialect: str, catalog: Catalog, retries: int = 30) -> None:
    s = catalog.settings
    db = make_adapter(dialect, catalog)
    for i in range(retries):
        try:
            db.connect()
            break
        except Exception as e:  # DB 起動直後はリスナ未準備のことがある
            if i == retries - 1:
                raise
            print(f"[{dialect}] waiting for DB... ({e.__class__.__name__})")
            time.sleep(5)
    try:
        for path in sorted((s.root / s.raw["deploy"][dialect]).glob("*.sql")):
            print(f"[{dialect}] apply {path.relative_to(s.root)}")
            db.execute_script(path.read_text(encoding="utf-8"))
        print(f"[{dialect}] {db.version_info()}")
    finally:
        db.close()


def main(argv: list[str]) -> int:
    which = argv[0] if argv else "all"
    catalog = Catalog.load(Settings.load(), with_cases=False)
    for d in (["oracle", "postgres"] if which == "all" else [which]):
        deploy(d, catalog)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
