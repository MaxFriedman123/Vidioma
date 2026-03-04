from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
import re
from deep_translator import GoogleTranslator

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
        data=request.get_json()
        video_url=data.get('url')
        fromLang=data.get('from_lang', 'en')
        toLang=data.get('to_lang', 'es')

        if not video_url:
            return jsonify({"error": "URL is required"}), 400
        
        video_id=extract_video_id(video_url)
        transcripts = YouTubeTranscriptApi().list(video_id, cookies='cookies.txt')
        source_transcript = None

        for transcript in transcripts:
            if transcript.language_code == fromLang:
                source_transcript = transcript.fetch()
                break
        if not source_transcript:
            source_transcript = next(iter(transcripts)).fetch()
           
        cleaned_snippets = []
        for i in range(len(source_transcript)):
            text=source_transcript[i].text
            if text.startswith('[') or text.startswith('('):
                continue
            if not re.search('[a-zA-Z\u00C0-\u017F]', text):
                continue
            cleaned_snippets.append({
                'source': text,
                'start': source_transcript[i].start,
                'duration': source_transcript[i].duration
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
        data=request.get_json()
        text=data.get('text')
        fromLang=data.get('from_lang', 'en')
        toLang=data.get('to_lang', 'es')

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