from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
import re
from deep_translator import GoogleTranslator

app = Flask(__name__)
CORS(app)

def extract_video_id(url):
    if 'v=' in url:
        return url.split('v=')[1].split('&')[0]
    elif 'youtu.be' in url:
        return url.split('/')[-1]
    return url
@app.route('/')
def home():
    return {"message": "Welcome to the backend API!"}

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
        transcripts = YouTubeTranscriptApi().list(video_id)
        if fromLang in [t.language_code for t in transcripts]:
            source_transcript = transcripts.find_transcript([fromLang]).fetch()
        else:
            source_transcript = next(iter(transcripts)).fetch()
            for i in range(len(source_transcript)):
                source_transcript[i].text = GoogleTranslator(source=source_transcript.language_code, target=fromLang).translate(source_transcript[i].text)

        #source_transcript = YouTubeTranscriptApi().fetch(video_id, [fromLang])
        """try:
            
        except:
            # Fallback to the first available language (usually the video's native one)
            try:
                source_transcript = transcripts.find_transcript([fromLang])
            except:
                source_transcript = next(iter(transcripts))
        transcript_data = source_transcript
        if source_transcript.language_code != fromLang:
            temp_translator = GoogleTranslator(source=source_transcript.language_code, target=fromLang)
            texts = [line.text for line in transcript_data]
            source_transcript = temp_translator.translate_batch(texts)"""

        translator = GoogleTranslator(source=fromLang, target=toLang)
        texts = [line.text for line in source_transcript]
        translations = translator.translate_batch(texts)

        cleaned_snippets = []
        translated_snippets = []
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
            translated_snippets.append(translations[i])
        return jsonify({
            "video_id": video_id,
            "snippets": cleaned_snippets,
            "translated_snippets": translated_snippets
        })
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)