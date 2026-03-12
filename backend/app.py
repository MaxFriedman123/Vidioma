import os
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
import re
from deep_translator import GoogleTranslator

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

# This function implements the multi-tiered transcript fetching strategy
def find_proper_transcript(ytt_api, video_id, fromLang):
    transcripts = ytt_api.list(video_id)

    # 1. Try to fetch direct transcript in requested language
    try:
        fetched = ytt_api.fetch(video_id, languages=[fromLang])
        if fetched.language_code == fromLang:
            transcript_snippets = []
            for i in fetched.snippets:
                transcript_snippets.append({
                    'source': i.text,
                    'start': i.start,
                    'duration': i.duration
                })
            return transcript_snippets
    except Exception as e:
        print(f"No direct transcript in {fromLang}, attempting translation: {e}")

    # 2. Try to fetch any transcript and automatically translate it
    try:
        untranslated_source_transcript = next(iter(transcripts))
        fetched = untranslated_source_transcript.translate(fromLang).fetch()
        if fetched.language_code == fromLang:
            transcript_snippets = []
            for i in fetched.snippets:
                transcript_snippets.append({
                    'source': i.text,
                    'start': i.start,
                    'duration': i.duration
                })
            return transcript_snippets
    except Exception as e:
        print(f"Translation failed, attempting similar language translation: {e}")

    # 3. Try to fetch transcript in English or Spanish and translate it
    try:
        untranslated_source_transcript = ytt_api.fetch(video_id, languages=['en', 'es'])
        transcript_snippets = []
        for i in untranslated_source_transcript.snippets:
            transcript_snippets.append({
                'source': i.text,                    
                'start': i.start,
                'duration': i.duration
            })
        return(translate_with_context(transcript_snippets, fromLang))
    except Exception as e:
        print(f"No similar language, reverting to translation with random language: {e}")

    # 4. Final fallback: Take any available transcript and manually translate it
    try:
        untranslated_source_transcript = next(iter(transcripts)).fetch()
        transcript_snippets = []
        for i in untranslated_source_transcript.snippets:
            transcript_snippets.append({
                'source': i.text,                    
                'start': i.start,
                'duration': i.duration
            })
        return(translate_with_context(transcript_snippets, fromLang))
    except Exception as e:
        print(f"Final fallback failed: {e}")
    return None

# This function translates snippets in batches to preserve context, then recombines them.
def translate_with_context(snippets, target_lang, source_lang='auto'):
    translator = GoogleTranslator(source=source_lang, target=target_lang)
    
    if len(snippets) == 1: 
        # Single snippet, translate directly
        text = snippets[0]['source']
        translated_text = translator.translate(text)
        return [{
            'source': translated_text,
            'start': snippets[0]['start'],
            'duration': snippets[0]['duration']
        }]

    # \n is the best delimiter because NMT engines treat it as a natural sentence break
    delimiter = "\n"
    max_chars = 4500 
    
    all_translated_texts = []
    current_chunk_texts = []
    current_chunk_length = 0
    
    def process_chunk(texts_to_translate):
        combined_text = delimiter.join(texts_to_translate)
        translated_combined = translator.translate(combined_text)
        return translated_combined.split(delimiter)

    # 1. Group texts into chunks
    for snippet in snippets:
        # We use dictionary access here because 'snippets' is our custom cleaned_snippets list
        text = snippet['source']    
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
        trans_text = all_translated_texts[i].strip() if i < len(all_translated_texts) else snippet['source']
        
        translated_snippets.append({
            'source': trans_text,
            'start': snippet['start'],
            'duration': snippet['duration']
        })
        
    return translated_snippets

@app.route('/api/transcript', methods=['POST'])
def get_transcript():
    try:
        print("Received request for transcript")
        data = request.get_json()
        video_url = data.get('url')
        from_lang = data.get('from_lang', 'en')

        if not video_url:
            return jsonify({"error": "URL is required"}), 400
        
        video_id = extract_video_id(video_url)
        
        # --- 1. SECURE CREDENTIAL FETCHING ---
        proxy_username = os.environ.get("WEBSHARE_USERNAME")
        proxy_password = os.environ.get("WEBSHARE_PASSWORD")
        
        if not proxy_username or not proxy_password:
            return jsonify({"error": "Proxy credentials missing from environment"}), 500

        # Use SOCKS5 (port 1080) instead of HTTP CONNECT (port 80)
        # because Webshare's HTTP proxy drops TLS handshakes through CONNECT tunnels
        socks5_url = f"socks5://{proxy_username}-rotate:{proxy_password}@p.webshare.io:1080"
        proxy_config = GenericProxyConfig(
            http_url=socks5_url,
            https_url=socks5_url
        )
        
        # --- 2. INITIALIZE API WITH PROXIES ---
        ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
        
        # --- 3. FETCH TRANSCRIPTS ---  
        source_transcript = find_proper_transcript(ytt_api, video_id, from_lang)
        if not source_transcript:
            return jsonify({"error": "Could not fetch or translate transcript"}), 500
        cleaned_snippets = []
        
        # Filter out non-dialogue text
        for snippet in source_transcript:
            text = snippet['source'].strip()
            
            if text.startswith('[') or text.startswith('('):
                continue
            if not re.search(r'[^\W\d_]', text, re.UNICODE):
                continue
                
            cleaned_snippets.append({
                'source': text,
                'start': snippet['start'],
                'duration': snippet['duration']
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
        text = data.get('text')
        from_lang = data.get('from_lang', 'en')
        to_lang = data.get('to_lang', 'es')

        if not text:
            return jsonify({"error": "Text is required"}), 400
        
        translated_text = translate_with_context(text, to_lang, from_lang)
        return jsonify({"translated_text": translated_text})
        
    except Exception as e:
        print(f"Translate Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run()