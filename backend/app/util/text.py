"""Text tokenisation and XML escaping.

Script-aware tokenising (the no-space-script handling in particular) is used by
the alignment code and is pure, so it lives here. Moved verbatim from app.py.
"""
import re


def _xml_escape(text):
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def _xml_unescape(text):
    return (
        text.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&")
    )


# ── Line-level alignment ────────────────────────────────────────────────
# Proportional splitting of a paragraph translation across lines breaks down
# when source and target languages reorder words (e.g. "you want to speak"
# → "quieres hablar" places the verb at the end). We instead:
#   1. Translate each source line individually — low-quality but gives us a
#      "semantic fingerprint" of what words belong to that line.
#   2. Align the full paragraph translation to those fingerprints via DP,
#      maximising word-overlap between each span and its anchor fingerprint.
# The displayed text still comes from the high-quality paragraph translation;
# the anchors are only used to decide where to cut it.

_WORD_RE = re.compile(r"[^\w']+", re.UNICODE)

# Scripts without spaces between words (CJK + Thai). For these, splitting on
# whitespace yields ~1 "word" for a whole paragraph, which used to collapse the
# per-line split (everything on one line, the rest blank). We segment these into
# character units instead so the fallback splitter has something to distribute.
_NO_SPACE_CHAR_RE = re.compile(
    r"[぀-ヿ"      # Hiragana + Katakana
    r"㐀-䶿"       # CJK Ext A
    r"一-鿿"       # CJK Unified
    r"豈-﫿"       # CJK Compatibility
    r"ｦ-ﾟ"       # Halfwidth Katakana
    r"฀-๿]"      # Thai
)


def _tokenize(text):
    if not text:
        return []
    return [tok for tok in _WORD_RE.split(text.lower()) if tok]


def _is_no_space_script(text):
    """True when the text is mostly a no-space script (CJK/Thai), so it should
    be segmented by character rather than by whitespace."""
    if not text:
        return False
    cjk = len(_NO_SPACE_CHAR_RE.findall(text))
    # If a large share of non-space characters are CJK/Thai, treat as no-space.
    non_space = sum(1 for ch in text if not ch.isspace())
    return non_space > 0 and (cjk / non_space) >= 0.3


def _segment_units(text):
    """Split text into display units for alignment: whitespace-delimited words
    for spaced scripts, or individual characters for no-space scripts (CJK/Thai)
    so per-line splitting has enough granularity to distribute across lines."""
    if not text:
        return []
    if _is_no_space_script(text):
        # Keep non-space characters as individual units (drop spaces).
        return [ch for ch in text if not ch.isspace()]
    return text.split()
