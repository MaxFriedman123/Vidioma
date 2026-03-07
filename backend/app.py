from flask import Flask, request, jsonify
from flask_cors import CORS
import re
from deep_translator import GoogleTranslator
import yt_dlp
import requests
import os

app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return "Vidioma Backend is Awake!"

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
        toLang = data.get('to_lang', 'es')

        if not video_url:
            return jsonify({"error": "URL is required"}), 400
        
        video_id = extract_video_id(video_url)
        
        # 1. Configure yt-dlp to fetch subtitles without downloading the video
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,  # FORCE it to ignore video format crashes
            'extract_flat': 'in_playlist' 
        }
        
        # If you still want to bypass age-restrictions, yt-dlp can use your cookies.txt
        if os.path.exists('cookies.txt'):
            ydl_opts['cookiefile'] = 'cookies.txt'

        # 2. Extract video info using yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        # 3. Locate the correct language transcripts
        subs = info.get('subtitles', {})
        auto_subs = info.get('automatic_captions', {})
        
        # Check manual subs first, then auto-subs. 
        # If the requested language isn't there, fallback to the first available language.
        available_langs = list(subs.keys()) + list(auto_subs.keys())
        if not available_langs:
            return jsonify({"error": "No transcripts available for this video"}), 404

        chosen_lang = fromLang if fromLang in available_langs else available_langs[0]
        tracks = subs.get(chosen_lang) or auto_subs.get(chosen_lang)

        # 4. Find the json3 formatted track (easiest to parse)
        json3_track = next((t for t in tracks if t.get('ext') == 'json3'), None)
        if not json3_track:
            return jsonify({"error": "Could not find a parseable transcript format"}), 500

        # 5. Fetch the actual transcript JSON data from YouTube
        response = requests.get(json3_track['url'])
        response.raise_for_status()
        raw_transcript = response.json()

        cleaned_snippets = []
        
        # 6. Parse the json3 events and clean the text
        for event in raw_transcript.get('events', []):
            if 'segs' not in event:
                continue
                
            # Combine the text segments
            text = "".join(seg.get('utf8', '') for seg in event['segs']).strip()
            
            # Skip empty lines, lines starting with brackets/parentheses, or non-alphabetical lines
            if not text:
                continue
            if text.startswith('[') or text.startswith('('):
                continue
            if not re.search('[a-zA-Z\u00C0-\u017F]', text):
                continue
                
            # json3 timestamps are in milliseconds, convert to seconds
            start_sec = event.get('tStartMs', 0) / 1000.0
            duration_sec = event.get('dDurationMs', 0) / 1000.0

            cleaned_snippets.append({
                'source': text.replace('\n', ' '), # Optional: remove line breaks inside segments
                'start': start_sec,
                'duration': duration_sec
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