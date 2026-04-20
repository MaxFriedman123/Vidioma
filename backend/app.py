import os
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from flask import Flask, request, jsonify, g
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
import re
import json
import hashlib
import string
import random
import redis
import requests as http_requests
from datetime import datetime, timezone
from deep_translator import GoogleTranslator
from functools import lru_cache, wraps
from concurrent.futures import ThreadPoolExecutor
import jwt

app = Flask(__name__)
CORS(app)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_TTL_SECONDS = int(os.environ.get("REDIS_TTL_SECONDS", "86400"))

# ── Supabase Configuration ──────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")   # service role key (bypasses RLS)
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")     # fallback for HS256

# ── JWKS client for ES256 token verification ────────────────────────────
_jwks_client = None
if SUPABASE_URL:
    try:
        _jwks_client = jwt.PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
        print("JWKS client initialised for ES256 verification.")
    except Exception as e:
        print(f"Warning: JWKS init failed ({e}). Falling back to HS256.")

# ── Supabase REST helpers (bypasses broken supabase-py on Python 3.14) ──
SUPABASE_REST_URL = f"{SUPABASE_URL}/rest/v1" if SUPABASE_URL else ""
SUPABASE_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
} if SUPABASE_SERVICE_KEY else {}

supabase_ready = bool(SUPABASE_REST_URL and SUPABASE_HEADERS)
if supabase_ready:
    print("Supabase REST client configured.")
else:
    print("Warning: Supabase env vars missing. Progress features disabled.")


# ── Auth Middleware ──────────────────────────────────────────────────────
def _verify_token(token):
    """Verify a Supabase JWT. Tries JWKS (ES256) first, falls back to HS256."""
    # Try JWKS (ES256) first
    if _jwks_client:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
        )
    # Fallback to HS256
    if SUPABASE_JWT_SECRET:
        return jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    raise jwt.InvalidTokenError("No verification method configured")


def require_auth(f):
    """Decorator that verifies the Supabase JWT from the Authorization header.
    On success, sets g.user_id to the authenticated user's UUID.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]

        try:
            payload = _verify_token(token)
            g.user_id = payload["sub"]  # Supabase stores user UUID in 'sub'
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired"}), 401
        except jwt.InvalidTokenError as e:
            return jsonify({"error": f"Invalid token: {e}"}), 401

        return f(*args, **kwargs)
    return decorated


def optional_auth(f):
    """Like require_auth but doesn't block guests — just sets g.user_id or None."""
    @wraps(f)
    def decorated(*args, **kwargs):
        g.user_id = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
            try:
                payload = _verify_token(token)
                g.user_id = payload["sub"]
            except jwt.InvalidTokenError:
                pass  # guest fallback
        return f(*args, **kwargs)
    return decorated

try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()  # Test connection
except Exception as e:
    print(f"Warning: Redis connection failed ({e}). Caching will be disabled.")
    redis_client = None

@app.route('/')
def home():
    return "Vidioma Backend is Awake - Proxies Active!"

# Utility function to extract video ID from various YouTube URL formats
def extract_video_id(url):
    if 'v=' in url:
        return url.split('v=')[1].split('&')[0]
    elif 'youtu.be' in url:
        return url.split('/')[-1]
    return url

@lru_cache(maxsize=100)
def get_cached_transcript(video_id, from_lang):
    """
    Fetch a transcript for the requested language. Attempts a fast direct connection first,
    falling back to a rotating proxy if YouTube blocks the request.

    Returns:
        (transcript_data, is_correct_lang)

    - transcript_data: fetched transcript snippets
    - is_correct_lang:
        True  -> transcript is already in the requested language
        False -> transcript is from another language and should be manually translated
    """

    def attempt_fetch(api_instance):
        available_transcripts = api_instance.list(video_id)

        if not available_transcripts:
            raise ValueError("No transcripts are available for this video")

        requested = from_lang.lower()

        def base_lang(code):
            return code.lower().split("-")[0]

        def sort_key(transcript):
            # Prefer manual transcripts over auto-generated ones when possible
            return (
                getattr(transcript, "is_generated", False),
                transcript.language_code.lower()
            )

        # 1. Exact match: en -> en
        exact_match = next(
            (t for t in available_transcripts if t.language_code.lower() == requested),
            None
        )
        if exact_match:
            return exact_match.fetch(), True

        # 2. Regional/base-language match: en -> en-US, en-GB
        regional_matches = [
            t for t in available_transcripts
            if base_lang(t.language_code) == requested
        ]
        if regional_matches:
            best_match = sorted(regional_matches, key=sort_key)[0]
            return best_match.fetch(), True

        # 3. Try YouTube auto-translate from any translatable transcript
        translatable_candidates = sorted(
            [t for t in available_transcripts if getattr(t, "is_translatable", False)],
            key=sort_key
        )

        for transcript in translatable_candidates:
            try:
                return transcript.translate(from_lang).fetch(), True
            except Exception:
                continue

        # 4. Final fallback: return the best available source transcript
        fallback = sorted(available_transcripts, key=sort_key)[0]
        return fallback.fetch(), False


    # --- Main execution flow: Direct fetch first, Proxy fallback second ---

    # 1. ATTEMPT FAST DIRECT CONNECTION FIRST
    try:
        print(f"Attempting direct fetch for {video_id} in {from_lang}...")
        direct_api = YouTubeTranscriptApi()
        return attempt_fetch(direct_api)

    except Exception as e:
        print(f"Direct fetch failed, falling back to proxy: {e}")

        # 2. FALLBACK TO SLOW PROXY IF BLOCKED OR FAILED
        proxy_username = os.environ.get("WEBSHARE_USERNAME")
        proxy_password = os.environ.get("WEBSHARE_PASSWORD")

        if not proxy_username or not proxy_password:
            raise ValueError("Proxy credentials are not configured and direct fetch failed.")

        socks5_url = f"socks5://{proxy_username}-rotate:{proxy_password}@p.webshare.io:1080"
        proxy_config = GenericProxyConfig(http_url=socks5_url, https_url=socks5_url)

        proxy_api = YouTubeTranscriptApi(proxy_config=proxy_config)
        return attempt_fetch(proxy_api)

