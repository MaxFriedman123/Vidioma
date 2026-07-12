import os
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from flask import Flask, request, jsonify, g
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
try:
    # Purpose-built config for Webshare rotating RESIDENTIAL proxies: uses the
    # correct HTTP endpoint (p.webshare.io:80), rotates IPs automatically, and
    # retries when an IP is blocked — the reliable way past YouTube's
    # datacenter-IP blocking on hosts like Render.
    from youtube_transcript_api.proxies import WebshareProxyConfig
except Exception:  # pragma: no cover - older library versions
    WebshareProxyConfig = None
import re
import json
import hashlib
import hmac
import string
import random
import redis
import requests as http_requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - very old urllib3
    Retry = None
from datetime import datetime, timezone
from deep_translator import GoogleTranslator
from functools import lru_cache, wraps
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
import jwt


def _build_pooled_session(pool_maxsize=16):
    """A requests.Session with HTTP keep-alive + a connection pool so repeated
    calls to the same host (DeepL, Supabase) reuse a warm TCP+TLS connection
    instead of paying a fresh handshake every time (bare requests.post/get opens
    and discards a Session per call).

    A `connect`-only Retry is the safety guardrail: a pooled connection can go
    stale between requests, and because callers like _deepl_request swallow
    exceptions and fall back to a lower-quality engine, a stale-connection error
    must NOT surface as a translation failure. Retrying only on CONNECT errors
    (never on read/status) re-establishes a dead pooled socket BEFORE the request
    body is sent, so there are no duplicate side effects and behavior is
    no-worse-than today's fresh-connection-per-call.
    """
    session = http_requests.Session()
    if Retry is not None:
        retry = Retry(total=None, connect=2, read=0, status=0, redirect=0,
                      backoff_factor=0.2, raise_on_status=False)
        adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize, max_retries=retry)
    else:  # pragma: no cover
        adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Reused across all DeepL and Supabase calls. Pool sized comfortably above the
# translation ThreadPoolExecutor's max_workers (4) plus Supabase concurrency.
_HTTP_SESSION = _build_pooled_session(pool_maxsize=16)

# Hard timeout (seconds) for a single scraped-translation network call. Without
# it, a stalled socket in the translators/deep-translator libraries hangs a Flask
# worker indefinitely (the "transcript loads forever" symptom). Defined here so
# the deep-translator request shim below can use it at import time.
_TRANSLATE_CALL_TIMEOUT = 12.0

# deep-translator's GoogleTranslator issues a bare `requests.get(...)` with NO
# timeout, so a stalled connection would hang forever. Inject a default timeout
# into the requests.get/post that deep-translator's modules call. We patch each
# deep_translator submodule's own `requests` reference (they do `import requests`
# then `requests.get(...)`), leaving the app's other requests usage untouched.
def _install_deep_translator_timeout(default_timeout):
    try:
        import importlib
        import requests as _rq

        def _with_timeout(func):
            def wrapper(*args, **kwargs):
                kwargs.setdefault("timeout", default_timeout)
                return func(*args, **kwargs)
            return wrapper

        for _mod_name in ("google", "base"):
            try:
                _m = importlib.import_module(f"deep_translator.{_mod_name}")
            except Exception:
                continue
            _mod_rq = getattr(_m, "requests", None)
            if _mod_rq is _rq:
                # Wrap the shared requests module's get/post once (idempotent).
                if not getattr(_rq.get, "_vidioma_timeout_wrapped", False):
                    _rq.get = _with_timeout(_rq.get)
                    _rq.get._vidioma_timeout_wrapped = True
                if not getattr(_rq.post, "_vidioma_timeout_wrapped", False):
                    _rq.post = _with_timeout(_rq.post)
                    _rq.post._vidioma_timeout_wrapped = True
    except Exception as _exc:  # pragma: no cover - never block startup on this
        print(f"Warning: could not install deep-translator timeout shim: {_exc}")


_install_deep_translator_timeout(_TRANSLATE_CALL_TIMEOUT)

app = Flask(__name__)
CORS(app)

import gzip as _gzip

# Transcript and translate payloads are sizeable JSON (snippets + paragraphs +
# per-line chunks). gzip-compress responses so the transfer is smaller on slow
# links — a pure transport win: the client decompresses to byte-identical JSON.
# Implemented inline (no extra dependency) and guarded so it never alters small
# payloads, streamed/passthrough responses, or already-encoded content.
_GZIP_MIN_BYTES = 1024


@app.after_request
def _gzip_response(response):
    try:
        accept_encoding = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept_encoding.lower():
            return response
        if response.direct_passthrough:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        if response.headers.get("Content-Encoding"):
            return response
        ctype = (response.content_type or "").lower()
        if "application/json" not in ctype and "text/" not in ctype:
            return response
        data = response.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return response
        compressed = _gzip.compress(data, compresslevel=6)
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = len(compressed)
        response.headers.add("Vary", "Accept-Encoding")
    except Exception as exc:  # never let compression break a response
        print(f"gzip after_request skipped: {exc}")
    return response

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_TTL_SECONDS = int(os.environ.get("REDIS_TTL_SECONDS", "86400"))

DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
DEEPL_API_URL = (
    "https://api-free.deepl.com/v2/translate"
    if DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2/translate"
)

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
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        except Exception as e:
            # JWKS lookup failures (unknown/absent kid, transient JWKS outage)
            # raise PyJWKClientError, which is NOT an InvalidTokenError. Treat
            # them as auth failures (401) rather than leaking a 500.
            print(f"Auth verification error: {e}")
            return jsonify({"error": "Could not verify authentication token"}), 401

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
            except Exception:
                # Any verification failure (invalid token, JWKS lookup error,
                # transient outage) simply falls back to guest — never a 500.
                pass
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
# Handles watch?v=, youtu.be/, /embed/, /v/, /shorts/, and bare 11-char IDs,
# plus trailing query/fragment params (?t=30, &feature=...).
_YOUTUBE_ID_RE = re.compile(
    r'(?:youtu\.be/|/embed/|/v/|/shorts/|watch\?v=|[?&]v=)([0-9A-Za-z_-]{11})'
)
_BARE_ID_RE = re.compile(r'^[0-9A-Za-z_-]{11}$')


def extract_video_id(url):
    if not isinstance(url, str):
        return None
    url = url.strip()
    match = _YOUTUBE_ID_RE.search(url)
    if match:
        return match.group(1)
    # Allow callers to pass a bare 11-character video ID directly.
    if _BARE_ID_RE.match(url):
        return url
    return None

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

        # 2. FALLBACK TO ROTATING RESIDENTIAL PROXY IF BLOCKED OR FAILED.
        # YouTube blocks datacenter IPs (e.g. Render), so in prod this fallback
        # is the path that actually works — direct fetch above almost always
        # fails there.
        proxy_username = os.environ.get("WEBSHARE_USERNAME")
        proxy_password = os.environ.get("WEBSHARE_PASSWORD")

        if not proxy_username or not proxy_password:
            raise ValueError("Proxy credentials are not configured and direct fetch failed.")

        if WebshareProxyConfig is not None:
            # Rotating residential proxies over Webshare's HTTP endpoint
            # (p.webshare.io:80). This auto-rotates IPs and retries when an IP
            # is blocked. Passing the raw username is correct — the config adds
            # the "-rotate" suffix itself.
            proxy_config = WebshareProxyConfig(
                proxy_username=proxy_username,
                proxy_password=proxy_password,
            )
        else:
            # Fallback for older library versions without WebshareProxyConfig:
            # use the HTTP rotating endpoint (NOT socks5:1080, which targets the
            # wrong product/port and does not rotate).
            http_url = f"http://{proxy_username}-rotate:{proxy_password}@p.webshare.io:80/"
            proxy_config = GenericProxyConfig(http_url=http_url, https_url=http_url)

        proxy_api = YouTubeTranscriptApi(proxy_config=proxy_config)

        # Bounded server-side retry on the proxy path. The Webshare rotating
        # config already retries a blocked IP internally, but other transient
        # upstream errors (connection resets, sporadic 5xx, a cold first proxy
        # handshake) can still surface on the first try and then succeed on the
        # next — which is exactly the "fails once, works on retry" symptom.
        # Retrying here means the user usually doesn't have to.
        import time
        last_exc = None
        for attempt in range(3):
            try:
                return attempt_fetch(proxy_api)
            except Exception as proxy_exc:
                last_exc = proxy_exc
                print(f"Proxy fetch attempt {attempt + 1} failed: {proxy_exc}")
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
        raise last_exc

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
            buf_last_end = None

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
            buf_last_end = None

    flush()
    return assigned, paragraphs


