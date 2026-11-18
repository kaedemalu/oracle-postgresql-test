"""ORA-xxxxx / SQLSTATE → 論理エラー名の変換(仕様 10章, config/error_map.yaml)。"""
from __future__ import annotations

import re


class ErrorMapper:
    def __init__(self, error_map: dict):
        self.rules = error_map.get("errors") or []
        ue = error_map.get("user_errors") or {}
        self.user_range = tuple(ue.get("oracle_range") or (20000, 20999))
        self.user_pg = ue.get("postgres") or []

    def oracle(self, native_code: str, message: str) -> str:
        m = re.fullmatch(r"ORA-(\d{5})", native_code)
        if m and self.user_range[0] <= int(m.group(1)) <= self.user_range[1]:
            return f"ERR_USER_{int(m.group(1))}"
        for r in self.rules:
            for entry in r.get("oracle") or []:
                if _match(entry, native_code, message):
                    return r["name"]
        return f"ERR_UNMAPPED:{native_code}"

    def postgres(self, sqlstate: str, message: str) -> str:
        for r in self.user_pg:
            m = re.search(r["code_regex"], sqlstate)
            if not m:
                continue
            groups = [sqlstate, *m.groups()]
            if r.get("message_regex"):
                mm = re.search(r["message_regex"], message or "")
                if not mm:
                    continue
                groups = [sqlstate, *mm.groups()]
            number = re.sub(r"\{(\d+)\}", lambda g: groups[int(g.group(1))] or "", r["number"])
            return f"ERR_USER_{int(number)}"
        for r in self.rules:
            for entry in r.get("postgres") or []:
                if _match(entry, sqlstate, message):
                    return r["name"]
        return f"ERR_UNMAPPED:{sqlstate}"


def _match(entry: str | dict, code: str, message: str) -> bool:
    """対応表の 1 要素(コード文字列 or {code, message_regex})との一致判定"""
    if isinstance(entry, dict):
        return entry["code"] == code and bool(re.search(entry.get("message_regex", ""), message or ""))
    return str(entry) == code