_SENTENCE_END_RE = re.compile(r'[.!?…]["\')\]]*\s*$')
# Paragraph sizing: translation quality benefits from context, but the unit
# should still be short enough that a fuzzy substring match stays meaningful.
_PARAGRAPH_TIME_GAP = 2.0
_MAX_PARAGRAPH_FRAGMENTS = 6
_MAX_PARAGRAPH_CHARS = 350


def _ends_sentence(text):
    return bool(_SENTENCE_END_RE.search(text))


def group_into_paragraphs(fragments):
    """
    Assign each fragment to a paragraph and return (fragments_with_paragraph, paragraph_texts).

    A paragraph groups a handful of consecutive fragments so translation sees
    enough context to produce a natural target-language rendering. The
    frontend still plays line by line — paragraphs only exist to give the
    translator room.

    Boundaries:
      - Force new paragraph when the gap between fragments exceeds
        _PARAGRAPH_TIME_GAP seconds (speaker pause).
      - Prefer to end at sentence-final punctuation (".!?…") once we already
        have enough content.
      - Hard cap at _MAX_PARAGRAPH_FRAGMENTS fragments or _MAX_PARAGRAPH_CHARS
        characters to keep paragraphs usefully small.
    """
    assigned = []
    paragraphs = []

    buf_texts = []
    buf_start_time = None
    buf_last_end = None
    paragraph_index = 0

    MIN_FRAGMENTS_FOR_SENTENCE_BREAK = 2

    def flush():
        if not buf_texts:
            return
        paragraph_text = re.sub(r"\s+", " ", " ".join(buf_texts)).strip()
        paragraphs.append(paragraph_text)

    for frag in fragments:
        text = frag["source"].strip()
        if not text:
            continue

        gap = 0.0
        if buf_last_end is not None:
            gap = max(0.0, frag["start"] - buf_last_end)

        current_chars = sum(len(t) for t in buf_texts) + max(0, len(buf_texts) - 1)

        break_before = (
            buf_texts
            and (
                gap >= _PARAGRAPH_TIME_GAP
                or len(buf_texts) >= _MAX_PARAGRAPH_FRAGMENTS
                or current_chars + len(text) + 1 >= _MAX_PARAGRAPH_CHARS
            )
        )

        if break_before:
            flush()
            paragraph_index += 1
            buf_texts = []
            buf_start_time = None
            buf_last_end = None

        if buf_start_time is None:
            buf_start_time = frag["start"]

        buf_texts.append(text)
        buf_last_end = frag["start"] + frag["duration"]

        assigned.append({
            "source": text,
            "start": frag["start"],
            "duration": frag["duration"],
            "paragraph": paragraph_index,
        })

        # Close paragraph opportunistically on sentence punctuation once we
        # already have enough content for the translator to work with.
        if _ends_sentence(text) and len(buf_texts) >= MIN_FRAGMENTS_FOR_SENTENCE_BREAK:
            flush()
            paragraph_index += 1
            buf_texts = []
            buf_start_time = None
            buf_last_end = None

    flush()
    return assigned, paragraphs


@lru_cache(maxsize=100)
def get_cached_processed_snippets(video_id, from_lang):
    """
    Fetch and clean the transcript, group fragments into paragraphs, and —
    when the transcript is from a different language than requested — also
    return paragraph-level translations into the source language.

    Returns (snippets, paragraphs) where each snippet is
    {source, start, duration, paragraph} and paragraphs is a list of strings
    aligned to the paragraph indices on the snippets.
    """
    source_transcript, is_correct_lang = get_cached_transcript(video_id, from_lang)

    cleaned_fragments = []

    for snippet in source_transcript:
        # Support both object-style and dict-style transcript entries
        if isinstance(snippet, dict):
            text = str(snippet.get("text", "")).strip()
            start = snippet.get("start", 0)
            duration = snippet.get("duration", 0)
        else:
            text = str(getattr(snippet, "text", "")).strip()
            start = getattr(snippet, "start", 0)
            duration = getattr(snippet, "duration", 0)

        text = re.sub(r"\[[^\]]*\]", "", text).strip()

        # Filter non-dialogue
        if not text:
            continue
        if text.startswith('[') or text.startswith('('):
            continue
        if not re.search(r'[^\W\d_]', text, re.UNICODE):
            continue

        cleaned_fragments.append({
            "source": text,
            "start": start,
            "duration": duration
        })

    assigned, paragraphs = group_into_paragraphs(cleaned_fragments)

    # Only translate when transcript is not already in requested language
    if not is_correct_lang and paragraphs:
        print(f"Manually translating {video_id} to {from_lang}")
        paragraphs = translate_paragraphs(paragraphs, from_lang)

    return assigned, paragraphs