class TranscriptTranslationError(Exception):
    """Raised when the transcript must be translated into `from_lang` but the
    translation didn't actually produce `from_lang` text (provider outage,
    cooldown, or a no-op auto->same-language result). Raising instead of
    returning means the (poisoned) result is NOT memoized by lru_cache and a
    later retry can succeed."""
    pass


def _looks_like_same_text(sources, translations):
    """True when translations came back byte-identical to their sources for
    every non-empty source line — i.e. the translator effectively no-op'd, so
    the "translated" text is still in the original language."""
    non_empty = [(s or "").strip() for s in sources if (s or "").strip()]
    if not non_empty:
        return True
    same = 0
    total = 0
    for s, t in zip(sources, translations):
        s_clean = (s or "").strip()
        if not s_clean:
            continue
        total += 1
        if (t or "").strip() == s_clean:
            same += 1
    if total == 0:
        return True
    # Treat as un-translated only when (almost) everything is identical, so a
    # transcript with a few proper-noun/number lines that legitimately translate
    # to themselves isn't flagged.
    return same / total >= 0.9


def _processed_snippets_cache_key(video_id, from_lang):
    """Redis key for the cross-worker processed-transcript cache. Hashed so a
    from_lang containing ':' can't collide with the key structure."""
    raw = f"{video_id}\x00{(from_lang or '').lower()}"
    return "transcript:v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@lru_cache(maxsize=100)
def get_cached_processed_snippets(video_id, from_lang):
    """
    Fetch and clean the transcript, group fragments into paragraphs, and —
    when the transcript is from a different language than requested — also
    return paragraph-level translations into the source language.

    Returns (snippets, paragraphs) where each snippet is
    {source, start, duration, paragraph} and paragraphs is a list of strings
    aligned to the paragraph indices on the snippets.

    Two cache layers sit in front of the (expensive) fetch: the per-process
    lru_cache (L1, this decorator) and a cross-worker Redis layer (L2, below)
    that survives worker restarts and is shared across gunicorn workers. The L2
    write is SCOPED to is_correct_lang=True only — i.e. videos that already carry
    from_lang subtitles, where the output is a deterministic function of the
    immutable YouTube subtitles + deterministic grouping and NO translation
    cascade runs. The manual-translation path is provider-state-dependent and
    deadline-truncatable, so it is intentionally never written to L2.
    """
    # L2 read: a hit skips the YouTube fetch + grouping entirely.
    _redis_key = _processed_snippets_cache_key(video_id, from_lang)
    if redis_client:
        try:
            cached = redis_client.get(_redis_key)
            if cached:
                assigned_c, paragraphs_c = json.loads(cached)
                return assigned_c, paragraphs_c
        except Exception as e:
            print(f"Redis transcript get error: {e}. Proceeding without L2 cache.")

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

    # Only translate when transcript is not already in the requested language.
    # YouTube couldn't give us a `from_lang` transcript (native or auto-
    # translated), so the fetched text is in the video's original language. We
    # must translate BOTH the paragraphs AND each displayed snippet line into
    # `from_lang` — otherwise the transcript the user reads stays in the wrong
    # language (e.g. picking German shows English) even though the target-side
    # translation is derived correctly.
    if not is_correct_lang and paragraphs:
        print(f"Manually translating {video_id} to {from_lang}")

        # Build per-paragraph source-line lists in paragraph order so we can
        # translate paragraphs (for context) and recover aligned per-line text.
        # We keep this pre-translation source (lines_by_paragraph) so we can
        # detect (and repair) any line that failed to translate.
        # Single O(n) pass over `assigned` instead of re-scanning it once per
        # paragraph (which was O(paragraphs * snippets) — quadratic on long
        # videos). `assigned` is already in paragraph order, so members land in
        # ascending index order exactly as the old nested scan produced them.
        snippet_idx_by_paragraph = [[] for _ in range(len(paragraphs))]
        lines_by_paragraph = [[] for _ in range(len(paragraphs))]
        for i, s in enumerate(assigned):
            p_idx = s["paragraph"]
            snippet_idx_by_paragraph[p_idx].append(i)
            lines_by_paragraph[p_idx].append(s["source"])

        translated_paragraphs, translated_lines = translate_with_alignment(
            paragraphs, lines_by_paragraph, from_lang, source_lang="auto"
        )

        # Overwrite each snippet's displayed source with its translated line so
        # the shown transcript is actually in `from_lang`. CRITICAL: never leave
        # an original-language line in place. If a per-line chunk is missing or
        # empty (DeepL can drop/blank a line, or the aligned list can be short),
        # translate that individual line directly rather than showing the user
        # untranslated (original-language) text — which is exactly the "shows
        # the English transcript" bug when a Spanish video has only English subs.
        repaired_line_texts = []
        for p_idx, members in enumerate(snippet_idx_by_paragraph):
            line_chunks = translated_lines[p_idx] if p_idx < len(translated_lines) else []
            for slot, snippet_i in enumerate(members):
                chunk = line_chunks[slot] if slot < len(line_chunks) else ""
                if not (chunk or "").strip():
                    # Direct per-line fallback so no original-language line leaks.
                    original = assigned[snippet_i]["source"]
                    chunk = (_translate_text(original, from_lang, "auto") or "").strip()
                if (chunk or "").strip():
                    assigned[snippet_i]["source"] = chunk
                repaired_line_texts.append(assigned[snippet_i]["source"])

        # If the paragraph translation is empty for a paragraph, rebuild it from
        # the (now translated) member lines so the practice view has context.
        rebuilt_paragraphs = []
        for p_idx, members in enumerate(snippet_idx_by_paragraph):
            para = translated_paragraphs[p_idx] if p_idx < len(translated_paragraphs) else ""
            if not (para or "").strip():
                para = " ".join(assigned[i]["source"] for i in members).strip()
            rebuilt_paragraphs.append(para)
        paragraphs = rebuilt_paragraphs

        # Guard against caching an un-translated result. If the whole translation
        # cascade no-op'd (provider outage / DeepL cooldown / auto-detect landed
        # on the same language), the "translated" lines are byte-identical to the
        # originals — i.e. still the video's original language. Raise so this bad
        # result is NOT memoized by lru_cache and the user's retry can re-attempt.
        flat_original = [ln for group in lines_by_paragraph for ln in group]
        if _looks_like_same_text(flat_original, repaired_line_texts):
            raise TranscriptTranslationError(
                f"Transcript translation into '{from_lang}' produced no translated text "
                f"(provider unavailable). Not caching; retry may succeed."
            )

    # L2 write, ONLY for the already-in-language path (no translation ran, so
    # the result is deterministic and safe to share across workers/restarts).
    if redis_client and is_correct_lang and assigned:
        try:
            redis_client.setex(
                _redis_key,
                REDIS_TTL_SECONDS,
                json.dumps((assigned, paragraphs), ensure_ascii=False),
            )
        except Exception as e:
            print(f"Redis transcript setex error: {e}. Continuing without L2 cache.")

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
    # v6: per-line translation now comes from DeepL structured output (exact
    # 1:1 line alignment) rather than blob-split-by-anchor-DP, so old cached
    # results have different (worse) line boundaries — bump to invalidate them.
    return "translate_paragraphs:v6:" + hashlib.sha256(key_raw.encode("utf-8")).hexdigest()


def generate_paragraph_cache_key(from_lang, to_lang, paragraph, lines):
    """Per-PARAGRAPH cache key for the alignment path. Because
    translate_with_alignment._process(p_idx) reads only paragraphs[p_idx] and
    lines_by_paragraph[p_idx] — no cross-paragraph context on either the DeepL
    or the fallback path — a paragraph's translation is a pure function of its
    own text + lines + languages. Keying per paragraph (instead of per batch)
    means the SAME paragraph reused in a different lookahead/scrub window is a
    cache HIT rather than a miss, with byte-identical output.
    """
    key_payload = {
        "from": from_lang.lower(),
        "to": to_lang.lower(),
        "p": paragraph,
        "l": lines,
    }
    key_raw = json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "translate_para:v1:" + hashlib.sha256(key_raw.encode("utf-8")).hexdigest()


_PARAGRAPH_SEPARATOR = "\n\n"
_TARGET_SENTENCE_RE = re.compile(r'(?<=[.!?…])\s+')

_TRANSLATORS_IMPORT_FAILED = False
_BING_COOLDOWN_UNTIL = 0.0  # epoch seconds — skip Bing while rate-limited
_BING_COOLDOWN_SECONDS = 120
_DEEPL_COOLDOWN_UNTIL = 0.0  # cooldown when monthly quota is exhausted
_DEEPL_COOLDOWN_SECONDS = 3600


