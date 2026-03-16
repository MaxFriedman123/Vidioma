import os
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
import re
from deep_translator import GoogleTranslator
from functools import lru_cache

app = Flask(__name__)
CORS(app)

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
    Fetches the transcript using Webshare proxies and caches the last 100 requests.
    """
    # 1. Retrieve credentials inside the function to avoid caching sensitive data
    proxy_username = os.environ.get("WEBSHARE_USERNAME")
    proxy_password = os.environ.get("WEBSHARE_PASSWORD")
    
    # 2. Validate that credentials are present
    if not proxy_username or not proxy_password:
        raise ValueError("Proxy credentials (WEBSHARE_USERNAME and WEBSHARE_PASSWORD) are not configured")
    
    # 3. Build the Webshare proxy URL
    socks5_url = f"socks5://{proxy_username}-rotate:{proxy_password}@p.webshare.io:1080"
    proxy_config = GenericProxyConfig(http_url=socks5_url, https_url=socks5_url)
    
    # 4. Initialize the API with proxies enabled
    ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config) 
    transcripts = ytt_api.list(video_id)
    
    try:
        # 5. Try to find the exact language first
        return transcripts.find_transcript([from_lang]).fetch()
    except Exception:
        pass 
        
    # 6. If exact match fails, grab the first transcript available
    source_transcript = next(iter(transcripts))
    
    # 7. SAFETY CHECK: If we asked for 'en' and the transcript is 'en-US', just use it!
    if from_lang in source_transcript.language_code:
        return source_transcript.fetch()
        
    # 8. If it's a completely different language, use YouTube auto-translate
    return source_transcript.translate(from_lang).fetch()

# This function translates snippets in batches to preserve context, then recombines them.
def translate_with_context(snippets, target_lang, source_lang='auto'):
    translator = GoogleTranslator(source=source_lang, target=target_lang)
    
    # [GGG] is the best delimiter from my tests
    delimiter = "[GGG]"
    max_chars = 4500 
    
    all_translated_texts = []
    current_chunk_texts = []
    current_chunk_length = 0
    
    def process_chunk(texts_to_translate):
        combined_text = delimiter.join(texts_to_translate)
        translated_combined = translator.translate(combined_text)
        return re.split(r'\s*\[\s*GGG\s*\]\s*', translated_combined, flags=re.IGNORECASE)

    # 1. Group texts into chunks
    for snippet in snippets:
        text = snippet['source'].replace('\n', ' ').strip()   
        if current_chunk_length + len(text) + len(delimiter) > max_chars:
            all_translated_texts.extend(process_chunk(current_chunk_texts))
            current_chunk_texts = [text]
            current_chunk_length = len(text)
        else:
            current_chunk_texts.append(text)
            current_chunk_length += len(text) + len(delimiter)
            
    # 2. Process the final leftover chunk
    if current_chunk_texts:
        all_translated_texts.extend(process_chunk(current_chunk_texts))
        
    # 3. Map translated texts back to the original timestamps
    translated_snippets = []
    for i, snippet in enumerate(snippets):
        if i < len(all_translated_texts):
            trans_text = all_translated_texts[i].strip()
        else:
            trans_text = snippet['source'].replace('\n', ' ').strip()  # Fallback to original if translation is missing
        
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
        source_transcript = get_cached_transcript(video_id, from_lang)
        
        if not source_transcript:
            return jsonify({"error": "Could not fetch transcript"}), 500
            
        cleaned_snippets = []
        
        # Filter out non-dialogue text
        for snippet in source_transcript:
            text = snippet.text.strip() # Note: standard library uses 'text', not 'source'
            
            if text.startswith('[') or text.startswith('('):
                continue
            if not re.search(r'[^\W\d_]', text, re.UNICODE):
                continue
                
            cleaned_snippets.append({
                'source': text,
                'start': snippet.start,
                'duration': snippet.duration
            })
            
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
        
        translated_snippets = translate_with_context(snippets, to_lang, from_lang)
        return jsonify({"translated_snippets": translated_snippets})
        
    except Exception as e:
        print(f"Translate Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    is_dev = os.environ.get("FLASK_ENV") == "development"
    app.run(host='0.0.0.0', port=port, debug=is_dev)