def generate_cache_key(from_lang, to_lang, paragraphs, lines_by_paragraph=None):
    """Generate a cache key for paragraph + per-line translation results."""
    key_payload = {
        "from": from_lang.lower(),
        "to": to_lang.lower(),
        "paragraphs": paragraphs,
        "lines": lines_by_paragraph,  # None ↔ old shape, list ↔ new shape
    }
    key_raw = json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "translate_paragraphs:v4:" + hashlib.sha256(key_raw.encode("utf-8")).hexdigest()


_PARAGRAPH_SEPARATOR = "\n\n"
_TARGET_SENTENCE_RE = re.compile(r'(?<=[.!?…])\s+')

_TRANSLATORS_IMPORT_FAILED = False
_BING_COOLDOWN_UNTIL = 0.0  # epoch seconds — skip Bing while rate-limited
_BING_COOLDOWN_SECONDS = 120


def _ts_translate(engine, text, target_lang, source_lang, attempts=1):
    """Generic wrapper for a `translators` package engine. Returns None on failure."""
    global _TRANSLATORS_IMPORT_FAILED
    if _TRANSLATORS_IMPORT_FAILED:
        return None
    if not (text or "").strip():
        return ""
    try:
        import translators as ts
    except Exception as exc:
        print(f"translators import failed: {exc}; package disabled for this process")
        _TRANSLATORS_IMPORT_FAILED = True
        return None
    src = "auto" if (source_lang or "auto") == "auto" else source_lang
    last_exc = None
    for attempt in range(attempts):
        try:
            return ts.translate_text(
                text,
                translator=engine,
                from_language=src,
                to_language=target_lang,
            )
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                import time
                time.sleep(0.4 * (attempt + 1))
    return last_exc


def _bing_translate(text, target_lang, source_lang, attempts=3):
    """Translate via Bing.

    Bing handles elephant 'trunks' → 'trompas' where the free Google endpoint
    gives 'baúles' (luggage). Skips Bing entirely for ~2 min after a 429 to
    avoid hammering the rate-limiter. Returns None on failure.
    """
    global _BING_COOLDOWN_UNTIL
    import time
    if time.time() < _BING_COOLDOWN_UNTIL:
        return None
    result = _ts_translate("bing", text, target_lang, source_lang, attempts=attempts)
    if isinstance(result, Exception):
        msg = str(result)
        if "429" in msg or "Too Many Requests" in msg:
            _BING_COOLDOWN_UNTIL = time.time() + _BING_COOLDOWN_SECONDS
            print(f"Bing rate-limited (429); cooling down for {_BING_COOLDOWN_SECONDS}s")
        else:
            print(f"Bing translate failed ({msg}); falling back")
        return None
    return result


def _translate_text(text, target_lang, source_lang="auto"):
    """Quality-first cascade: Bing → Alibaba → Google.

    Bing is best but rate-limits Render's IP; Alibaba is a decent free fallback
    that doesn't return 'baúles' for elephant trunks; Google is the final safety
    net (worst quality for context-sensitive words).
    """
    if not (text or "").strip():
        return ""
    bing = _bing_translate(text, target_lang, source_lang)
    if bing:
        return bing
    alibaba = _ts_translate("alibaba", text, target_lang, source_lang)
    if alibaba and not isinstance(alibaba, Exception):
        return alibaba
    translator = GoogleTranslator(source=source_lang, target=target_lang)
    return translator.translate(text) or ""


def translate_paragraphs(paragraphs, target_lang, source_lang='auto'):
    """
    Translate all paragraphs in ONE call for maximum cross-paragraph context.
    This preserves pronoun resolution, consistent terminology, and register
    across the entire transcript — things that paragraph-by-paragraph or
    batched translation loses.

    Recovery cascade:
      1. Newline split — Google Translate usually preserves paragraph breaks.
      2. Sentence-boundary proportional alignment — for languages/inputs where
         newlines get mangled, map source-paragraph char ratios onto sentences
         in the translated text.
      3. Per-paragraph translate fallback — if full-text translation fails,
         translate each paragraph alone (degrades context but stays correct).
    """
    if not paragraphs:
        return []

    result = ["" for _ in paragraphs]
    non_empty_indices = [i for i, p in enumerate(paragraphs) if (p or "").strip()]
    if not non_empty_indices:
        return result

    # Join ALL paragraphs into one text with \n\n separators for maximum context.
    clean_paras = [(p or "").replace("\n", " ").strip() for p in paragraphs]
    joined = _PARAGRAPH_SEPARATOR.join(clean_paras)

    translated = None
    try:
        translated = _translate_text(joined, target_lang, source_lang) or ""
    except Exception as exc:
        print(f"Full-text translate failed: {exc}; falling back per-paragraph")

    chunks = _recover_chunks(translated, clean_paras) if translated else None
    if chunks is None:
        # Fallback: translate each paragraph alone.
        for idx in non_empty_indices:
            try:
                result[idx] = (_translate_text(clean_paras[idx], target_lang, source_lang) or clean_paras[idx]).strip()
            except Exception as exc:
                print(f"Per-paragraph fallback failed for para {idx}: {exc}")
                result[idx] = clean_paras[idx]
        return result

    # Assign recovered chunks to their positions.
    for idx, chunk in zip(range(len(paragraphs)), chunks):
        result[idx] = chunk.strip()
    return result


