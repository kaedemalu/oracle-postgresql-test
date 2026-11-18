"""ケース実行制御と実行モード(仕様 4章 / 6.5 / 7章)。"""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .compare import apply_tolerances, compare, judge, unmapped_errors, validate_tolerances
from .errors import ErrorMapper
from .loader import Catalog, case_hash, load_fixture
from .models import Case, CaseOutcome, Diff, ResetScope, Verdict
from .normalize import observe

MODES = ("record", "verify", "diff", "check-oracle")


def make_adapter(dialect: str, catalog: Catalog):
    mapper = ErrorMapper(catalog.error_map)
    if dialect == "oracle":
        from ..adapters.oracle import OracleAdapter
        return OracleAdapter(catalog.settings, mapper)
    if dialect == "postgres":
        from ..adapters.postgres import PostgresAdapter
        return PostgresAdapter(catalog.settings, mapper)
    raise ValueError(f"unknown dialect: {dialect}")


def check_golden_source(source: dict, expected: dict) -> str | None:
    """ゴールデンの記録元(source)が想定(settings.golden_source)と一致するか。不一致なら理由を返す"""
    problems = []
    prefix = expected.get("version_prefix")
    if prefix and not str(source.get("version", "")).startswith(str(prefix)):
        problems.append(f"version={source.get('version')!r}(想定 {prefix}*)")
    nls = source.get("nls") or {}
    for k, v in (expected.get("nls") or {}).items():
        if str(nls.get(k)) != str(v):
            problems.append(f"{k}={nls.get(k)!r}(想定 {v!r})")
    return ", ".join(problems) or None


@dataclass
class Execution:
    observed: dict
    warnings: list
    source: dict