def _deepl_available():
    """True when DeepL is configured and not in a cooldown window."""
    import time
    return bool(DEEPL_API_KEY) and time.time() >= _DEEPL_COOLDOWN_UNTIL


# App/YouTube language codes (frontend list + youtube-transcript-api) don't all
# match DeepL's expected codes. Left un-mapped, e.g. "zh-CN".upper() -> "ZH-CN"
# and "iw".upper() -> "IW" are rejected by DeepL (400), silently disabling the
# highest-quality engine for those languages. Map to DeepL's codes; return None
# for languages DeepL doesn't support so we skip DeepL instead of firing a
# guaranteed-400 request.
_DEEPL_TARGET_CODES = {
    "en": "EN-US", "es": "ES", "fr": "FR", "de": "DE", "it": "IT",
    "pt": "PT-BR", "ja": "JA", "ko": "KO", "ru": "RU",
    "zh-cn": "ZH", "zh": "ZH", "iw": None, "he": None,
}
# Source codes: DeepL wants the base code (no regional variant) and no EN-US.
_DEEPL_SOURCE_CODES = {
    "en": "EN", "es": "ES", "fr": "FR", "de": "DE", "it": "IT",
    "pt": "PT", "ja": "JA", "ko": "KO", "ru": "RU",
    "zh-cn": "ZH", "zh": "ZH", "iw": None, "he": None,
}


def _deepl_target_code(lang):
    """Map an app language code to DeepL's target code, or None if unsupported."""
    if not lang:
        return None
    key = lang.lower()
    if key in _DEEPL_TARGET_CODES:
        return _DEEPL_TARGET_CODES[key]
    base = key.split("-")[0]
    if base in _DEEPL_TARGET_CODES:
        return _DEEPL_TARGET_CODES[base]
    return lang.upper()  # best-effort for codes we didn't special-case


def _deepl_source_code(lang):
    """Map an app language code to DeepL's source code, or None if unsupported/auto."""
    if not lang or lang == "auto":
        return None
    key = lang.lower()
    if key in _DEEPL_SOURCE_CODES:
        return _DEEPL_SOURCE_CODES[key]
    base = key.split("-")[0]
    if base in _DEEPL_SOURCE_CODES:
        return _DEEPL_SOURCE_CODES[base]
    return lang.upper()


def _deepl_supports_target(lang):
    """True when DeepL can translate INTO this language."""
    return _deepl_target_code(lang) is not None


# ── Per-engine language-code normalization ──────────────────────────────
# The app/YouTube language code for a language is NOT accepted uniformly by
# every backend engine. Hebrew is the worst offender: the app sends "iw" (the
# legacy ISO code YouTube uses), but the `translators` package (Bing + the free
# engines) only accepts "he" and rejects "iw" outright, while deep-translator's
# Google only accepts "iw" and rejects "he". Left unmapped, a Hebrew target made
# EVERY translators engine raise "Unsupported language[iw]" — so a video that
# needed manual translation into Hebrew burned ~15 failing scraping calls per
# paragraph before deep-translator's Google finally worked. Combined with the
# lack of any request timeout, that reads to the user as "loads forever".
#
# Normalize per engine so each call uses the code that engine actually accepts.
_TS_CODE_OVERRIDES = {   # codes for the `translators` package (Bing + free)
    "iw": "he",          # translators wants modern "he", not legacy "iw"
}
_GOOGLE_CODE_OVERRIDES = {  # codes for deep-translator's GoogleTranslator
    "he": "iw",             # deep-translator's Google wants legacy "iw"
}


def _ts_lang(code):
    """Normalize a language code for the `translators` package."""
    if not code:
        return code
    key = code.lower()
    return _TS_CODE_OVERRIDES.get(key, code)


def _google_lang(code):
    """Normalize a language code for deep-translator's GoogleTranslator."""
    if not code or code == "auto":
        return code
    key = code.lower()
    return _GOOGLE_CODE_OVERRIDES.get(key, code)


# _TRANSLATE_CALL_TIMEOUT is defined near the top of the module (before the
# deep-translator request-timeout shim, which needs it at import time).


def _deepl_request(texts, target_lang, source_lang="auto", extra_params=None):
    """Low-level DeepL call. `texts` is a list of strings (DeepL translates each
    as its own element and returns them in the SAME order, 1:1).

    Returns a list of translated strings (same length as `texts`), or None on
    any failure so callers can fall back. Honors the shared DeepL cooldown.
    """
    global _DEEPL_COOLDOWN_UNTIL
    import time
    if not DEEPL_API_KEY:
        return None
    if time.time() < _DEEPL_COOLDOWN_UNTIL:
        return None
    if not texts:
        return []

    # Normalize to DeepL's own codes. If DeepL doesn't support the target
    # language, skip it (return None) so callers fall back instead of us firing
    # a request DeepL will reject with a 400.
    deepl_target = _deepl_target_code(target_lang)
    if deepl_target is None:
        return None
    deepl_source = _deepl_source_code(source_lang)

    # requests encodes a list value as repeated `text=` params, which is exactly
    # DeepL's multi-text format. Build an explicit tuple list so ordering and the
    # extra params (context/tag_handling/…) are unambiguous.
    fields = [("text", t) for t in texts]
    fields.append(("target_lang", deepl_target))
    if deepl_source:
        fields.append(("source_lang", deepl_source))
    for k, v in (extra_params or {}).items():
        fields.append((k, v))

    try:
        resp = _HTTP_SESSION.post(
            DEEPL_API_URL,
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            data=fields,
            timeout=30,
        )
        if resp.status_code == 456:
            _DEEPL_COOLDOWN_UNTIL = time.time() + _DEEPL_COOLDOWN_SECONDS
            print(f"DeepL quota exhausted (456); cooling down for {_DEEPL_COOLDOWN_SECONDS}s")
            return None
        if resp.status_code == 429:
            _DEEPL_COOLDOWN_UNTIL = time.time() + 60
            print("DeepL rate-limited (429); 60s cooldown")
            return None
        resp.raise_for_status()
        translations = (resp.json() or {}).get("translations") or []
        if len(translations) != len(texts):
            print(f"DeepL returned {len(translations)} translations for {len(texts)} inputs; falling back")
            return None
        return [t.get("text", "") for t in translations]
    except Exception as exc:
        print(f"DeepL request failed ({exc}); falling back")
        return None


def _deepl_translate(text, target_lang, source_lang="auto"):
    """Translate a single string via DeepL. Returns None on failure, "" for empty
    input. Thin wrapper over _deepl_request for the paragraph/blob callers.
    """
    if not DEEPL_API_KEY:
        return None
    if not (text or "").strip():
        return ""
    result = _deepl_request([text], target_lang, source_lang)
    if not result:
        return None
    return result[0] or None


# ── Structured per-line translation (context-preserving, no re-splitting) ──
# The old approach translated a paragraph as one blob then tried to CUT it back
# into per-line pieces. That is lossy: cross-language word reordering means a
# line's translation is not a contiguous slice, and CJK output has no spaces to
# cut on. Instead we get per-line output directly FROM DeepL while still giving
# it the whole paragraph as context, so line-to-line correspondence is exact by
# construction and translation quality keeps full cross-line context.

_DEEPL_LINE_OPEN = "<ln>"
_DEEPL_LINE_CLOSE = "</ln>"
# Matches the translated content of each <ln>…</ln>, tolerating whitespace and
# any attributes DeepL might add. DOTALL so multi-word/segmented content matches.
_DEEPL_LINE_RE = re.compile(r"<ln\b[^>]*>(.*?)</ln>", re.DOTALL | re.IGNORECASE)


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