def _recover_chunks(translated, source_paragraphs):
    """
    Recover N paragraph-aligned chunks from a single translated string.
    Returns a list of length len(source_paragraphs), or None if recovery
    isn't confident enough (caller should fall back).
    """
    if not translated or not source_paragraphs:
        return None

    n = len(source_paragraphs)
    if n == 1:
        return [translated.strip()]

    # 1) Newline split — Google Translate usually preserves paragraph breaks.
    for sep in ("\n\n", "\n"):
        parts = [p.strip() for p in translated.split(sep) if p.strip()]
        if len(parts) == n:
            return parts

    # 2) Proportional sentence alignment.
    return _proportional_sentence_split(translated, source_paragraphs)


def _proportional_sentence_split(translated, source_paragraphs):
    """
    Split translated text into N chunks, snapping boundaries to sentence ends
    based on source-paragraph character proportions. A robust free alignment
    when the translator strips newlines.
    """
    sentences = [s.strip() for s in _TARGET_SENTENCE_RE.split(translated.strip()) if s.strip()]
    n = len(source_paragraphs)
    if len(sentences) < n:
        return None

    total_src = sum(len(p) for p in source_paragraphs) or 1
    cum_src = []
    running = 0
    for p in source_paragraphs:
        running += len(p)
        cum_src.append(running / total_src)

    total_tgt = sum(len(s) for s in sentences) or 1
    cum_tgt = []
    running = 0
    for s in sentences:
        running += len(s)
        cum_tgt.append(running / total_tgt)

    chunks = []
    prev_boundary = -1
    for target_ratio in cum_src[:-1]:
        search_start = prev_boundary + 1
        # Pick the sentence index whose cumulative ratio is closest to the
        # target, but never go backwards and always leave sentences for the
        # remaining paragraphs.
        best_idx = search_start
        best_diff = abs(cum_tgt[best_idx] - target_ratio)
        max_idx = len(sentences) - (n - len(chunks))  # reserve one per remaining para
        for i in range(search_start, max_idx + 1):
            diff = abs(cum_tgt[i] - target_ratio)
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        chunk = " ".join(sentences[prev_boundary + 1 : best_idx + 1])
        if not chunk:
            return None
        chunks.append(chunk)
        prev_boundary = best_idx

    last_chunk = " ".join(sentences[prev_boundary + 1 :])
    if not last_chunk:
        return None
    chunks.append(last_chunk)
    return chunks


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


def _tokenize(text):
    if not text:
        return []
    return [tok for tok in _WORD_RE.split(text.lower()) if tok]


def align_lines_to_paragraph(paragraph_translation, line_anchors):
    """
    Partition paragraph_translation into contiguous word spans, one per line,
    maximising word-overlap between each span and its anchor fingerprint.

    paragraph_translation: str — the quality translation of the paragraph.
    line_anchors: list of str — solo translation of each source line (can be
                                 rough; used only as a content fingerprint).

    Returns: list of str of length len(line_anchors). Each string is a slice
    of paragraph_translation. Falls back to word-count proportion if the
    paragraph is empty or anchors are entirely unhelpful.
    """
    n = len(line_anchors)
    if n == 0:
        return []
    if not paragraph_translation or not paragraph_translation.strip():
        return [""] * n
    if n == 1:
        return [paragraph_translation.strip()]

    words = paragraph_translation.split()
    m = len(words)
    if m < n:
        # Fewer paragraph words than lines — can't give each line its own word.
        # Fall back to proportional split so we at least return n chunks.
        return _proportional_word_split(paragraph_translation, line_anchors)

    word_tokens = [_tokenize(w) for w in words]  # per-word lowercased tokens
    anchor_sets = [set(_tokenize(a)) for a in line_anchors]

    # dp[j][i] = best score using first i words to cover first j lines.
    # Each line must receive at least one word, so i >= j.
    NEG_INF = float("-inf")
    dp = [[NEG_INF] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0

    for j in range(1, n + 1):
        anchor = anchor_sets[j - 1]
        # Leave room for remaining lines to each get at least one word.
        min_i = j
        max_i = m - (n - j)
        for i in range(min_i, max_i + 1):
            best_score = NEG_INF
            best_k = j - 1
            # Previous boundary k: span for line j is words[k:i].
            for k in range(j - 1, i):
                if dp[j - 1][k] == NEG_INF:
                    continue
                # Count anchor tokens that appear in this span.
                span_tokens = set()
                for w in range(k, i):
                    span_tokens.update(word_tokens[w])
                overlap = len(span_tokens & anchor) if anchor else 0
                # Normalise by anchor size so long anchors don't dominate,
                # and add a tiny prior that discourages pathological 1-word
                # assignments when anchors give no signal.
                if anchor:
                    score_term = overlap / max(len(anchor), 1)
                else:
                    # Anchor empty — prefer proportional share of remaining
                    # words, so lines aren't starved.
                    share = (i - k) / max(m, 1)
                    score_term = share * 0.1  # tiny, only a tiebreaker
                total = dp[j - 1][k] + score_term
                if total > best_score:
                    best_score = total
                    best_k = k
            dp[j][i] = best_score
            back[j][i] = best_k

    # Recover split points.
    splits = [m]
    j = n
    i = m
    while j > 0:
        k = back[j][i]
        splits.append(k)
        i = k
        j -= 1
    splits.reverse()  # [0, s_1, s_2, ..., s_{n-1}, m]

    chunks = []
    for j in range(n):
        chunks.append(" ".join(words[splits[j] : splits[j + 1]]).strip())
    return chunks


def _proportional_word_split(paragraph_translation, line_anchors):
    """Fallback: divide paragraph words across lines by count (no semantics)."""
    words = paragraph_translation.split()
    n = len(line_anchors)
    if n == 0 or not words:
        return [""] * n
    base = len(words) // n
    extra = len(words) % n
    chunks = []
    idx = 0
    for j in range(n):
        size = base + (1 if j < extra else 0)
        size = max(1, size)
        chunks.append(" ".join(words[idx : idx + size]))
        idx += size
    return chunks


def _translate_line_anchors(lines_flat, target_lang, source_lang, max_workers=8):
    """
    Translate a flat list of source lines individually, in parallel.
    Returns a list of the same length; empty strings map to empty translations.
    Failures fall back to the source line so alignment still gets *some*
    signal (and display won't break).
    """
    if not lines_flat:
        return []

    # Anchors are only used for DP alignment fingerprinting — Google's output
    # is good enough as a per-line signal and avoids hammering Bing with 8
    # parallel calls (which triggers 429 rate limits on Render's shared IP).
    def _one(text):
        clean = (text or "").replace("\n", " ").strip()
        if not clean:
            return ""
        try:
            translator = GoogleTranslator(source=source_lang, target=target_lang)
            return (translator.translate(clean) or clean).strip()
        except Exception as exc:
            print(f"Line anchor translate failed: {exc}")
            return clean

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_one, lines_flat))


