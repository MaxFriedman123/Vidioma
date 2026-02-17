from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
import re

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

        if not video_url:
            return jsonify({"error": "URL is required"}), 400
        
        video_id=extract_video_id(video_url)
        transcript_data=YouTubeTranscriptApi().fetch(video_id, languages=['en', 'es'])

        cleaned_snippets = []
        for line in transcript_data:
            text=line.text
            if text.startswith('[') or text.startswith('('):
                continue
            if not re.search('[a-zA-Z\u00C0-\u017F]', text):
                continue
            cleaned_snippets.append(line)
        return jsonify({
            "video_id": video_id,
            "snippets": cleaned_snippets
        })
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)