def _deepl_translate_lines(lines, target_lang, source_lang="auto"):
    """Translate a paragraph's subtitle lines into per-line target strings,
    preserving full paragraph context AND exact 1:1 line correspondence.

    Returns a list of translated strings the SAME length as `lines`, or None on
    any failure (caller falls back). Empty/whitespace-only lines map to "".

    Two DeepL strategies, tried in order:
      1) XML tag handling: wrap each line in <ln>…</ln>, translate the WHOLE
         paragraph as ONE unit (maximum cross-line context / cohesion), then
         split the response on the tags. Boundaries come straight from DeepL.
      2) Multi-text array + context: send each line as its own text[] element in
         ONE request, passing the joined paragraph as the (untranslated) context
         parameter. DeepL guarantees N outputs for N inputs, in order.
    """
    if not _deepl_available():
        return None
    if not lines:
        return []

    # Track which lines are non-empty; empty lines are re-inserted as "" so the
    # returned list stays index-aligned with the input.
    idx_map = [i for i, ln in enumerate(lines) if (ln or "").strip()]
    non_empty = [lines[i].strip() for i in idx_map]
    if not non_empty:
        return ["" for _ in lines]

    def _rebuild(translated_non_empty):
        out = ["" for _ in lines]
        for slot, val in zip(idx_map, translated_non_empty):
            out[slot] = (val or "").strip()
        return out

    # --- Strategy 1: XML tag handling (single context-rich translation unit) ---
    tagged = "".join(f"{_DEEPL_LINE_OPEN}{_xml_escape(t)}{_DEEPL_LINE_CLOSE}" for t in non_empty)
    result = _deepl_request(
        [tagged],
        target_lang,
        source_lang,
        extra_params={
            "tag_handling": "xml",
            "outline_detection": "0",
            "splitting_tags": "ln",
        },
    )
    if result:
        matches = _DEEPL_LINE_RE.findall(result[0] or "")
        if len(matches) == len(non_empty):
            return _rebuild([_xml_unescape(m).strip() for m in matches])
        print(f"DeepL XML returned {len(matches)} segments for {len(non_empty)} lines; trying array mode")

    # --- Strategy 2: multi-text array + paragraph context ---
    context = " ".join(non_empty)
    arr = _deepl_request(
        non_empty,
        target_lang,
        source_lang,
        extra_params={"context": context, "split_sentences": "0"},
    )
    if arr and len(arr) == len(non_empty):
        return _rebuild(arr)

    return None


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
    src = "auto" if (source_lang or "auto") == "auto" else _ts_lang(source_lang)
    tgt = _ts_lang(target_lang)
    last_exc = None
    for attempt in range(attempts):
        try:
            # timeout bounds the underlying HTTP call so a stalled socket can't
            # hang the whole cascade (and the request) indefinitely.
            return ts.translate_text(
                text,
                translator=engine,
                from_language=src,
                to_language=tgt,
                timeout=_TRANSLATE_CALL_TIMEOUT,
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


_QUALITY_FALLBACK_ENGINES = ("alibaba", "caiyun", "sogou", "iciba", "youdao", "reverso")


def _google_translate(text, target_lang, source_lang="auto"):
    """Translate one string via deep-translator's Google with a hard timeout.

    deep-translator issues a plain requests.get() with NO timeout (see the shim
    installed at import time, which injects _TRANSLATE_CALL_TIMEOUT), so a stalled
    socket now errors at the timeout instead of hanging the worker forever — the
    "transcript loads forever" symptom. Language codes are normalized to the codes
    Google's endpoint wants (e.g. Hebrew -> "iw"). Returns "" on timeout/failure so
    the caller can fall back or surface a clean error rather than spinning.
    """
    clean = (text or "").strip()
    if not clean:
        return ""
    tgt = _google_lang(target_lang)
    src = _google_lang(source_lang) if source_lang and source_lang != "auto" else "auto"
    try:
        translator = GoogleTranslator(source=src, target=tgt)
        return translator.translate(clean) or ""
    except Exception as exc:
        print(f"Google translate failed: {exc}")
        return ""


def _translate_text(text, target_lang, source_lang="auto"):
    """Quality-first cascade: DeepL → Bing → multiple free engines → Google.

    DeepL is the highest-quality translator available and uses an official API
    (no scraping / rate-limit surprises) within the 500K chars/month free tier.
    Bing is the best free-scraped option but rate-limits Render's IP. The free
    engines after Bing all return better-than-Google output on context-sensitive
    words; Google is the absolute last resort (gives 'baúles' for elephant trunks).
    """
    if not (text or "").strip():
        return ""
    deepl = _deepl_translate(text, target_lang, source_lang)
    if deepl:
        return deepl
    bing = _bing_translate(text, target_lang, source_lang)
    if bing:
        return bing
    for engine in _QUALITY_FALLBACK_ENGINES:
        result = _ts_translate(engine, text, target_lang, source_lang, attempts=2)
        if result and not isinstance(result, Exception):
            return result
    print("All quality engines failed; falling back to Google (may produce lower-quality output)")
    return _google_translate(text, target_lang, source_lang)


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

    # For no-space scripts (CJK/Thai), the anchor-overlap DP over CHARACTER units
    # is unreliable — a single character matches many anchor positions, so the DP
    # degenerates. A proportional character split (used here) is closer to even
    # and far better than the old whole-paragraph-on-one-line collapse. This only
    # runs on the fallback path anyway (DeepL structured output is primary).
    if _is_no_space_script(paragraph_translation):
        return _proportional_word_split(paragraph_translation, line_anchors)

    words = paragraph_translation.split()
    joiner = " "
    m = len(words)
    if m < n:
        # Fewer paragraph units than lines — can't give each line its own unit.
        # Fall back to proportional split so we at least return n chunks.
        return _proportional_word_split(paragraph_translation, line_anchors)

    # The DP below is ~O(n * m^2) in time and O(n * m) in memory. Paragraphs are
    # capped at _MAX_PARAGRAPH_CHARS server-side, but /api/translate accepts
    # client-supplied paragraphs/lines directly, so guard against a crafted
    # oversized input pinning a worker. Fall back to the cheap proportional
    # split when the DP would be too large.
    if m > 600 or n > 200 or (n * m) > 20000:
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
        chunks.append(joiner.join(words[splits[j] : splits[j + 1]]).strip())
    return chunks


def _proportional_word_split(paragraph_translation, line_anchors):
    """Fallback: divide paragraph units across lines by count (no semantics).
    Uses character units for no-space scripts (CJK/Thai) so a spaceless
    paragraph is spread across all lines instead of dumped onto one."""
    no_space = _is_no_space_script(paragraph_translation)
    joiner = "" if no_space else " "
    words = _segment_units(paragraph_translation)
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
        chunks.append(joiner.join(words[idx : idx + size]))
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
        # Timeout-bounded + language-code-normalized Google call.
        return (_google_translate(clean, target_lang, source_lang) or clean).strip()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_one, lines_flat))


def _legacy_align_paragraph(paragraph_text, source_lines, target_lang, source_lang):
    """Fallback per-line split: blob paragraph translation + per-line Google
    anchors + DP alignment. Used only when DeepL structured translation is
    unavailable. Kept for graceful degradation, not the primary path.
    """
    anchors = _translate_line_anchors(list(source_lines or []), target_lang, source_lang)
    return align_lines_to_paragraph(paragraph_text, anchors)


def translate_with_alignment(paragraphs, lines_by_paragraph, target_lang, source_lang='auto'):
    """
    Translate paragraphs AND produce per-line aligned chunks such that each
    source subtitle line maps 1:1 to a translated line.

    Primary path: DeepL structured per-line translation (_deepl_translate_lines)
    which keeps full paragraph context AND returns exact per-line boundaries, so
    no blob-splitting/anchor-DP is needed. The paragraph translation is then the
    join of those per-line pieces, so the paragraph view and per-line view use
    identical wording.

    Fallback path (per paragraph, only when DeepL is unavailable / returns a bad
    count): the legacy blob translation + Google anchors + DP alignment.

    Returns (translated_paragraphs, translated_lines_by_paragraph).
    """
    n_paras = len(paragraphs)
    translated_paragraphs = [""] * n_paras
    translated_lines = [[] for _ in range(n_paras)]

    def _process(p_idx):
        source_lines = lines_by_paragraph[p_idx] if p_idx < len(lines_by_paragraph) else []
        source_lines = list(source_lines or [])

        # Primary: DeepL structured per-line (context-preserving, exact 1:1).
        if source_lines and _deepl_available():
            per_line = _deepl_translate_lines(source_lines, target_lang, source_lang)
            if per_line is not None and len(per_line) == len(source_lines):
                # Paragraph text = join of the per-line translations, so both
                # views stay perfectly consistent (same engine, same wording).
                para_text = " ".join(chunk for chunk in per_line if chunk).strip()
                return p_idx, para_text, per_line

        # Fallback: translate the blob for context, then split via anchor DP.
        para_text = translate_paragraphs([paragraphs[p_idx]], target_lang, source_lang)
        para_text = para_text[0] if para_text else ""
        if source_lines:
            lines = _legacy_align_paragraph(para_text, source_lines, target_lang, source_lang)
        else:
            lines = []
        return p_idx, para_text, lines

    # Parallelize across paragraphs (a lookahead batch is small, ~3 paragraphs).
    # A hard wall-clock cap guarantees this returns even if an engine stalls past
    # its own per-call timeout: any paragraph that doesn't finish in time is left
    # empty (the manual-translate branch repairs empty lines individually, and
    # /api/translate rebuilds empty paragraphs). This is the backstop that keeps
    # the transcript from "loading forever".
    max_workers = min(4, max(1, n_paras))
    # Budget scales with batch size but is bounded so a huge lookahead can't run
    # unbounded. Per-call timeout is _TRANSLATE_CALL_TIMEOUT; allow a couple of
    # sequential calls per paragraph plus slack.
    deadline = _TRANSLATE_CALL_TIMEOUT * 3 + 5.0
    # NOTE: we deliberately do NOT use `with ThreadPoolExecutor(...) as pool`.
    # The context manager's __exit__ calls shutdown(wait=True), which would block
    # on any still-running task — reintroducing the exact "hangs forever" bug we
    # are fixing. Instead we shut down without waiting and cancel queued tasks, so
    # this function always returns within `deadline`. Per-call network timeouts
    # ensure abandoned worker threads die on their own shortly after.
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {pool.submit(_process, p_idx): p_idx for p_idx in range(n_paras)}
        try:
            for future in as_completed(futures, timeout=deadline):
                try:
                    p_idx, para_text, lines = future.result()
                    translated_paragraphs[p_idx] = para_text
                    translated_lines[p_idx] = lines
                except Exception as exc:
                    print(f"Paragraph translation task failed: {exc}")
        except FuturesTimeoutError:
            done = sum(1 for f in futures if f.done())
            print(f"translate_with_alignment hit {deadline:.0f}s cap; {done}/{n_paras} paragraphs done, rest left empty")
    finally:
        # Don't wait on in-flight tasks; cancel anything not yet started.
        pool.shutdown(wait=False, cancel_futures=True)

    return translated_paragraphs, translated_lines


