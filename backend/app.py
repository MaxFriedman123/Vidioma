from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig
import re
from deep_translator import GoogleTranslator

app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return "Vidioma Backend is Awake - Proxies Active!"

def extract_video_id(url):
    if 'v=' in url:
        return url.split('v=')[1].split('&')[0]
    elif 'youtu.be' in url:
        return url.split('/')[-1]
    return url

@app.route('/api/transcript', methods=['POST'])
def get_transcript():
    try:
        data = request.get_json()
        video_url = data.get('url')
        fromLang = data.get('from_lang', 'en')

        if not video_url:
            return jsonify({"error": "URL is required"}), 400
        
        video_id = extract_video_id(video_url)
        
        # --- 1. SET UP WEBSHARE PROXY ---
        # Using the exact credentials from your dashboard
        proxy_config = WebshareProxyConfig(
            proxy_username="zgehmkre-1", 
            proxy_password="rxmx68c0wbym"
        )
        
        # --- 2. INITIALIZE API WITH PROXIES ---
        ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
        
        # --- 3. FETCH TRANSCRIPTS (Bypassing IP Block!) ---
        transcripts = ytt_api.list(video_id)
        
        source_transcript = None

        for transcript in transcripts:
            if transcript.language_code == fromLang:
                source_transcript = transcript.fetch()
                break
                
        if not source_transcript:
            source_transcript = next(iter(transcripts)).fetch()
            
        cleaned_snippets = []
        
        # Note: .fetch() returns dictionaries, so we use ['text'] instead of .text
        for snippet in source_transcript:
            text = snippet['text']
            
            if text.startswith('[') or text.startswith('('):
                continue
            if not re.search('[a-zA-Z\u00C0-\u017F]', text):
                continue
                
            cleaned_snippets.append({
                'source': text,
                'start': snippet['start'],
                'duration': snippet['duration']
            })
            
        return jsonify({
            "video_id": video_id,
            "snippets": cleaned_snippets,
        })
        
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/translate', methods=['POST'])
def translate_text():
    try:
        data = request.get_json()
        text = data.get('text')
        fromLang = data.get('from_lang', 'en')
        toLang = data.get('to_lang', 'es')

        if not text:
            return jsonify({"error": "Text is required"}), 400
        
        translator = GoogleTranslator(source=fromLang, target=toLang)
        translated_text = translator.translate_batch(text)
        return jsonify({"translated_text": translated_text})
        
    except Exception as e:
        print(f"Translate Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)