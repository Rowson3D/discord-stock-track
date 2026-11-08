"""Which product is this? Turn messy titles from different sources into one candidate key.

Priority: a **model number** (most specific, shared across stores and eBay titles), then a
**UPC**, then the **cleaned title**. "Sony WH-1000XM5 Wireless Headphones $248" and Best
Buy's model "WH1000XM5/B" both become `model:wh1000xm5`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)*")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Specs, not models: 128GB, 65in, 4K, 120Hz, 2TB, 1080p, 5000mAh, 2nd, 3pk…
_UNIT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:gb|tb|mb|hz|ghz|mhz|in|inch|w|mah|mm|cm|ft|k|p|fps|ms|mp|v|oz|lb|lbs|"
    r"pk|ct|x|th|nd|rd|st|gen|qt|l|ml|g|kg|m|pc|pcs|pack|count|ea)$"
)
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
_PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
_NOISE_RE = re.compile(
    r"(\+\s*free\s+(?:shipping|s/h|ship(?:ping)?\s+w/\s*\S+)|free\s+shipping|"
    r"\bfs\b|\bymmv\b|\bw/\s*code\b.*$|\bvia\s+\S+$|\bat\s+[a-z&.\- ]+$)",
    re.IGNORECASE,
)
_UPC_RE = re.compile(r"^\d{12,14}$")
STOP_WORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "for", "with", "w", "new", "sealed", "brand", "free",
        "shipping", "deal", "sale", "in", "of", "to", "on", "by", "edition", "version",
    }
)  # fmt: skip


def compact(text: str) -> str:
    return "".join(_TOKEN_RE.findall(text.lower()))


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def clean_title(title: str) -> str:
    text = _PRICE_RE.sub(" ", title)
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"\s*[|•–—]\s*.*$", "", text)  # "Product | Store" / "Product - extra"
    return re.sub(r"\s+", " ", text).strip(" -,:;")[:300] or title.strip()[:300]


def _is_model(word: str) -> bool:
    flat = compact(word)
    if not 3 <= len(flat) <= 24 or flat.isdigit() or flat.isalpha():
        return False
    if _UNIT_RE.match(flat) or _YEAR_RE.match(flat):
        return False
    letters = sum(ch.isalpha() for ch in flat)
    return letters >= 2 or len(flat) >= 5


def model_candidates(title: str) -> Iterator[str]:
    """Model-number-looking words, plus 'RTX 5090'-style pairs (an acronym and a number)."""
    words = [m.group(0) for m in _WORD_RE.finditer(_PRICE_RE.sub(" ", title))]
    for index, word in enumerate(words):
        if _is_model(word):
            yield word
        elif word.isalpha() and word.isupper() and 2 <= len(word) <= 5 and index + 1 < len(words):
            following = words[index + 1]
            if following.isdigit() and 3 <= len(following) <= 5 and not _YEAR_RE.match(following):
                yield f"{word} {following}"


def pick_model(title: str, explicit: str | None = None) -> str | None:
    if explicit and explicit.strip():
        # Store model numbers often carry a color/region suffix: "WH1000XM5/B".
        base = explicit.strip().split("/")[0].strip()
        if len(compact(base)) >= 3:
            return base
    found = list(model_candidates(title))
    if not found:
        words = tokens(title)
        if "lego" in words:  # LEGO sets are identified by their set number.
            for word in words:
                if word.isdigit() and 4 <= len(word) <= 6 and not _YEAR_RE.match(word):
                    return f"LEGO {word}"
        return None
    return max(found, key=lambda word: (len(compact(word)), -found.index(word)))


def normalize_upc(upc: str | None) -> str | None:
    digits = re.sub(r"\D", "", upc or "")
    return digits if _UPC_RE.match(digits) else None


def candidate_key(title: str, *, model: str | None = None, upc: str | None = None) -> str:
    chosen = pick_model(title, model)
    if chosen:
        return f"model:{compact(chosen)}"[:160]
    code = normalize_upc(upc)
    if code:
        return f"upc:{code}"
    words = [w for w in tokens(clean_title(title)) if w not in STOP_WORDS][:8]
    return ("title:" + "-".join(words))[:160]