@app.route('/api/transcript', methods=['POST'])
def get_transcript():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    video_url = data.get('url')
    from_lang = data.get('from_lang', 'en')

    if not video_url or not isinstance(video_url, str):
        return jsonify({"error": "URL is required"}), 400
    if not isinstance(from_lang, str) or not from_lang.strip():
        return jsonify({"error": "from_lang must be a language code string"}), 400

    video_id = extract_video_id(video_url)
    if not video_id:
        return jsonify({"error": "Could not parse a YouTube video ID from the provided URL"}), 400

    try:
        snippets, paragraphs = get_cached_processed_snippets(video_id, from_lang)
    except TranscriptTranslationError as e:
        # The transcript needed manual translation into from_lang but the
        # translator no-op'd (outage / cooldown). Not cached, so a retry can
        # succeed — surface a retryable message rather than a hard error.
        print(f"Transcript translation unavailable for {video_id}: {e}")
        return jsonify({
            "error": "We couldn't finish translating this video's subtitles right now. "
                     "Please try again in a moment.",
        }), 503
    except Exception as e:
        # Map to an actionable message + status. YouTube blocks datacenter IPs,
        # so on hosts like Render a "blocked" error almost always means the
        # proxy is missing/misconfigured rather than the video being unavailable.
        msg = str(e)
        low = msg.lower()
        print(f"Error fetching transcript for {video_id}: {type(e).__name__}: {msg}")

        if "proxy credentials are not configured" in low:
            return jsonify({
                "error": "Transcript fetching is temporarily unavailable (server proxy not configured).",
            }), 503
        # Parenthesized so precedence is explicit (and binds tighter than or).
        if "blocked" in low or ("ip" in low and "block" in low):
            return jsonify({
                "error": "YouTube is currently blocking transcript requests from the server. "
                         "Please try again in a moment.",
            }), 503
        # The pivot-impossible case: the requested language isn't offered by the
        # video and YouTube can't auto-translate into it. Give a clear message.
        if "not translatable" in low or ("translation language" in low and "not available" in low):
            return jsonify({
                "error": "This video's subtitles can't be provided in the requested language. "
                         "Try a different language pairing or another video.",
            }), 404
        if "disabled" in low or "no transcript" in low or "transcriptsdisabled" in low:
            return jsonify({
                "error": "This video doesn't have subtitles available for the requested language.",
            }), 404
        if "unavailable" in low or "no longer available" in low:
            return jsonify({
                "error": "This video is unavailable. Please check the link and try another video.",
            }), 404
        return jsonify({
            "error": "We couldn't fetch a transcript for this video. It may have "
                     "subtitles disabled, be unavailable, or not offer the requested language.",
        }), 502

    if not snippets:
        return jsonify({
            "error": "No usable subtitles were found for this video in the requested language.",
        }), 404

    return jsonify({
        "video_id": video_id,
        "snippets": snippets,
        "paragraphs": paragraphs,
        "from_lang": from_lang,
    })


