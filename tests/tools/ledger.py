"""差異台帳の操作(仕様 12章)。

  python -m tests.tools.ledger sync  reports/<run>/results.json   # 未登録の FAIL を open で追記
  python -m tests.tools.ledger check                              # リリース判定(open 残存・許容差紐付けを検査)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path

from tests.core.loader import Catalog, Settings
from tests.core.report import read_ledger

FIELDS = ["diff_id", "case_id", "detected_at", "category", "summary", "root_cause", "status", "resolution", "owner"]
CATEGORIES = {"conversion_bug", "accepted_difference", "test_issue", "spec_unknown"}
STATUSES = {"open", "fixed", "accepted", "pending"}


def sync(results_path: Path, ledger_path: Path) -> int:
    results = json.loads(results_path.read_text(encoding="utf-8"))["results"]
    rows = read_ledger(ledger_path)
    open_cases = {r["case_id"] for r in rows if r["status"] in ("open", "pending")}
    next_no = max([int(r["diff_id"].split("-")[-1]) for r in rows if r["diff_id"]] + [0]) + 1
    added = 0
    for r in results:
        if r["verdict"] != "FAIL" or r["case_id"] in open_cases:
            continue
        summary = "; ".join(f"{d['path']}: expected={d['expected']} actual={d['actual']}" for d in r["diffs"])[:500]
        rows.append({"diff_id": f"DIFF-{next_no:04d}", "case_id": r["case_id"],
                     "detected_at": dt.date.today().isoformat(), "category": "spec_unknown",
                     "summary": summary, "root_cause": "", "status": "open", "resolution": "", "owner": ""})
        next_no += 1
        added += 1
    with open(ledger_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"ledger: {added} 件追加 ({ledger_path})")
    return 0


def check(catalog: Catalog, ledger_path: Path) -> int:
    rows = read_ledger(ledger_path)
    tol_ids = {t["id"] for t in catalog.tolerances}
    problems = []
    for r in rows:
        if r["category"] not in CATEGORIES:
            problems.append(f"{r['diff_id']}: category が不正 ({r['category']})")
        if r["status"] not in STATUSES:
            problems.append(f"{r['diff_id']}: status が不正 ({r['status']})")
        if r["status"] == "open":
            problems.append(f"{r['diff_id']}: open のまま ({r['case_id']})")
        if r["category"] == "accepted_difference" and r["resolution"] not in tol_ids:
            problems.append(f"{r['diff_id']}: accepted_difference に許容差 ID が紐づいていない ({r['resolution']!r})")
    for p in problems:
        print("NG", p)
    print(f"ledger check: {len(rows)} 件 / 問題 {len(problems)} 件")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    settings = Settings.load()
    ledger_path = settings.path("ledger")
    if argv[:1] == ["sync"] and len(argv) == 2:
        return sync(Path(argv[1]), ledger_path)
    if argv[:1] == ["check"]:
        return check(Catalog.load(settings, with_cases=False), ledger_path)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
