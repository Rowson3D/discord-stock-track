"""Decide whether a marketplace listing title is really the variant we're valuing.

Resale comps are only useful if they're the *same* item: U7-Pro must not match "U7 Pro Max",
a single unit must not be priced off a 5-pack, and "for parts" or "cover for U7 Pro" listings
must not count. Rules, in order:

1. The SKU phrase must appear (token-wise, or compacted: "U7PRO").
2. Reject when a longer catalog SKU matches (U7-Pro-Max, UCG-Max-NS, …) or the token after
   the SKU is a model modifier (UDM-Pro "SE", UNVR "Pro", …).
3. Reject parts/broken listings, accessories ("mount for …"), and box-only listings.
4. Detect lot size. Pack variants need a matching quantity; singles accept lots at a lower
   score (priced per unit).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Tokens that, right after a SKU phrase, mean a different model.
MODEL_MODIFIERS = frozenset(
    {
        "max", "xg", "xgs", "wall", "outdoor", "lite", "plus", "mesh", "se", "ultra", "mini",
        "flex", "industrial", "enterprise", "lr", "hd", "nano", "iw", "edge", "pro", "beast",
        "fiber", "instant", "ns", "long",
    }
)  # fmt: skip
PARTS_PHRASES = (
    "for parts",
    "parts only",
    "not working",
    "non working",
    "broken",
    "as is",
    "untested",
    "no power",
    "damaged",
    "faulty",
    "cracked",
    "bricked",
    "locked",
    "defective",
)
BOX_ONLY_PHRASES = ("box only", "empty box", "manual only", "packaging only")
ACCESSORY_WORDS = frozenset(
    {
        "mount", "bracket", "cover", "case", "skin", "adapter", "stand", "holder", "shelf",
        "plate", "cable", "psu", "injector", "faceplate", "kit", "antenna", "arm", "clip",
    }
)  # fmt: skip

_QTY_PATTERNS = (
    re.compile(r"\blot\s+of\s+(\d{1,2})\b"),
    re.compile(r"\bset\s+of\s+(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})\s*-?\s*(?:pack|pk|pcs|pieces|units|count)\b"),
    re.compile(r"\bqty\s*:?\s*(\d{1,2})\b"),
    re.compile(r"^\s*\(?(\d{1,2})\)?\s*x?\s+(?:ubiquiti|unifi)\b"),
    re.compile(r"\b(\d{1,2})\s*x\s+(?:ubiquiti|unifi|u[a-z0-9]*)\b"),
)

CONDITION_IDS = {
    1000: "new",
    1500: "open_box",
    1750: "open_box",
    2000: "refurbished",
    2010: "refurbished",
    2020: "refurbished",
    2030: "refurbished",
    2500: "refurbished",
    2750: "used",
    3000: "used",
    4000: "used",
    5000: "used",
    6000: "used",
    7000: "parts",
}


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _find(haystack: list[str], needle: list[str]) -> int | None:
    width = len(needle)
    for index in range(len(haystack) - width + 1):
        if haystack[index : index + width] == needle:
            return index
    return None


def condition_from_id(condition_id: int | str | None) -> str | None:
    try:
        return CONDITION_IDS.get(int(condition_id)) if condition_id is not None else None
    except (TypeError, ValueError):
        return None


def condition_from_title(title: str) -> str | None:
    text = " ".join(tokens(title))
    if any(phrase in text for phrase in PARTS_PHRASES):
        return "parts"
    if "open box" in text:
        return "open_box"
    if "refurb" in text:
        return "refurbished"
    if re.search(r"\b(used|pre owned|preowned)\b", text):
        return "used"
    if re.search(r"\b(new|sealed|nib|bnib|nisb)\b", text):
        return "new"
    return None


def detect_quantity(title: str) -> int:
    text = title.lower()
    for pattern in _QTY_PATTERNS:
        match = pattern.search(text)
        if match:
            quantity = int(match.group(1))
            if 2 <= quantity <= 50:
                return quantity
    return 1


def base_sku(sku: str, pack_size: int) -> str:
    """'U7-Pro-5' (5-pack) → 'U7-Pro'. Pack variants are matched on the base model + quantity."""
    suffix = f"-{pack_size}"
    if pack_size > 1 and sku.endswith(suffix):
        return sku[: -len(suffix)]
    return sku


@dataclass(frozen=True, slots=True)
class MatchResult:
    matched: bool
    score: float = 0.0
    quantity: int = 1
    condition: str | None = None
    reason: str | None = None


class VariantMatcher:
    def __init__(self, sku: str, pack_size: int, catalog_skus: Iterable[str]) -> None:
        self.sku = sku
        self.pack_size = max(1, pack_size)
        self.phrase = tokens(base_sku(sku, self.pack_size))
        self.compact = "".join(self.phrase)
        # Longer catalog SKUs that start with ours (U7-Pro → U7-Pro-Max), ignoring pack variants.
        self.longer: list[list[str]] = []
        for other in catalog_skus:
            other_tokens = tokens(other)
            extra = other_tokens[len(self.phrase) :]
            if (
                len(other_tokens) > len(self.phrase)
                and other_tokens[: len(self.phrase)] == self.phrase
                and not all(token.isdigit() for token in extra)
            ):
                self.longer.append(other_tokens)

    def _position(self, title_tokens: list[str]) -> tuple[int, int] | None:
        """(start, end) of the SKU in the title, by tokens or compacted form ("u7pro")."""
        start = _find(title_tokens, self.phrase)
        if start is not None:
            return start, start + len(self.phrase)
        for index, token in enumerate(title_tokens):
            if token == self.compact:
                return index, index + 1
        return None

    def match(self, title: str, condition_id: int | str | None = None) -> MatchResult:
        title_tokens = tokens(title)
        position = self._position(title_tokens)
        if position is None:
            return MatchResult(False, reason="sku not in title")
        start, end = position

        for longer in self.longer:
            if _find(title_tokens, longer) is not None:
                return MatchResult(False, reason=f"different model ({'-'.join(longer)})")
        if end < len(title_tokens) and title_tokens[end] in MODEL_MODIFIERS:
            return MatchResult(False, reason=f"different model (+{title_tokens[end]})")

        text = " ".join(title_tokens)
        condition = condition_from_id(condition_id) or condition_from_title(title)
        if condition == "parts":
            return MatchResult(False, condition=condition, reason="parts or not working")
        if any(phrase in text for phrase in BOX_ONLY_PHRASES):
            return MatchResult(False, reason="box only")
        before = title_tokens[:start]
        if "for" in before and ACCESSORY_WORDS.intersection(title_tokens):
            return MatchResult(False, reason="accessory")

        quantity = detect_quantity(title)
        if self.pack_size > 1:
            if quantity != self.pack_size:
                return MatchResult(False, quantity=quantity, reason="wrong pack size")
            return MatchResult(True, 1.0, quantity, condition)
        if quantity > 1:
            return MatchResult(True, 0.6, quantity, condition)  # lot: priced per unit
        return MatchResult(True, 1.0, 1, condition)