@app.route('/api/translate', methods=['POST'])
def translate_text():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    paragraphs = data.get('paragraphs')
    lines_by_paragraph = data.get('lines')  # optional nested list, per paragraph
    from_lang = data.get('from_lang', 'en')
    to_lang = data.get('to_lang', 'es')

    if not paragraphs or not isinstance(paragraphs, list):
        return jsonify({"error": "paragraphs (list of strings) is required"}), 400
    if not all(isinstance(p, str) for p in paragraphs):
        return jsonify({"error": "every element of paragraphs must be a string"}), 400
    if not isinstance(from_lang, str) or not isinstance(to_lang, str):
        return jsonify({"error": "from_lang and to_lang must be language code strings"}), 400

    # Alignment is only requested when `lines` is a nested list matching the
    # paragraph count AND each entry is itself a list of strings.
    want_alignment = (
        isinstance(lines_by_paragraph, list)
        and len(lines_by_paragraph) == len(paragraphs)
        and all(
            isinstance(group, list) and all(isinstance(ln, str) for ln in group)
            for group in lines_by_paragraph
        )
    )

    # Whether a single paragraph's translation came back un-translated (source
    # unchanged) — used to skip caching failed work in BOTH paths.
    def _para_untranslated(source, translation):
        s = (source or "").strip()
        if not s:
            return False
        return (translation or "").strip() == s

    try:
        if want_alignment:
            # PER-PARAGRAPH caching. Each paragraph's translation is a pure
            # function of (from, to, its own text, its own lines) — verified: no
            # cross-paragraph context in translate_with_alignment — so caching
            # per paragraph makes the same paragraph a HIT regardless of which
            # lookahead/scrub batch it arrives in, with identical output.
            n = len(paragraphs)
            para_keys = [
                generate_paragraph_cache_key(from_lang, to_lang, paragraphs[i], lines_by_paragraph[i])
                for i in range(n)
            ]

            cached_by_idx = {}
            if redis_client:
                try:
                    raw_vals = redis_client.mget(para_keys)
                    for i, raw in enumerate(raw_vals):
                        if raw:
                            entry = json.loads(raw)  # {"p": <str>, "l": [<str>...]}
                            cached_by_idx[i] = entry
                except Exception as e:
                    print(f"Redis mget error: {e}. Proceeding without cache.")

            miss_indices = [i for i in range(n) if i not in cached_by_idx]
            all_hit = not miss_indices

            translated_paragraphs = [""] * n
            translated_lines = [[] for _ in range(n)]

            # Fill from cache.
            for i, entry in cached_by_idx.items():
                translated_paragraphs[i] = entry.get("p", "")
                tl = entry.get("l", [])
                translated_lines[i] = tl if isinstance(tl, list) else []

            # Translate only the misses (preserving original indices).
            if miss_indices:
                miss_paras = [paragraphs[i] for i in miss_indices]
                miss_lines = [lines_by_paragraph[i] for i in miss_indices]
                m_paras, m_lines = translate_with_alignment(
                    miss_paras, miss_lines, to_lang, from_lang
                )
                writes = {}
                for j, i in enumerate(miss_indices):
                    p_txt = m_paras[j] if j < len(m_paras) else ""
                    l_chunks = m_lines[j] if j < len(m_lines) else []
                    translated_paragraphs[i] = p_txt
                    translated_lines[i] = l_chunks
                    # Cache only genuinely-translated paragraphs (skip empty /
                    # deadline-truncated / source-identical) so we never pin a
                    # failed or un-translated paragraph for the whole TTL. This
                    # is a per-paragraph version of the old whole-batch guard,
                    # and strictly better: one bad paragraph no longer blocks
                    # caching its good siblings.
                    if (p_txt or "").strip() and not _para_untranslated(paragraphs[i], p_txt):
                        writes[para_keys[i]] = json.dumps({"p": p_txt, "l": l_chunks}, ensure_ascii=False)
                if redis_client and writes:
                    try:
                        pipe = redis_client.pipeline()
                        for k, v in writes.items():
                            pipe.setex(k, REDIS_TTL_SECONDS, v)
                        pipe.execute()
                    except Exception as e:
                        print(f"Redis per-paragraph setex error: {e}. Continuing without cache.")

            payload = {
                "translated_paragraphs": translated_paragraphs,
                "translated_lines": translated_lines,
                "cache_hit": all_hit,
            }
            return jsonify(payload)

        # Non-alignment path: keep the whole-batch key. translate_paragraphs
        # joins all paragraphs for cross-paragraph context, so its output IS
        # batch-dependent and must not be split into per-paragraph keys.
        cache_key = generate_cache_key(from_lang, to_lang, paragraphs, None)
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

        translated_paragraphs = translate_paragraphs(paragraphs, to_lang, from_lang)
        payload = {"translated_paragraphs": translated_paragraphs}

        # Don't cache a failed translation. When the whole cascade fails,
        # translate_paragraphs returns the source text unchanged; caching that
        # for REDIS_TTL_SECONDS would serve un-translated text for 24h even
        # after the provider recovers. Skip the write when nothing was
        # translated (all non-empty paragraphs came back byte-identical).
        def _looks_untranslated(sources, translations):
            non_empty = [(s or "").strip() for s in sources if (s or "").strip()]
            if not non_empty:
                return False
            return all(
                (t or "").strip() == (s or "").strip()
                for s, t in zip(sources, translations)
                if (s or "").strip()
            )

        translation_failed = _looks_untranslated(paragraphs, translated_paragraphs)

        if redis_client and not translation_failed:
            try:
                redis_client.setex(cache_key, REDIS_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                print(f"Redis setex error: {e}. Continuing without cache.")

        payload["cache_hit"] = False
        return jsonify(payload)

    except Exception as e:
        print(f"Translate Error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500

# ── Progress Endpoints ───────────────────────────────────────────────────

# Bound every Supabase REST call so a slow/hung Supabase can't pin a Flask
# worker indefinitely. (connect timeout, read timeout)
SUPABASE_TIMEOUT = (5, 15)


def _sb_get(table, params=None):
    """GET from Supabase REST API."""
    resp = _HTTP_SESSION.get(f"{SUPABASE_REST_URL}/{table}", headers=SUPABASE_HEADERS, params=params or {}, timeout=SUPABASE_TIMEOUT)
    if not resp.ok:
        raise Exception(f"Supabase GET {table} failed ({resp.status_code}): {resp.text}")
    return resp.json()

def _sb_post(table, data, extra_headers=None, params=None):
    """POST to Supabase REST API."""
    headers = {**SUPABASE_HEADERS, **(extra_headers or {})}
    resp = _HTTP_SESSION.post(f"{SUPABASE_REST_URL}/{table}", headers=headers, json=data, params=params or {}, timeout=SUPABASE_TIMEOUT)
    if not resp.ok:
        raise Exception(f"Supabase POST {table} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _sb_patch(table, data, params=None):
    """PATCH (update) rows in Supabase REST API."""
    headers = {**SUPABASE_HEADERS}
    resp = _HTTP_SESSION.patch(f"{SUPABASE_REST_URL}/{table}", headers=headers, json=data, params=params or {}, timeout=SUPABASE_TIMEOUT)
    if not resp.ok:
        raise Exception(f"Supabase PATCH {table} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _sb_delete(table, params=None):
    """DELETE rows from Supabase REST API."""
    headers = {**SUPABASE_HEADERS}
    resp = _HTTP_SESSION.delete(f"{SUPABASE_REST_URL}/{table}", headers=headers, params=params or {}, timeout=SUPABASE_TIMEOUT)
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

    try:
        result = _sb_post("videos", row)
    except Exception as e:
        # Concurrent first-view of the same video: two requests both saw no row
        # and both tried to insert. A UNIQUE(youtube_id) constraint makes the
        # loser fail — re-read the row the winner created rather than 500.
        existing = _sb_get("videos", {"select": "id", "youtube_id": f"eq.{youtube_id}"})
        if existing:
            return existing[0]["id"]
        raise e

    if not result:
        # PATCH/POST returned no representation — fall back to a read.
        existing = _sb_get("videos", {"select": "id", "youtube_id": f"eq.{youtube_id}"})
        return existing[0]["id"] if existing else None
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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


@app.route("/api/progress/upsert", methods=["POST"])
@require_auth
def upsert_progress():
    """Create or update a user's progress on a specific video + language pair."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    youtube_id = data.get("youtube_id")
    transcript_language = data.get("transcript_language")
    translation_language = data.get("translation_language")
    title = data.get("title")

    if not youtube_id or not transcript_language or not translation_language:
        return jsonify({"error": "youtube_id, transcript_language, and translation_language are required"}), 400

    # Coerce + clamp the counters so a malformed client can't write negative or
    # non-integer progress that later renders as impossible percentages.
    def _non_negative_int(value, default=0):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    current_line_index = _non_negative_int(data.get("current_line_index", 0))
    total_lines = _non_negative_int(data.get("total_lines", 0))
    if total_lines and current_line_index > total_lines:
        current_line_index = total_lines

    try:
        video_id = _ensure_video(youtube_id, title=title)
        if not video_id:
            return jsonify({"error": "Failed to resolve video"}), 500

        # Videos always restart from the beginning on entry, so we no longer keep
        # a "resume point". Instead current_line_index is a high-water mark: the
        # FARTHEST line the user has ever reached in this video/language pair. Read
        # the previous value and only ever advance it, so starting over and
        # quitting early can't shrink the recorded progress shown on the dashboard.
        existing = _sb_get("user_progress", {
            "select": "current_line_index,total_lines",
            "user_id": f"eq.{g.user_id}",
            "video_id": f"eq.{video_id}",
            "transcript_language": f"eq.{transcript_language}",
            "translation_language": f"eq.{translation_language}",
            "limit": "1",
        })
        prev = existing[0] if existing else None
        prev_max = _non_negative_int(prev.get("current_line_index", 0)) if prev else 0
        farthest_line = max(prev_max, current_line_index)

        row = {
            "user_id": g.user_id,
            "video_id": video_id,
            "transcript_language": transcript_language,
            "translation_language": translation_language,
            "current_line_index": farthest_line,
            "total_lines": total_lines or (_non_negative_int(prev.get("total_lines", 0)) if prev else 0),
            "last_accessed_at": datetime.now(timezone.utc).isoformat(),
        }

        result = _sb_post("user_progress", row,
            extra_headers={"Prefer": "return=representation,resolution=merge-duplicates"},
            params={"on_conflict": "user_id,video_id,transcript_language,translation_language"},
        )

        return jsonify({"progress": result[0] if result else None})
    except Exception as e:
        print(f"POST /api/progress/upsert error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
                    # Students must not see the class code (enrollment secret).
                    cls.pop("class_code", None)
                    cls["joined_at"] = e.get("joined_at")
                    classes.append(cls)
            return jsonify({"classes": classes, "role": "student"})
    except Exception as e:
        print(f"GET /api/classes error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
            # The class code is the enrollment secret — only the teacher who owns
            # the class should ever see it. `select=*` above pulls it in, so drop
            # it from the payload for enrolled students.
            cls.pop("class_code", None)

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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


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
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


# ── Assignment Endpoints ─────────────────────────────────────────────────
# Teachers assign a YouTube video to whole classes and/or individual students,
# with an optional due date. Students see their assignments per class, resume
# where they left off, and must complete the video linearly (no skipping).
# Assignment progress is tracked separately from user_progress so it never
# shows up in the personal dashboard.

def _get_role(user_id):
    profile = _sb_get("user_profiles", {"select": "user_role", "user_id": f"eq.{user_id}"})
    return profile[0].get("user_role") if profile else None


def _names_for_users(user_ids):
    """Map a set of user ids -> user_name. Assignment FKs point at auth.users,
    not user_profiles, so we can't embed the name via PostgREST — look it up.
    Returns {} for an empty/None input set."""
    ids = {u for u in (user_ids or set()) if u}
    if not ids:
        return {}
    profiles = _sb_get("user_profiles", {"select": "user_id, user_name", "user_id": f"in.({','.join(ids)})"})
    return {p["user_id"]: p.get("user_name") for p in profiles}


def _teacher_owns_class(class_id, teacher_id):
    rows = _sb_get("classes", {"select": "class_id", "class_id": f"eq.{class_id}", "teacher_id": f"eq.{teacher_id}"})
    return bool(rows)


# Max active-practice seconds accepted from a single progress save. The client
# saves once per completed line, so this bounds how much a single interval can
# contribute and neutralizes idle time or crafted requests.
MAX_ASSIGNMENT_ELAPSED_DELTA = 300

# Max submission attempts accepted from a single progress save. One save happens
# per line advance, so this bounds how many attempts a single line can add and
# neutralizes crafted requests.
MAX_ASSIGNMENT_ATTEMPTS_DELTA = 100


def _assignment_student_ids(assignment_id):
    """Expand an assignment's targets into the concrete set of student ids.
    Whole-class targets expand to the class's current roster (so students who
    join later still get class-wide assignments); individual targets add just
    that student. Returns a set of user-id strings.
    """
    targets = _sb_get("assignment_targets", {
        "select": "class_id, student_id",
        "assignment_id": f"eq.{assignment_id}",
    })
    student_ids = set()
    class_wide = set()
    for t in targets:
        if t.get("student_id"):
            student_ids.add(t["student_id"])
        else:
            class_wide.add(t["class_id"])
    for class_id in class_wide:
        roster = _sb_get("student_classes", {"select": "student_id", "class_id": f"eq.{class_id}"})
        for r in roster:
            if r.get("student_id"):
                student_ids.add(r["student_id"])
    return student_ids


def _student_sees_assignment(assignment_id, student_id):
    return student_id in _assignment_student_ids(assignment_id)


@app.route("/api/assignments", methods=["POST"])
@require_auth
def create_assignment():
    """Create an assignment (teacher only).

    Body:
      url                 (required) YouTube URL or video id
      title               (optional) label; falls back to youtube id
      transcript_language / translation_language (optional, default en/es)
      instructions        (optional)
      due_date            (optional ISO8601 string)
      class_ids           (optional list) whole classes to assign to
      student_targets     (optional list of {class_id, student_id}) individual
                          students within a class
    At least one of class_ids / student_targets is required. Every referenced
    class must be owned by the requesting teacher.
    """
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    if _get_role(g.user_id) != "teacher":
        return jsonify({"error": "Only teachers can create assignments"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    url = data.get("url")
    if not url or not isinstance(url, str):
        return jsonify({"error": "A YouTube URL is required"}), 400
    youtube_id = extract_video_id(url)
    if not youtube_id:
        return jsonify({"error": "Could not parse a YouTube video ID from the provided URL"}), 400

    transcript_language = (data.get("transcript_language") or "en").strip() or "en"
    translation_language = (data.get("translation_language") or "es").strip() or "es"
    instructions = (data.get("instructions") or "").strip() or None
    title = (data.get("title") or "").strip() or None
    due_date = (data.get("due_date") or "").strip() or None

    class_ids = data.get("class_ids") or []
    student_targets = data.get("student_targets") or []
    if not isinstance(class_ids, list) or not isinstance(student_targets, list):
        return jsonify({"error": "class_ids and student_targets must be lists"}), 400
    if not class_ids and not student_targets:
        return jsonify({"error": "Select at least one class or student to assign to"}), 400

    try:
        # Authorize every referenced class against the requesting teacher.
        referenced_classes = set(str(c) for c in class_ids)
        for st in student_targets:
            if not isinstance(st, dict) or not st.get("class_id") or not st.get("student_id"):
                return jsonify({"error": "Each student target needs a class_id and student_id"}), 400
            referenced_classes.add(str(st["class_id"]))
        for class_id in referenced_classes:
            if not _teacher_owns_class(class_id, g.user_id):
                return jsonify({"error": "You can only assign to your own classes"}), 403

        video_id = _ensure_video(youtube_id, title=title)
        if not video_id:
            return jsonify({"error": "Failed to resolve video"}), 500

        assignment = _sb_post("assignments", {
            "teacher_id": g.user_id,
            "video_id": video_id,
            "youtube_id": youtube_id,
            "title": title,
            "transcript_language": transcript_language,
            "translation_language": translation_language,
            "instructions": instructions,
            "due_date": due_date,
            "is_active": True,
        })
        assignment_id = assignment[0]["assignment_id"]

        # Build target rows: whole classes + individual students.
        target_rows = [{"assignment_id": assignment_id, "class_id": str(c)} for c in class_ids]
        for st in student_targets:
            target_rows.append({
                "assignment_id": assignment_id,
                "class_id": str(st["class_id"]),
                "student_id": str(st["student_id"]),
            })
        if target_rows:
            _sb_post("assignment_targets", target_rows)

        return jsonify({"assignment": assignment[0]}), 201
    except Exception as e:
        print(f"POST /api/assignments error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


@app.route("/api/assignments", methods=["GET"])
@require_auth
def list_assignments():
    """List assignments for the caller.

    Teachers: assignments they created (with target + completion counts).
    Students: assignments targeted at them, with their own progress. Optional
    ?class_id= filters to a single class.
    """
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    role = _get_role(g.user_id)
    class_filter = request.args.get("class_id")

    try:
        if role == "teacher":
            # When scoped to a class (ClassView), limit to assignments that
            # target that class — otherwise a teacher viewing one class would
            # see every assignment they've ever made across all classes.
            class_assignment_ids = None
            if class_filter:
                ct = _sb_get("assignment_targets", {
                    "select": "assignment_id",
                    "class_id": f"eq.{class_filter}",
                })
                class_assignment_ids = {t["assignment_id"] for t in ct}
                if not class_assignment_ids:
                    return jsonify({"assignments": [], "role": "teacher"})

            params = {
                "select": "*, videos(youtube_id, title, thumbnail_url)",
                "teacher_id": f"eq.{g.user_id}",
                "is_active": "eq.true",
                "order": "created_at.desc",
            }
            if class_assignment_ids is not None:
                params["assignment_id"] = f"in.({','.join(class_assignment_ids)})"
            assignments = _sb_get("assignments", params)
            result = []
            for a in assignments:
                targeted = _assignment_student_ids(a["assignment_id"])
                prog = _sb_get("assignment_progress", {
                    "select": "student_id, completed",
                    "assignment_id": f"eq.{a['assignment_id']}",
                })
                completed = sum(1 for p in prog if p.get("completed"))
                a["assigned_count"] = len(targeted)
                a["completed_count"] = completed
                a["started_count"] = len(prog)
                result.append(a)
            return jsonify({"assignments": result, "role": "teacher"})

        # Student view: find assignments they're a target of.
        # Gather assignment ids from individual targets + their classes.
        enrollments = _sb_get("student_classes", {"select": "class_id", "student_id": f"eq.{g.user_id}"})
        my_class_ids = {e["class_id"] for e in enrollments}

        direct = _sb_get("assignment_targets", {"select": "assignment_id, class_id", "student_id": f"eq.{g.user_id}"})
        assignment_ids = {t["assignment_id"] for t in direct}
        assignment_class = {t["assignment_id"]: t["class_id"] for t in direct}

        if my_class_ids:
            in_list = ",".join(my_class_ids)
            class_targets = _sb_get("assignment_targets", {
                "select": "assignment_id, class_id, student_id",
                "class_id": f"in.({in_list})",
            })
            for t in class_targets:
                if t.get("student_id"):
                    continue  # individual target handled above
                assignment_ids.add(t["assignment_id"])
                assignment_class.setdefault(t["assignment_id"], t["class_id"])

        if not assignment_ids:
            return jsonify({"assignments": [], "role": "student"})

        in_ids = ",".join(assignment_ids)
        # NOTE: teacher_id references auth.users (not user_profiles), so we can't
        # embed the teacher name via a PostgREST FK join here — look it up
        # separately below.
        assignments = _sb_get("assignments", {
            "select": "*, videos(youtube_id, title, thumbnail_url)",
            "assignment_id": f"in.({in_ids})",
            "is_active": "eq.true",
            "order": "due_date.asc.nullslast",
        })

        # Attach this student's progress.
        my_prog = _sb_get("assignment_progress", {
            "select": "*",
            "student_id": f"eq.{g.user_id}",
            "assignment_id": f"in.({in_ids})",
        })
        prog_by_assignment = {p["assignment_id"]: p for p in my_prog}

        teacher_names = _names_for_users({a.get("teacher_id") for a in assignments})

        result = []
        for a in assignments:
            aid = a["assignment_id"]
            cls_id = assignment_class.get(aid)
            if class_filter and cls_id != class_filter:
                continue
            a["class_id"] = cls_id
            a["progress"] = prog_by_assignment.get(aid)
            a["teacher_name"] = teacher_names.get(a.get("teacher_id"))
            result.append(a)
        return jsonify({"assignments": result, "role": "student"})
    except Exception as e:
        print(f"GET /api/assignments error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


@app.route("/api/assignments/<assignment_id>", methods=["GET"])
@require_auth
def get_assignment_detail(assignment_id):
    """Assignment detail. Teacher (owner) sees per-student completion; a
    targeted student sees the assignment plus their own progress."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        rows = _sb_get("assignments", {
            "select": "*, videos(youtube_id, title, thumbnail_url)",
            "assignment_id": f"eq.{assignment_id}",
        })
        if not rows:
            return jsonify({"error": "Assignment not found"}), 404
        assignment = rows[0]
        assignment["teacher_name"] = _names_for_users({assignment.get("teacher_id")}).get(assignment.get("teacher_id"))
        is_teacher = assignment["teacher_id"] == g.user_id

        if is_teacher:
            student_ids = _assignment_student_ids(assignment_id)
            prog = _sb_get("assignment_progress", {"select": "*", "assignment_id": f"eq.{assignment_id}"})
            prog_by_student = {p["student_id"]: p for p in prog}
            students = []
            if student_ids:
                in_ids = ",".join(student_ids)
                profiles = _sb_get("user_profiles", {"select": "user_id, user_name", "user_id": f"in.({in_ids})"})
                name_by_id = {p["user_id"]: p.get("user_name") for p in profiles}
                for sid in student_ids:
                    p = prog_by_student.get(sid)
                    students.append({
                        "student_id": sid,
                        "user_name": name_by_id.get(sid) or "Student",
                        "completed": bool(p and p.get("completed")),
                        "current_line_index": (p or {}).get("current_line_index", 0),
                        "total_lines": (p or {}).get("total_lines", 0),
                        "active_seconds": (p or {}).get("active_seconds", 0),
                        "total_attempts": (p or {}).get("total_attempts", 0),
                        "completed_at": (p or {}).get("completed_at"),
                        "started": p is not None,
                    })
            students.sort(key=lambda s: s["user_name"].lower())
            return jsonify({"assignment": assignment, "students": students, "is_teacher": True})

        # Student: must be a target.
        if not _student_sees_assignment(assignment_id, g.user_id):
            return jsonify({"error": "Access denied"}), 403
        prog = _sb_get("assignment_progress", {
            "select": "*", "assignment_id": f"eq.{assignment_id}", "student_id": f"eq.{g.user_id}",
        })
        assignment["progress"] = prog[0] if prog else None
        return jsonify({"assignment": assignment, "is_teacher": False})
    except Exception as e:
        print(f"GET /api/assignments/{assignment_id} error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


@app.route("/api/assignments/<assignment_id>", methods=["DELETE"])
@require_auth
def delete_assignment(assignment_id):
    """Delete an assignment (owning teacher only)."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    try:
        rows = _sb_get("assignments", {"select": "assignment_id, teacher_id", "assignment_id": f"eq.{assignment_id}"})
        if not rows:
            return jsonify({"error": "Assignment not found"}), 404
        if rows[0]["teacher_id"] != g.user_id:
            return jsonify({"error": "Only the assigning teacher can delete this assignment"}), 403

        _sb_delete("assignment_targets", {"assignment_id": f"eq.{assignment_id}"})
        _sb_delete("assignment_progress", {"assignment_id": f"eq.{assignment_id}"})
        _sb_delete("assignments", {"assignment_id": f"eq.{assignment_id}"})
        return jsonify({"message": "Assignment deleted"})
    except Exception as e:
        print(f"DELETE /api/assignments/{assignment_id} error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


@app.route("/api/assignments/<assignment_id>/progress", methods=["POST"])
@require_auth
def upsert_assignment_progress(assignment_id):
    """Save a student's progress on an assignment. Enforces no-skip: the stored
    max_line_reached only ever advances by the allowed step, and completion is
    only accepted once the last line is reached."""
    if not supabase_ready:
        return jsonify({"error": "Database not configured"}), 500

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    if not _student_sees_assignment(assignment_id, g.user_id):
        return jsonify({"error": "Access denied"}), 403

    def _nn_int(v, default=0):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return default

    incoming_line = _nn_int(data.get("current_line_index", 0))
    total_lines = _nn_int(data.get("total_lines", 0))
    # Active practice time: the client reports the seconds elapsed since its last
    # save. Cap each delta so an idle gap (student walked away, tab left open)
    # can't inflate the accumulated total past a plausible per-interval maximum.
    elapsed_seconds = min(_nn_int(data.get("elapsed_seconds", 0)), MAX_ASSIGNMENT_ELAPSED_DELTA)
    # Submission attempts: the client reports how many answer submissions it made
    # for the line it just cleared. Capped per save, same rationale as above.
    attempts = min(_nn_int(data.get("attempts", 0)), MAX_ASSIGNMENT_ATTEMPTS_DELTA)

    try:
        existing = _sb_get("assignment_progress", {
            "select": "*", "assignment_id": f"eq.{assignment_id}", "student_id": f"eq.{g.user_id}",
        })
        prev = existing[0] if existing else None
        prev_max = prev.get("max_line_reached", 0) if prev else 0
        prev_active = prev.get("active_seconds", 0) if prev else 0
        prev_attempts = prev.get("total_attempts", 0) if prev else 0

        if total_lines and incoming_line > total_lines:
            incoming_line = total_lines

        # No-skip: a student may only be as far as one line past their previous
        # furthest point. This prevents jumping ahead via crafted requests.
        allowed_max = prev_max + 1
        new_max = min(max(prev_max, incoming_line), allowed_max) if prev else min(incoming_line, 1)
        new_max = max(new_max, prev_max)
        current_line = min(incoming_line, new_max)

        completed = bool(total_lines) and current_line >= total_lines
        row = {
            "assignment_id": assignment_id,
            "student_id": g.user_id,
            "current_line_index": current_line,
            "max_line_reached": new_max,
            "total_lines": total_lines or (prev.get("total_lines", 0) if prev else 0),
            "active_seconds": prev_active + elapsed_seconds,
            "total_attempts": prev_attempts + attempts,
            "completed": completed or (prev.get("completed", False) if prev else False),
            "last_accessed_at": datetime.now(timezone.utc).isoformat(),
        }
        if completed and not (prev and prev.get("completed")):
            row["completed_at"] = datetime.now(timezone.utc).isoformat()

        result = _sb_post("assignment_progress", row,
            extra_headers={"Prefer": "return=representation,resolution=merge-duplicates"},
            params={"on_conflict": "assignment_id,student_id"},
        )
        return jsonify({"progress": result[0] if result else None})
    except Exception as e:
        print(f"POST /api/assignments/{assignment_id}/progress error: {e}")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


# ── Admin Cache Endpoints ────────────────────────────────────────────────

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "").strip()


@app.route('/api/admin/clear-translation-cache', methods=['POST'])
def clear_translation_cache():
    """
    Clear all translation cache entries. This is an operational/admin action
    (used after a translation-method change), NOT a per-user action, so it is
    gated behind a shared ADMIN_API_KEY secret supplied via the
    `X-Admin-Api-Key` header — not an ordinary user login. If ADMIN_API_KEY is
    unset the endpoint is disabled entirely.
    """
    if not ADMIN_API_KEY:
        return jsonify({"error": "Admin endpoint is disabled (ADMIN_API_KEY not configured)"}), 404

    supplied = request.headers.get("X-Admin-Api-Key", "")
    if not supplied or not hmac.compare_digest(supplied, ADMIN_API_KEY):
        return jsonify({"error": "Unauthorized"}), 401

    # Always clear the in-process transcript/snippet lru_caches. These are keyed
    # on (video_id, from_lang) and hold fetched + manually-translated snippets;
    # without clearing them, a translation/logic change won't take effect for an
    # already-cached video until the process restarts or the entry is evicted.
    get_cached_transcript.cache_clear()
    get_cached_processed_snippets.cache_clear()

    if not redis_client:
        return jsonify({
            "message": "Cleared in-process transcript caches (Redis not configured).",
            "entries_deleted": 0,
        })

    try:
        # KEYS is a blocking O(N) scan — fine for an occasional admin flush,
        # but use SCAN so we don't stall Redis on large keyspaces. Clear both the
        # translation cache (whole-batch + per-paragraph) and the cross-worker
        # processed-transcript cache so the version prefixes are invalidatable.
        total_deleted = 0
        for pattern in ("translate_paragraphs:*", "translate_para:*", "transcript:v1:*"):
            for key in redis_client.scan_iter(match=pattern, count=500):
                total_deleted += redis_client.delete(key)
        return jsonify({
            "message": f"Cleared {total_deleted} translation/transcript cache entries and in-process transcript caches",
            "entries_deleted": total_deleted,
        })
    except Exception as e:
        print(f"Clear cache error: {e}")
        return jsonify({"error": "Failed to clear cache"}), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    is_dev = os.environ.get("FLASK_ENV") == "development"
    app.run(host='0.0.0.0', port=port, debug=is_dev)