"""Stage 0: which buyer sent a credit, read from its narration.

Order: a checksum-valid GSTIN in the narration, the PAN inside such a GSTIN (characters 3 to
12), the exact buyer name, then a unique close name match. Each path carries a confidence
that follows the match through. TReDS platform credits name the platform, not the buyer.
"""

import re
from dataclasses import dataclass

import polars as pl

from matsya_contracts import ids

ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
PATH_CONFIDENCE = {"gstin": 1.0, "pan": 0.95, "name": 0.9, "name_fuzzy": 0.75, "reference": 0.9, "platform": 0.6}
NAME_SIMILARITY = 0.8


def gstin_checksum(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        prod = ALNUM.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return ALNUM[(36 - total % 36) % 36]


def gstin_valid(g: str) -> bool:
    return bool(_GSTIN.fullmatch(g or "")) and gstin_checksum(g[:14]) == g[14]


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^A-Z0-9]+", ids.normalise_party(text)) if t}


@dataclass(frozen=True)
class Resolution:
    buyer_id: str | None
    path: str | None  # gstin | pan | name | name_fuzzy | platform | None
    confidence: float
    platform: bool


class CreditBuyerResolver:
    def __init__(self, buyers: pl.DataFrame, tenant_buyers: pl.DataFrame | None, tenant_id: str,
                 platforms: tuple[str, ...]):
        self.by_gstin = {g: b for g, b in zip(buyers["gstin"], buyers["buyer_id"]) if g}
        self.by_pan = {p: b for p, b in zip(buyers["pan"], buyers["buyer_id"]) if p}
        names: dict[str, set[str]] = {}
        for name, b in zip(buyers["name"], buyers["buyer_id"]):
            names.setdefault(ids.normalise_party(name), set()).add(b)
        if tenant_buyers is not None and tenant_buyers.height:
            mine = tenant_buyers.filter(pl.col("tenant_id") == tenant_id)
            for name, b in zip(mine["display_name"], mine["buyer_id"]):
                if name:
                    names.setdefault(ids.normalise_party(name), set()).add(b)
        self.names = sorted(names.items(), key=lambda kv: -len(kv[0]))  # longest names first
        self.platforms = tuple(p.upper() for p in platforms)

    def resolve(self, narration: str | None) -> Resolution:
        text = ids.normalise_party(narration or "")
        platform = any(p in text for p in self.platforms)
        for g in _GSTIN.findall(text):
            if gstin_valid(g):
                if g in self.by_gstin:
                    return Resolution(self.by_gstin[g], "gstin", 1.0, platform)
                if ids.pan_of_gstin(g) in self.by_pan:
                    return Resolution(self.by_pan[ids.pan_of_gstin(g)], "pan", 0.95, platform)
        exact = {b for name, bs in self.names if name and re.search(rf"(^|[^A-Z0-9]){re.escape(name)}($|[^A-Z0-9])", text)
                 for b in bs}
        if len(exact) == 1:
            return Resolution(next(iter(exact)), "name", PATH_CONFIDENCE["name"], platform)
        if not exact:
            have = _tokens(text)
            scored = []
            for name, bs in self.names:
                want = _tokens(name)
                if want:
                    sim = len(want & have) / len(want)
                    if sim >= NAME_SIMILARITY:
                        scored.append((sim, bs))
            if scored:
                best = max(s for s, _ in scored)
                top = set().union(*(bs for s, bs in scored if s == best))
                if len(top) == 1:
                    return Resolution(next(iter(top)), "name_fuzzy", PATH_CONFIDENCE["name_fuzzy"], platform)
        if platform:
            return Resolution(None, "platform", PATH_CONFIDENCE["platform"], True)
        return Resolution(None, None, 0.0, False)