def translate_with_alignment(paragraphs, lines_by_paragraph, target_lang, source_lang='auto'):
    """
    Translate paragraphs (with full cross-paragraph context) AND produce
    per-line aligned chunks via anchor DP alignment.

    Returns (translated_paragraphs, translated_lines_by_paragraph).
    """
    translated_paragraphs = translate_paragraphs(paragraphs, target_lang, source_lang)

    # Flatten lines for parallel anchor translation.
    flat_lines = []
    offsets = []  # cumulative index where each paragraph's lines start
    for group in lines_by_paragraph:
        offsets.append(len(flat_lines))
        flat_lines.extend(group or [])
    offsets.append(len(flat_lines))

    flat_anchors = _translate_line_anchors(flat_lines, target_lang, source_lang)

    translated_lines = []
    for p_idx, group in enumerate(lines_by_paragraph):
        start, end = offsets[p_idx], offsets[p_idx + 1]
        anchors = flat_anchors[start:end]
        paragraph_text = translated_paragraphs[p_idx] if p_idx < len(translated_paragraphs) else ""
        translated_lines.append(align_lines_to_paragraph(paragraph_text, anchors))
    return translated_paragraphs, translated_lines


@app.route('/api/transcript', methods=['POST'])
def get_transcript():
    try:
        data = request.get_json()
        video_url = data.get('url')
        from_lang = data.get('from_lang', 'en')

        if not video_url:
            return jsonify({"error": "URL is required"}), 400

        video_id = extract_video_id(video_url)

        snippets, paragraphs = get_cached_processed_snippets(video_id, from_lang)

        return jsonify({
            "video_id": video_id,
            "snippets": snippets,
            "paragraphs": paragraphs,
            "from_lang": from_lang,
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/translate', methods=['POST'])
def translate_text():
    try:
        data = request.get_json()
        paragraphs = data.get('paragraphs')
        lines_by_paragraph = data.get('lines')  # optional nested list, per paragraph
        from_lang = data.get('from_lang', 'en')
        to_lang = data.get('to_lang', 'es')
        if not paragraphs or not isinstance(paragraphs, list):
            return jsonify({"error": "paragraphs (list of strings) is required"}), 400

        want_alignment = isinstance(lines_by_paragraph, list) and len(lines_by_paragraph) == len(paragraphs)

        cache_key = generate_cache_key(from_lang, to_lang, paragraphs, lines_by_paragraph if want_alignment else None)
        cached = None
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
            except Exception as e:
                print(f"Redis get error: {e}. Proceeding without cache.")

        if cached:
            payload = json.loads(cached)
            payload["cache_hit"] = True
            return jsonify(payload)

        if want_alignment:
            translated_paragraphs, translated_lines = translate_with_alignment(
                paragraphs, lines_by_paragraph, to_lang, from_lang
            )
            payload = {
                "translated_paragraphs": translated_paragraphs,
                "translated_lines": translated_lines,
            }
        else:
            translated_paragraphs = translate_paragraphs(paragraphs, to_lang, from_lang)
            payload = {"translated_paragraphs": translated_paragraphs}

        if redis_client:
            try:
                redis_client.setex(cache_key, REDIS_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                print(f"Redis setex error: {e}. Continuing without cache.")

        payload["cache_hit"] = False
        return jsonify(payload)

    except Exception as e:
        print(f"Translate Error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Progress Endpoints ───────────────────────────────────────────────────

def _sb_get(table, params=None):
    """GET from Supabase REST API."""
    resp = http_requests.get(f"{SUPABASE_REST_URL}/{table}", headers=SUPABASE_HEADERS, params=params or {})
    if not resp.ok:
        raise Exception(f"Supabase GET {table} failed ({resp.status_code}): {resp.text}")
    return resp.json()

def _sb_post(table, data, extra_headers=None, params=None):
    """POST to Supabase REST API."""
    headers = {**SUPABASE_HEADERS, **(extra_headers or {})}
    resp = http_requests.post(f"{SUPABASE_REST_URL}/{table}", headers=headers, json=data, params=params or {})
    if not resp.ok:
        raise Exception(f"Supabase POST {table} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _sb_patch(table, data, params=None):
    """PATCH (update) rows in Supabase REST API."""
    headers = {**SUPABASE_HEADERS}
    resp = http_requests.patch(f"{SUPABASE_REST_URL}/{table}", headers=headers, json=data, params=params or {})
    if not resp.ok:
        raise Exception(f"Supabase PATCH {table} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _sb_delete(table, params=None):
    """DELETE rows from Supabase REST API."""
    headers = {**SUPABASE_HEADERS}
    resp = http_requests.delete(f"{SUPABASE_REST_URL}/{table}", headers=headers, params=params or {})
    if not resp.ok:
        raise Exception(f"Supabase DELETE {table} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _ensure_video(youtube_id, title=None, thumbnail_url=None):
    """Insert a video row if it doesn't already exist. Returns the video UUID.
    Updates the title if it was previously missing.
    """
    if not supabase_ready:
        return None

    rows = _sb_get("videos", {"select": "id,title", "youtube_id": f"eq.{youtube_id}"})
    if rows:
        # Update title if we have one now but didn't before
        if title and not rows[0].get("title"):
            _sb_patch("videos", {"title": title}, {"youtube_id": f"eq.{youtube_id}"})
        return rows[0]["id"]

    row = {"youtube_id": youtube_id}
    if title:
        row["title"] = title
    row["thumbnail_url"] = thumbnail_url or f"https://img.youtube.com/vi/{youtube_id}/hqdefault.jpg"

    result = _sb_post("videos", row)
    return result[0]["id"]


@app.route("/api/progress", methods=["GET"])
@require_auth
def get_all_progress():
    """Fetch all progress rows for the authenticated user, joined with video metadata."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        rows = _sb_get("user_progress", {
            "select": "*, videos(youtube_id, title, thumbnail_url)",
            "user_id": f"eq.{g.user_id}",
            "order": "last_accessed_at.desc",
        })
        return jsonify({"progress": rows})
    except Exception as e:
        print(f"GET /api/progress error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/progress/upsert", methods=["POST"])
@require_auth
def upsert_progress():
    """Create or update a user's progress on a specific video + language pair."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    data = request.get_json()
    youtube_id = data.get("youtube_id")
    transcript_language = data.get("transcript_language")
    translation_language = data.get("translation_language")
    current_line_index = data.get("current_line_index", 0)
    total_lines = data.get("total_lines", 0)
    title = data.get("title")

    if not youtube_id or not transcript_language or not translation_language:
        return jsonify({"error": "youtube_id, transcript_language, and translation_language are required"}), 400

    try:
        video_id = _ensure_video(youtube_id, title=title)
        if not video_id:
            return jsonify({"error": "Failed to resolve video"}), 500

        row = {
            "user_id": g.user_id,
            "video_id": video_id,
            "transcript_language": transcript_language,
            "translation_language": translation_language,
            "current_line_index": current_line_index,
            "total_lines": total_lines,
            "last_accessed_at": datetime.now(timezone.utc).isoformat(),
        }

        result = _sb_post("user_progress", row,
            extra_headers={"Prefer": "return=representation,resolution=merge-duplicates"},
            params={"on_conflict": "user_id,video_id,transcript_language,translation_language"},
        )

        return jsonify({"progress": result[0] if result else None})
    except Exception as e:
        print(f"POST /api/progress/upsert error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/progress/<youtube_id>", methods=["GET"])
@require_auth
def get_video_progress(youtube_id):
    """Fetch the user's progress for a specific YouTube video."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        videos = _sb_get("videos", {"select": "id", "youtube_id": f"eq.{youtube_id}"})
        if not videos:
            return jsonify({"progress": None})

        video_uuid = videos[0]["id"]

        params = {
            "select": "*",
            "user_id": f"eq.{g.user_id}",
            "video_id": f"eq.{video_uuid}",
            "order": "last_accessed_at.desc",
            "limit": "1",
        }

        transcript_lang = request.args.get("transcript_language")
        translation_lang = request.args.get("translation_language")
        if transcript_lang:
            params["transcript_language"] = f"eq.{transcript_lang}"
        if translation_lang:
            params["translation_language"] = f"eq.{translation_lang}"

        rows = _sb_get("user_progress", params)
        return jsonify({"progress": rows[0] if rows else None})
    except Exception as e:
        print(f"GET /api/progress/{youtube_id} error: {e}")
        return jsonify({"error": str(e)}), 500


# ── User Profile Endpoints ──────────────────────────────────────────────

@app.route("/api/profile", methods=["GET"])
@require_auth
def get_profile():
    """Fetch the authenticated user's profile (name + role)."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500
    try:
        rows = _sb_get("user_profiles", {
            "select": "user_id,user_name,user_role",
            "user_id": f"eq.{g.user_id}",
        })
        return jsonify({"profile": rows[0] if rows else None})
    except Exception as e:
        print(f"GET /api/profile error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile", methods=["POST"])
@require_auth
def create_or_update_profile():
    """Create or update the user's profile (name + role)."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    data = request.get_json()
    user_name = (data.get("user_name") or "").strip()
    user_role = (data.get("user_role") or "").strip().lower()

    if not user_name or len(user_name) < 2:
        return jsonify({"error": "Name must be at least 2 characters"}), 400
    if user_role and user_role not in ("student", "teacher"):
        return jsonify({"error": "Role must be 'student' or 'teacher'"}), 400

    try:
        existing = _sb_get("user_profiles", {"select": "user_id,user_role", "user_id": f"eq.{g.user_id}"})

        if existing:
            # Update name (and role only if not already set)
            update_data = {"user_name": user_name}
            if user_role and not existing[0].get("user_role"):
                update_data["user_role"] = user_role
            result = _sb_patch("user_profiles", update_data, {"user_id": f"eq.{g.user_id}"})
        else:
            if not user_role:
                return jsonify({"error": "Role is required for new profiles"}), 400
            row = {
                "user_id": g.user_id,
                "user_name": user_name,
                "user_role": user_role,
            }
            result = _sb_post("user_profiles", row)

        return jsonify({"profile": result[0] if result else None})
    except Exception as e:
        print(f"POST /api/profile error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile/name", methods=["PATCH"])
@require_auth
def update_profile_name():
    """Update just the user's name (for existing users who lack one)."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    data = request.get_json()
    user_name = (data.get("user_name") or "").strip()

    if not user_name or len(user_name) < 2:
        return jsonify({"error": "Name must be at least 2 characters"}), 400

    try:
        existing = _sb_get("user_profiles", {"select": "user_id", "user_id": f"eq.{g.user_id}"})
        if existing:
            result = _sb_patch("user_profiles", {"user_name": user_name}, {"user_id": f"eq.{g.user_id}"})
        else:
            # Edge case: profile row doesn't exist yet — can't set name without role
            return jsonify({"error": "Profile not found. Please complete signup first."}), 404
        return jsonify({"profile": result[0] if result else None})
    except Exception as e:
        print(f"PATCH /api/profile/name error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Class Endpoints ─────────────────────────────────────────────────────

def _generate_class_code():
    """Generate a unique 6-character alphanumeric class code."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(20):  # max attempts
        code = ''.join(random.choices(chars, k=6))
        existing = _sb_get("classes", {"select": "class_id", "class_code": f"eq.{code}"})
        if not existing:
            return code
    raise Exception("Failed to generate unique class code after 20 attempts")


@app.route("/api/classes", methods=["POST"])
@require_auth
def create_class():
    """Create a new class (teacher only)."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    # Verify user is a teacher
    profile = _sb_get("user_profiles", {"select": "user_role", "user_id": f"eq.{g.user_id}"})
    if not profile or profile[0].get("user_role") != "teacher":
        return jsonify({"error": "Only teachers can create classes"}), 403

    data = request.get_json()
    class_name = (data.get("class_name") or "").strip()
    if not class_name:
        return jsonify({"error": "Class name is required"}), 400

    description = (data.get("description") or "").strip() or None
    subject = (data.get("subject") or "").strip() or None
    grade = (data.get("grade") or "").strip() or None

    try:
        class_code = _generate_class_code()
        row = {
            "class_name": class_name,
            "description": description,
            "class_code": class_code,
            "teacher_id": g.user_id,
            "subject": subject,
            "grade": grade,
            "is_active": True,
        }
        result = _sb_post("classes", row)
        return jsonify({"class": result[0] if result else None}), 201
    except Exception as e:
        print(f"POST /api/classes error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/classes", methods=["GET"])
@require_auth
def get_classes():
    """Get all classes for the authenticated user (teacher's classes or student's enrolled classes)."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        profile = _sb_get("user_profiles", {"select": "user_role", "user_id": f"eq.{g.user_id}"})
        if not profile:
            return jsonify({"classes": []})

        role = profile[0].get("user_role")

        if role == "teacher":
            classes = _sb_get("classes", {
                "select": "*, student_classes(count)",
                "teacher_id": f"eq.{g.user_id}",
                "is_active": "eq.true",
                "order": "created_at.desc",
            })
            return jsonify({"classes": classes, "role": "teacher"})
        else:
            # Student: get classes they've joined
            enrollments = _sb_get("student_classes", {
                "select": "class_id, joined_at, classes(*, user_profiles!classes_teacher_id_fkey(user_name))",
                "student_id": f"eq.{g.user_id}",
            })
            # Flatten the response
            classes = []
            for e in enrollments:
                cls = e.get("classes")
                if cls and cls.get("is_active"):
                    cls["joined_at"] = e.get("joined_at")
                    classes.append(cls)
            return jsonify({"classes": classes, "role": "student"})
    except Exception as e:
        print(f"GET /api/classes error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/classes/<class_id>", methods=["GET"])
@require_auth
def get_class_detail(class_id):
    """Get detailed class info including teacher and student list."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        # Fetch class info
        classes = _sb_get("classes", {
            "select": "*, user_profiles!classes_teacher_id_fkey(user_name, user_role)",
            "class_id": f"eq.{class_id}",
        })
        if not classes:
            return jsonify({"error": "Class not found"}), 404

        cls = classes[0]

        # Verify the user is the teacher or an enrolled student
        is_teacher = cls["teacher_id"] == g.user_id
        if not is_teacher:
            enrollment = _sb_get("student_classes", {
                "select": "student_class_id",
                "class_id": f"eq.{class_id}",
                "student_id": f"eq.{g.user_id}",
            })
            if not enrollment:
                return jsonify({"error": "Access denied"}), 403

        # Fetch enrolled students
        students = _sb_get("student_classes", {
            "select": "student_class_id, student_id, joined_at, user_profiles!student_classes_student_id_fkey(user_name)",
            "class_id": f"eq.{class_id}",
            "order": "joined_at.asc",
        })

        return jsonify({
            "class": cls,
            "students": students,
            "is_teacher": is_teacher,
        })
    except Exception as e:
        print(f"GET /api/classes/{class_id} error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/classes/join", methods=["POST"])
@require_auth
def join_class():
    """Student joins a class via class code."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    # Verify user is a student
    profile = _sb_get("user_profiles", {"select": "user_role", "user_id": f"eq.{g.user_id}"})
    if not profile or profile[0].get("user_role") != "student":
        return jsonify({"error": "Only students can join classes"}), 403

    data = request.get_json()
    class_code = (data.get("class_code") or "").strip().upper()
    if not class_code or len(class_code) != 6:
        return jsonify({"error": "Please enter a valid 6-character class code"}), 400

    try:
        # Find the class
        classes = _sb_get("classes", {
            "select": "class_id, class_name, is_active",
            "class_code": f"eq.{class_code}",
        })
        if not classes or not classes[0].get("is_active"):
            return jsonify({"error": "Class not found. Please check the code and try again."}), 404

        cls = classes[0]

        # Check if already enrolled
        existing = _sb_get("student_classes", {
            "select": "student_class_id",
            "class_id": f"eq.{cls['class_id']}",
            "student_id": f"eq.{g.user_id}",
        })
        if existing:
            return jsonify({"error": "You are already enrolled in this class"}), 409

        # Enroll the student
        row = {
            "class_id": cls["class_id"],
            "student_id": g.user_id,
        }
        _sb_post("student_classes", row)
        return jsonify({"message": f"Successfully joined {cls['class_name']}", "class_id": cls["class_id"]}), 200
    except Exception as e:
        print(f"POST /api/classes/join error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/classes/<class_id>", methods=["DELETE"])
@require_auth
def delete_class(class_id):
    """Delete a class (teacher only)."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        classes = _sb_get("classes", {"select": "class_id, teacher_id", "class_id": f"eq.{class_id}"})
        if not classes:
            return jsonify({"error": "Class not found"}), 404
        if classes[0]["teacher_id"] != g.user_id:
            return jsonify({"error": "Only the class teacher can delete this class"}), 403

        # Remove all student enrollments first
        _sb_delete("student_classes", {"class_id": f"eq.{class_id}"})
        # Delete the class
        _sb_delete("classes", {"class_id": f"eq.{class_id}"})
        return jsonify({"message": "Class deleted successfully"})
    except Exception as e:
        print(f"DELETE /api/classes/{class_id} error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/classes/<class_id>/students/<student_id>", methods=["DELETE"])
@require_auth
def remove_student(class_id, student_id):
    """Remove a student from a class. Teacher can remove any student; student can remove themselves."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        # Verify authorization
        classes = _sb_get("classes", {"select": "teacher_id", "class_id": f"eq.{class_id}"})
        if not classes:
            return jsonify({"error": "Class not found"}), 404

        is_teacher = classes[0]["teacher_id"] == g.user_id
        is_self = student_id == g.user_id

        if not is_teacher and not is_self:
            return jsonify({"error": "Not authorized to remove this student"}), 403

        _sb_delete("student_classes", {
            "class_id": f"eq.{class_id}",
            "student_id": f"eq.{student_id}",
        })
        return jsonify({"message": "Student removed from class"})
    except Exception as e:
        print(f"DELETE /api/classes/{class_id}/students/{student_id} error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Admin Cache Endpoints ────────────────────────────────────────────────

@app.route('/api/admin/clear-translation-cache', methods=['POST'])
@optional_auth
def clear_translation_cache():
    """
    Clear all translation cache entries. Requires auth token.
    Useful after a translation method change (e.g. new full-context algorithm).
    All videos will re-translate with the new method on next access.
    """
    if not g.user_id:
        return jsonify({"error": "Unauthorized"}), 401

    if not redis_client:
        return jsonify({"error": "Redis not configured"}), 503

    try:
        patterns = ["translate_paragraphs:*", "translate_paragraphs:v4:*"]
        total_deleted = 0
        for pattern in patterns:
            keys = redis_client.keys(pattern)
            if keys:
                total_deleted += redis_client.delete(*keys)
        return jsonify({
            "message": f"Cleared {total_deleted} translation cache entries",
            "entries_deleted": total_deleted,
        })
    except Exception as e:
        print(f"Clear cache error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    is_dev = os.environ.get("FLASK_ENV") == "development"
    app.run(host='0.0.0.0', port=port, debug=is_dev)