class Runner:
    def __init__(self, catalog: Catalog, mode: str, target: str | None = None, update_golden: bool = False):
        if mode not in MODES:
            raise ValueError(f"--mode は {MODES} のいずれか")
        self.catalog = catalog
        self.mode = mode
        self.target = {"record": "oracle", "check-oracle": "oracle", "diff": "oracle+postgres"}.get(
            mode, target or "postgres")
        self.update_golden = update_golden
        self.golden_dir = catalog.settings.path("golden")
        self.fixtures_dir = catalog.settings.path("fixtures")
        validate_tolerances(catalog.tolerances)
        self._version_cache: dict[str, dict] = {}

    # ------------------------------------------------------------ 1 ケース実行
    def execute(self, case: Case, dialect: str) -> Execution:
        """reset → 時刻注入 → load → COMMIT → call → (COMMIT) → 観測 → ROLLBACK"""
        s = self.catalog.settings
        db = make_adapter(dialect, self.catalog)
        db.connect()  # ケース単位で新しいセッション(6.4)
        try:
            if dialect not in self._version_cache:
                self._version_cache[dialect] = db.version_info()
            reset = s.reset
            db.reset(ResetScope(tables=reset.get("tables", []), sequences=reset.get("sequences", {})))
            # 時刻は事前データ投入より前に注入する(投入で発火するトリガも固定時刻にするため)
            db.set_clock(dt.datetime.fromisoformat(case.clock) if case.clock else None)
            fixture_rows = [r for name in case.setup.fixtures for r in load_fixture(self.fixtures_dir, name)]
            db.load(case.setup, fixture_rows)
            db.commit()  # 事前データは確定させ、呼び出しの成否と切り離す

            result = db.call(case)
            if case.transaction.commit_after_call:
                db.commit()

            snapshot = None
            if case.observe.snapshot is not None:
                if case.transaction.observe_from == "other_session":
                    other = make_adapter(dialect, self.catalog)
                    other.connect()
                    try:
                        snapshot = other.snapshot(case.observe.snapshot)
                    finally:
                        other.close()
                else:
                    snapshot = db.snapshot(case.observe.snapshot)
        finally:
            db.close()
        observed, warnings = observe(case, result, snapshot, self.catalog.normalize)
        return Execution(observed, warnings, self._version_cache[dialect])

    # ------------------------------------------------------------ モード別
    def run(self, case: Case) -> CaseOutcome:
        out = CaseOutcome(case_id=case.id, suite=case.suite, tags=case.tags, priority=case.priority,
                          mode=self.mode, target=self.target, verdict=Verdict.PASS)
        started = time.monotonic()
        try:
            skip_reason = self._skip(case)
            if skip_reason:
                out.verdict, out.message = Verdict.SKIP, skip_reason
            elif self.mode == "record":
                self._record(case, out)
            elif self.mode == "diff":
                self._diff(case, out)
            else:
                self._verify(case, out, "oracle" if self.mode == "check-oracle" else self.target)
        except Exception as e:  # 接続断・定義不備など、テスト実行自体の失敗
            out.verdict, out.message = Verdict.ERROR, f"{type(e).__name__}: {e}"
        out.duration_sec = round(time.monotonic() - started, 3)
        return out

    def _skip(self, case: Case) -> str | None:
        dbs = {"record": ["oracle"], "check-oracle": ["oracle"], "diff": ["oracle", "postgres"]}.get(
            self.mode, [self.target])
        if any(getattr(case.skip, d) for d in dbs):
            return f"skip: {case.skip.reason}"
        return None

    def golden_path(self, case: Case) -> Path:
        return self.golden_dir / f"{case.id}.json"

    def _judge(self, case: Case, out: CaseOutcome, expected: dict, actual: Execution) -> None:
        bad = unmapped_errors(actual.observed) or unmapped_errors(expected)
        diffs = compare(expected, actual.observed, case.error.compare)
        out.diffs, out.tolerated = apply_tolerances(case.id, diffs, self.catalog.tolerances)
        out.warnings = actual.warnings
        out.verdict = judge(out.diffs, out.tolerated, out.warnings)
        if bad:  # 10.1: 対応表に無いエラーはマッピング追加が必要
            out.verdict, out.message = Verdict.ERROR, f"{bad}: error_map.yaml へのマッピング追加が必要"

    def _record(self, case: Case, out: CaseOutcome) -> None:
        ex = self.execute(case, "oracle")
        bad = unmapped_errors(ex.observed)
        if bad:
            out.verdict, out.message = Verdict.ERROR, f"{bad}: error_map.yaml へのマッピング追加が必要"
            return
        golden = {
            "case_id": case.id,
            "case_hash": case_hash(case, self.fixtures_dir),
            "recorded_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": ex.source,
            "observed": ex.observed,
        }
        path = self.golden_path(case)
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            diffs = compare(old["observed"], ex.observed, "name_and_message")
            stale = old.get("case_hash") != golden["case_hash"]
            if not diffs and not stale:
                out.message = "golden unchanged"
                return
            if not self.update_golden:
                out.diffs = diffs + ([Diff("case_hash", old.get("case_hash"), golden["case_hash"])] if stale else [])
                out.verdict = Verdict.FAIL
                out.message = "既存ゴールデンと差異あり。上書きするには --update-golden を指定"
                return
            out.message = "golden updated"
        else:
            out.message = "golden recorded"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(golden, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _verify(self, case: Case, out: CaseOutcome, dialect: str) -> None:
        path = self.golden_path(case)
        if not path.exists():
            out.verdict, out.message = Verdict.ERROR, f"ゴールデン未記録: {path.name}(--mode=record を先に実行)"
            return
        golden = json.loads(path.read_text(encoding="utf-8"))
        if golden.get("case_hash") != case_hash(case, self.fixtures_dir):
            out.verdict, out.message = Verdict.ERROR, "ゴールデン陳腐化: ケース/フィクスチャが変更されています(再 record が必要)"
            return
        bad_source = check_golden_source(golden.get("source") or {}, self.catalog.settings.golden_source)
        if bad_source:
            out.verdict, out.message = Verdict.ERROR, f"ゴールデンの記録元が想定と異なる: {bad_source}(想定環境で再 record が必要)"
            return
        self._judge(case, out, golden["observed"], self.execute(case, dialect))

    def _diff(self, case: Case, out: CaseOutcome) -> None:
        ora = self.execute(case, "oracle")
        pg = self.execute(case, "postgres")
        self._judge(case, out, ora.observed, pg)
