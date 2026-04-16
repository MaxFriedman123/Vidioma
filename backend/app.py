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

@lru_cache(maxsize=100)
def get_cached_processed_snippets(video_id, from_lang):
    """
    Cache final snippet output (already filtered and optionally translated).
    This is what /api/transcript should return directly on cache hits.
    """
    source_transcript, is_correct_lang = get_cached_transcript(video_id, from_lang)

    cleaned_snippets = []

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

        cleaned_snippets.append({
            "source": text,
            "start": start,
            "duration": duration
        })

    # Only translate when transcript is not already in requested language
    if not is_correct_lang and cleaned_snippets:
        print(f"Manually translating {video_id} to {from_lang}")
        cleaned_snippets = translate_with_context(cleaned_snippets, from_lang)

    return cleaned_snippets

# This function translates snippets in batches to preserve context, then recombines them.
def generate_cache_key(from_lang, to_lang, snippets):
    """Generate a cache key for translation results."""
    key_payload = {
        "from": from_lang.lower(),
        "to": to_lang.lower(),
        "snippets": snippets
    }
    key_raw = json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "translate:" + hashlib.sha256(key_raw.encode("utf-8")).hexdigest()

def translate_with_context(snippets, target_lang, source_lang='auto'):
    translator = GoogleTranslator(source=source_lang, target=target_lang)

    # @@<index>@@ <text> format with newline separators is used to mark snippet boundaries
    max_chars = 4500

    all_translated = {}

    def process_chunk(indexed_texts):
        if not indexed_texts:
            return

        combined = "\n".join(f"@@{idx}@@ {txt}" for idx, txt in indexed_texts)
        translated = translator.translate(combined) or ""

        # Parse translated output by markers (tolerant to whitespace)
        pattern = re.compile(r"@@\s*(\d+)\s*@@\s*")
        matches = list(pattern.finditer(translated))

        if not matches:
            # Fallback: if markers were destroyed, keep originals for this chunk
            for idx, txt in indexed_texts:
                all_translated[idx] = txt
            return

        for i, m in enumerate(matches):
            idx = int(m.group(1))
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(translated)
            text_part = translated[start:end].strip()
            all_translated[idx] = text_part

        # Fill any missing ids with original text
        for idx, txt in indexed_texts:
            if idx not in all_translated or not all_translated[idx]:
                all_translated[idx] = txt

    current_chunk = []
    current_len = 0

    for i, snippet in enumerate(snippets):
        text = snippet['source'].replace('\n', ' ').strip()
        line = f"@@{i}@@ {text}"
        add_len = len(line) + 1  # + newline

        if current_chunk and current_len + add_len > max_chars:
            process_chunk(current_chunk)
            current_chunk = []
            current_len = 0

        current_chunk.append((i, text))
        current_len += add_len

    if current_chunk:
        process_chunk(current_chunk)

    translated_snippets = []
    for i, snippet in enumerate(snippets):
        trans_text = all_translated.get(i, snippet['source'].replace('\n', ' ').strip())
        translated_snippets.append({
            'source': trans_text,
            'start': snippet['start'],
            'duration': snippet['duration']
        })

    return translated_snippets

@app.route('/api/transcript', methods=['POST'])
def get_transcript():
    try:
        data = request.get_json()
        video_url = data.get('url')
        from_lang = data.get('from_lang', 'en')

        if not video_url:
            return jsonify({"error": "URL is required"}), 400
        
        video_id = extract_video_id(video_url)
        
        # Call the cached function instead of hitting the network every time
        cleaned_snippets = get_cached_processed_snippets(video_id, from_lang)
        
        return jsonify({
            "video_id": video_id,
            "snippets": cleaned_snippets,
            "from_lang": from_lang
        })
        
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/translate', methods=['POST'])
def translate_text():
    try:
        data = request.get_json()
        snippets = data.get('snippets')
        from_lang = data.get('from_lang', 'en')
        to_lang = data.get('to_lang', 'es')
        if not snippets:
            return jsonify({"error": "Snippets are required"}), 400
        
        cache_key = generate_cache_key(from_lang, to_lang, snippets)
        cached = None
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
            except Exception as e:
                print(f"Redis get error: {e}. Proceeding without cache.")
        
        if cached:
            return jsonify({"translated_snippets": json.loads(cached), "cache_hit": True})

        translated_snippets = translate_with_context(snippets, to_lang, from_lang)
        if redis_client:
            try:
                redis_client.setex(cache_key, REDIS_TTL_SECONDS, json.dumps(translated_snippets, ensure_ascii=False))
            except Exception as e:
                print(f"Redis setex error: {e}. Continuing without cache.")

        return jsonify({"translated_snippets": translated_snippets, "cache_hit": False})

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


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    is_dev = os.environ.get("FLASK_ENV") == "development"
    app.run(host='0.0.0.0', port=port, debug=is_dev)