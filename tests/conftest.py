"""pytest 統合: CLI オプション・ケースのパラメトリック展開・レポート出力。

例:
  pytest --mode=record                    # Oracle で実行し golden/ を生成
  pytest --mode=verify --target=postgres  # PostgreSQL で実行し golden と照合
  pytest --mode=check-oracle              # Oracle 再実行で golden の再現性確認
  pytest --mode=diff                      # 両 DB で実行し直接比較
  pytest --mode=verify -m dialect -k CALC_TAX
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

import pytest

from tests.core.loader import Catalog, Settings
from tests.core.models import CaseOutcome
from tests.core.report import new_report_dir, summary_lines, write_reports
from tests.core.runner import MODES, Runner

_STATE: dict = {}


def pytest_addoption(parser):
    g = parser.getgroup("dualtest", "PL/SQL ⇔ PL/pgSQL 二刀流検証")
    g.addoption("--mode", choices=MODES, default=None, help="実行モード(省略時はケース実行を行わない)")
    g.addoption("--target", choices=["oracle", "postgres"], default="postgres", help="verify の実行 DB")
    g.addoption("--update-golden", action="store_true", help="record で既存ゴールデンの上書きを許可")
    g.addoption("--settings", default=None, help="settings.yaml のパス")
    g.addoption("--report-dir", default=None, help="レポート出力先(既定: reports/<時刻>-<mode>)")
    g.addoption("--sql-log", action="store_true", help="実行 SQL・バインド値をレポート先の sql.log に出力")


def _marker(name: str) -> str:
    return re.sub(r"\W", "_", name)


def pytest_configure(config):
    mode = config.getoption("--mode")
    if not mode:
        return
    settings = Settings.load(config.getoption("--settings"))
    catalog = Catalog.load(settings)
    runner = Runner(catalog, mode, config.getoption("--target"), config.getoption("--update-golden"))
    report_dir = Path(config.getoption("--report-dir") or new_report_dir(settings.path("reports"), mode))
    report_dir.mkdir(parents=True, exist_ok=True)
    if not config.option.xmlpath and not hasattr(config, "workerinput"):
        config.option.xmlpath = str(report_dir / "junit.xml")
    if config.getoption("--sql-log"):
        h = logging.FileHandler(report_dir / "sql.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg = logging.getLogger("dualtest.sql")
        lg.setLevel(logging.DEBUG)
        lg.addHandler(h)
    for c in catalog.cases:
        for t in c.tags:
            config.addinivalue_line("markers", f"{_marker(t)}: tag {t}")
    for p in ("high", "medium", "low"):
        config.addinivalue_line("markers", f"priority_{p}: priority {p}")
    _STATE.update(catalog=catalog, runner=runner, report_dir=report_dir, outcomes=[],
                  started_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def pytest_generate_tests(metafunc):
    if "case" not in metafunc.fixturenames:
        return
    catalog: Catalog | None = _STATE.get("catalog")
    cases = catalog.cases if catalog else []
    metafunc.parametrize("case", [
        pytest.param(c, id=c.id, marks=[getattr(pytest.mark, _marker(t)) for t in c.tags]
                     + [getattr(pytest.mark, f"priority_{c.priority}")])
        for c in cases
    ])


@pytest.fixture(scope="session")
def runner() -> Runner:
    return _STATE["runner"]


@pytest.fixture
def record_outcome():
    def _rec(o: CaseOutcome) -> None:
        _STATE["outcomes"].append(o)
    return _rec


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--mode"):
        for item in items:
            if "case" in getattr(item, "fixturenames", []):
                item.add_marker(pytest.mark.skip(reason="--mode が未指定のためケース実行をスキップ"))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    outcomes = _STATE.get("outcomes")
    if not outcomes:
        return
    runner: Runner = _STATE["runner"]
    meta = {"mode": runner.mode, "target": runner.target, "started_at": _STATE["started_at"]}
    out_dir = write_reports(outcomes, meta, _STATE["report_dir"], _STATE["catalog"].settings.path("ledger"))
    tr = terminalreporter
    tr.section("dual-db summary")
    for line in summary_lines(outcomes, meta):
        tr.write_line(line)
    tr.write_line(f"  reports: {out_dir}")
