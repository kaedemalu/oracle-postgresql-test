"""ケース・フィクスチャ・設定の読込(仕様 3.2 / 5章 / 14.4)。"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Case, RowsSpec, SuiteFile

ROOT = Path(__file__).resolve().parents[2]

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(obj: Any) -> Any:
    if isinstance(obj, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), obj)
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    return obj


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class Settings:
    raw: dict
    root: Path = ROOT

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        p = Path(path) if path else ROOT / "config" / "settings.yaml"
        return cls(raw=_expand_env(load_yaml(p)), root=p.resolve().parent.parent)

    def path(self, key: str) -> Path:
        return self.root / self.raw["paths"][key]

    def db(self, dialect: str) -> dict:
        return self.raw["databases"][dialect]

    @property
    def allowed_hosts(self) -> list[str]:
        return self.raw.get("allowed_hosts", [])

    def resolve_name(self, logical: str, dialect: str) -> str:
        """論理名 → DB ごとの実名(5.5)"""
        mapped = (self.raw.get("name_mapping") or {}).get(logical, {}).get(dialect)
        if mapped:
            return mapped
        rule = (self.raw.get("naming") or {}).get(dialect, "upper" if dialect == "oracle" else "lower")
        return logical.lower() if rule == "lower" else logical.upper()

    @property
    def reset(self) -> dict:
        return self.raw.get("reset") or {"tables": [], "sequences": {}}

    @property
    def golden_source(self) -> dict:
        """ゴールデンの記録元として想定する Oracle(version_prefix / nls)。空ならチェックしない"""
        return self.raw.get("golden_source") or {}

    def clock_sql(self, dialect: str) -> str | None:
        return (self.raw.get("clock") or {}).get(dialect)


# ---------------------------------------------------------------- ケース

def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_cases(cases_dir: Path) -> list[Case]:
    """cases/ 配下の YAML を全て読み、pydantic で検証する。ID 重複はエラー。"""
    cases: list[Case] = []
    seen: dict[str, str] = {}
    for path in sorted(cases_dir.rglob("*.y*ml")):
        path = path.resolve()
        suite = SuiteFile.model_validate(load_yaml(path) or {})
        defaults = dict(suite.defaults)
        observe_scope = defaults.pop("observe_scope", None)
        for raw in suite.cases:
            merged = _deep_merge(defaults, raw)
            merged.setdefault("suite", suite.suite)
            merged["source_file"] = str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
            snap = (merged.get("observe") or {}).get("snapshot")
            if isinstance(snap, dict) and not snap.get("tables") and observe_scope:
                snap["tables"] = list(observe_scope)
            try:
                case = Case.model_validate(merged)
            except Exception as e:  # pydantic ValidationError をファイル名付きで再送出
                raise ValueError(f"{path}: case {raw.get('id')!r} が不正です\n{e}") from e
            if case.id in seen:
                raise ValueError(f"case id 重複: {case.id} ({seen[case.id]}, {path})")
            seen[case.id] = str(path)
            cases.append(case)
    return cases


# ---------------------------------------------------------------- フィクスチャ

NULL_TOKEN = r"\N"
EMPTY_TOKEN = "<EMPTY>"


def _fixture_files(fixtures_dir: Path, name: str) -> list[Path]:
    for ext in (".yaml", ".yml", ".json"):
        p = fixtures_dir / f"{name}{ext}"
        if p.exists():
            return [p]
    d = fixtures_dir / name
    if d.is_dir():
        return sorted(d.glob("*.csv"))
    raise FileNotFoundError(f"fixture が見つかりません: {name}")


def load_fixture(fixtures_dir: Path, name: str) -> list[RowsSpec]:
    """
    形式:
      - <name>.yaml / .json : {tables: [{table: T, rows: [{COL: v}]}]}(記述順に投入)
      - <name>/<NN_TABLE>.csv: ヘッダ行=列名。空セル/`\\N` は NULL、`<EMPTY>` は空文字
    """
    out: list[RowsSpec] = []
    for p in _fixture_files(fixtures_dir, name):
        if p.suffix == ".csv":
            table = re.sub(r"^\d+_", "", p.stem).upper()
            with open(p, encoding="utf-8", newline="") as f:
                rows = []
                for r in csv.DictReader(f):
                    rows.append({k: (None if v in ("", NULL_TOKEN) else "" if v == EMPTY_TOKEN else v)
                                 for k, v in r.items()})
            out.append(RowsSpec(table=table, data=rows))
        else:
            doc = load_yaml(p) if p.suffix != ".json" else json.loads(p.read_text(encoding="utf-8"))
            for t in doc.get("tables", []):
                out.append(RowsSpec(table=t["table"], data=t.get("rows", [])))
    return out


def case_hash(case: Case, fixtures_dir: Path) -> str:
    """ケース定義+フィクスチャ内容のハッシュ(8.3)。説明的な項目は除外する。"""
    h = hashlib.sha256()
    body = case.model_dump(mode="json", by_alias=True,
                           exclude={"title", "tags", "priority", "expect", "skip", "source_file"})
    h.update(json.dumps(body, sort_keys=True, ensure_ascii=False).encode())
    for name in case.setup.fixtures:
        for p in _fixture_files(fixtures_dir, name):
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


@dataclass
class Catalog:
    """settings・ケース・各種ルールの読込結果"""
    settings: Settings
    cases: list[Case] = field(default_factory=list)
    error_map: dict = field(default_factory=dict)
    normalize: dict = field(default_factory=dict)
    tolerances: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, settings: Settings, with_cases: bool = True) -> "Catalog":
        tol = load_yaml(settings.path("tolerances")) or {}
        return cls(
            settings=settings,
            cases=load_cases(settings.path("cases")) if with_cases else [],
            error_map=load_yaml(settings.path("error_map")) or {},
            normalize=load_yaml(settings.path("normalize")) or {},
            tolerances=tol.get("tolerances") or [],
        )
