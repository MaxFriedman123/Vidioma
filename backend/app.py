import os
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
import re
import json
import hashlib
import redis
from deep_translator import GoogleTranslator
from functools import lru_cache

app = Flask(__name__)
CORS(app)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_TTL_SECONDS = int(os.environ.get("REDIS_TTL_SECONDS", "86400"))

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
    Fetch a transcript for the requested language.

    Returns:
        (transcript_data, is_correct_lang)

    - transcript_data: fetched transcript snippets
    - is_correct_lang:
        True  -> transcript is already in the requested language
        False -> transcript is from another language and should be manually translated
    """
    proxy_username = os.environ.get("WEBSHARE_USERNAME")
    proxy_password = os.environ.get("WEBSHARE_PASSWORD")

    if not proxy_username or not proxy_password:
        raise ValueError(
            "Proxy credentials are not configured"
        )

    socks5_url = f"socks5://{proxy_username}-rotate:{proxy_password}@p.webshare.io:1080"
    proxy_config = GenericProxyConfig(http_url=socks5_url, https_url=socks5_url)

    ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
    available_transcripts = ytt_api.list(video_id)

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

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    is_dev = os.environ.get("FLASK_ENV") == "development"
    app.run(host='0.0.0.0', port=port, debug=is_dev)