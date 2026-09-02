"""Text normalisation helpers used by the matcher."""
import re
from difflib import SequenceMatcher

_STREET_ABBR = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "pike": "pk", "court": "ct", "place": "pl", "highway": "hwy",
    "parkway": "pkwy", "circle": "cir", "terrace": "ter", "square": "sq",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "saint": "st", "mount": "mt", "fort": "ft",
}
_NAME_CANON = {
    "rehabilitation": "rehab", "centre": "center", "ctr": "center",
    "healthcare": "healthcare", "nursing": "nursing",
}
_NAME_STOP = {"the", "of", "at", "and", "in", "on", "a", "an"}
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _tokens(s: str) -> list:
    s = (s or "").lower().replace("&", " and ").replace("-", " ").replace("/", " ")
    s = _PUNCT.sub(" ", s)
    return [t for t in s.split() if t]


def norm_street(s: str) -> str:
    toks = [_STREET_ABBR.get(t, t) for t in _tokens(s)]
    # "n w" -> "nw" style joins are rare; keep simple.
    return " ".join(toks)


def street_number(s: str) -> str:
    toks = _tokens(s)
    return toks[0] if toks and toks[0].isdigit() else ""


def is_po_box(s: str) -> bool:
    return bool(re.match(r"^\s*p\.?\s*o\.?\s*box", (s or "").lower()))


def norm_name_tokens(s: str) -> list:
    toks = _tokens(s)
    out = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "health" and i + 1 < len(toks) and toks[i + 1] == "care":
            out.append("healthcare")
            i += 2
            continue
        t = _NAME_CANON.get(t, t)
        if t not in _NAME_STOP:
            out.append(t)
        i += 1
    return out


def norm_name(s: str) -> str:
    return " ".join(norm_name_tokens(s))


def name_similarity(a: str, b: str) -> float:
    """0..1. Max of token Jaccard and sequence ratio on normalised names."""
    ta, tb = set(norm_name_tokens(a)), set(norm_name_tokens(b))
    if not ta or not tb:
        return 0.0
    jacc = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jacc, seq)


def norm_city(s: str) -> str:
    return " ".join(_tokens(s))


def norm_zip(s: str) -> str:
    return (s or "").strip()[:5]


def norm_phone(s: str) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def names_equivalent(a: str, b: str) -> bool:
    """True when two names differ only in punctuation/abbreviation/stop words."""
    return norm_name(a) == norm_name(b)
