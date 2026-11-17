"""レポート出力(仕様 15.3)。サマリ(コンソール/Markdown)・詳細(HTML)・機械可読(JSON)。

JUnit XML は pytest の --junitxml で出力する(conftest が既定の出力先を設定)。
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from .models import CaseOutcome, Verdict

ORDER = [v.value for v in Verdict]
OK = {Verdict.PASS, Verdict.PASS_WITH_WARNING, Verdict.TOLERATED}


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _rate(outcomes: list[CaseOutcome]) -> str:
    run = [o for o in outcomes if o.verdict != Verdict.SKIP]
    if not run:
        return "-"
    return f"{sum(o.verdict in OK for o in run) / len(run) * 100:.1f}% ({sum(o.verdict in OK for o in run)}/{len(run)})"


def summary_lines(outcomes: list[CaseOutcome], meta: dict) -> list[str]:
    counts = Counter(o.verdict.value for o in outcomes)
    lines = [f"mode={meta['mode']} target={meta['target']} cases={len(outcomes)}",
             "  " + "  ".join(f"{k}={counts.get(k, 0)}" for k in ORDER)]
    tol = Counter(d.tolerance_id for o in outcomes for d in o.tolerated)
    if tol:
        lines.append("  applied tolerances: " + ", ".join(f"{k}×{v}" for k, v in sorted(tol.items())))
    return lines


def _group_table(title: str, groups: dict[str, list[CaseOutcome]]) -> list[str]:
    out = [f"## {title}", "", "| 区分 | 合格率 | " + " | ".join(ORDER) + " |",
           "|---|---|" + "---|" * len(ORDER)]
    for g, os_ in sorted(groups.items()):
        c = Counter(o.verdict.value for o in os_)
        out.append(f"| {g} | {_rate(os_)} | " + " | ".join(str(c.get(k, 0)) for k in ORDER) + " |")
    return out + [""]


def write_reports(outcomes: list[CaseOutcome], meta: dict, report_dir: Path, ledger_path: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ledger = read_ledger(ledger_path)
    open_by_case = defaultdict(list)
    for r in ledger:
        if r.get("status") in ("open", "pending"):
            open_by_case[r["case_id"]].append(r["diff_id"])

    # --- JSON
    data = {
        "meta": meta,
        "summary": dict(Counter(o.verdict.value for o in outcomes)),
        "results": [{**asdict(o), "verdict": o.verdict.value,
                     "ledger_open": open_by_case.get(o.case_id, [])} for o in outcomes],
    }
    (report_dir / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                                             encoding="utf-8")

    # --- Markdown サマリ
    by_suite, by_tag = defaultdict(list), defaultdict(list)
    for o in outcomes:
        by_suite[o.suite].append(o)
        for t in o.tags or ["(none)"]:
            by_tag[t].append(o)
    md = [f"# テスト結果サマリ ({meta['mode']} / {meta['target']})", "",
          f"- 実行日時: {meta['started_at']}", f"- 合格率: {_rate(outcomes)}", ""]
    md += ["| 判定 | 件数 |", "|---|---|"] + [f"| {k} | {Counter(o.verdict.value for o in outcomes).get(k, 0)} |"
                                          for k in ORDER] + [""]
    md += _group_table("スイート別", by_suite) + _group_table("タグ別", by_tag)
    tol = Counter(d.tolerance_id for o in outcomes for d in o.tolerated)
    if tol:
        md += ["## 適用された許容差", "", "| 許容差 ID | 件数 | 対象ケース |", "|---|---|---|"]
        for tid, n in sorted(tol.items()):
            cases = sorted({o.case_id for o in outcomes for d in o.tolerated if d.tolerance_id == tid})
            md.append(f"| {tid} | {n} | {', '.join(cases)} |")
        md.append("")
    bad = [o for o in outcomes if o.verdict in (Verdict.FAIL, Verdict.ERROR)]
    if bad:
        md += ["## FAIL / ERROR", ""]
        for o in bad:
            ref = f"(台帳: {', '.join(open_by_case[o.case_id])})" if open_by_case.get(o.case_id) else "(台帳未登録)"
            md += [f"### {o.case_id} {o.verdict.value} {ref}", "", "```", o.render(), "```", ""]
    warn = [o for o in outcomes if o.warnings]
    if warn:
        md += ["## 警告", ""] + [f"- {o.case_id}: " + "; ".join(w.render() for w in o.warnings) for o in warn] + [""]
    (report_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")

    # --- HTML 詳細
    (report_dir / "report.html").write_text(_html(outcomes, meta), encoding="utf-8")
    return report_dir


def _html(outcomes: list[CaseOutcome], meta: dict) -> str:
    color = {"PASS": "#1a7f37", "PASS_WITH_WARNING": "#9a6700", "TOLERATED": "#0969da",
             "FAIL": "#cf222e", "ERROR": "#8250df", "SKIP": "#6e7781"}
    rows = []
    for o in outcomes:
        details = "".join(
            f"<tr class=d><td>{html.escape(d.path)}</td><td>{html.escape(str(d.expected))}</td>"
            f"<td>{html.escape(str(d.actual))}</td><td>{html.escape(d.tolerance_id or 'FAIL')}</td></tr>"
            for d in [*o.diffs, *o.tolerated])
        warns = "<br>".join(html.escape(w.render()) for w in o.warnings)
        rows.append(
            f"<tr><td><span class=b style='background:{color[o.verdict.value]}'>{o.verdict.value}</span></td>"
            f"<td>{html.escape(o.case_id)}</td><td>{html.escape(', '.join(o.tags))}</td>"
            f"<td>{html.escape(o.message)}{'<br>' if o.message and warns else ''}{warns}</td>"
            f"<td>{o.duration_sec:.2f}s</td></tr>"
            + (f"<tr><td></td><td colspan=4><table class=diff><tr><th>path</th><th>expected</th>"
               f"<th>actual</th><th>判定</th></tr>{details}</table></td></tr>" if details else ""))
    counts = Counter(o.verdict.value for o in outcomes)
    badges = " ".join(f"<span class=b style='background:{color[k]}'>{k} {counts.get(k, 0)}</span>" for k in ORDER)
    return f"""<!doctype html><html lang=ja><meta charset=utf-8>
<title>Dual DB Test Report</title>
<style>
body{{font:14px system-ui,sans-serif;margin:24px;color:#1f2328;background:#fff}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #d0d7de;padding:6px 8px;text-align:left;vertical-align:top}}
.b{{color:#fff;border-radius:4px;padding:2px 6px;font-size:12px;white-space:nowrap}}
.diff td,.diff th{{font-family:ui-monospace,monospace;font-size:12px;background:#f6f8fa}}
</style>
<h1>Dual DB Test Report</h1>
<p>mode=<b>{html.escape(meta['mode'])}</b> target=<b>{html.escape(meta['target'])}</b> started={html.escape(meta['started_at'])}</p>
<p>{badges}</p>
<table><tr><th>判定</th><th>ケース</th><th>タグ</th><th>メッセージ</th><th>時間</th></tr>{''.join(rows)}</table>
</html>"""


def new_report_dir(base: Path, mode: str) -> Path:
    return base / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{mode}"
