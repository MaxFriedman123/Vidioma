import requests
import time

BASE_URL = "http://127.0.0.1:5000" 
video_url = "https://www.youtube.com/watch?v=YICiHiU2GBU" 
from_lang = "es"
to_lang = "en"

print("--- 1. Testing Transcript Latency ---")

# Start the timer
start_time = time.time()

response = requests.post(
    f"{BASE_URL}/api/transcript", 
    json={"url": video_url, "from_lang": from_lang}
)

# Stop the timer
end_time = time.time()
elapsed_time = end_time - start_time

if response.status_code == 200:
    body = response.json()
    transcript_data = body.get("snippets", [])
    paragraphs = body.get("paragraphs", [])
    print(f"Fetched {len(transcript_data)} snippets / {len(paragraphs)} paragraphs successfully! (Took {elapsed_time:.2f} seconds)")
else:
    print(f"Error fetching transcript: {response.status_code}")
    print(response.text)
    exit()

print("\n--- 2. Testing Translation Endpoint ---")
if not paragraphs:
    print("No paragraphs available to translate. Exiting.")
    exit()

# /api/translate takes a list of paragraph strings and returns translated_paragraphs.
paragraphs_to_translate = paragraphs[:5]

start_trans_time = time.time()
translate_response = requests.post(
    f"{BASE_URL}/api/translate",
    json={
        "paragraphs": paragraphs_to_translate,
        "from_lang": from_lang,
        "to_lang": to_lang
    }
)
end_trans_time = time.time()

if translate_response.status_code == 200:
    translation_data = translate_response.json()
    translated_paragraphs = translation_data.get("translated_paragraphs", [])

    print(f"Translation Success! Got {len(translated_paragraphs)} translated paragraphs. (Took {end_trans_time - start_trans_time:.2f} seconds)")
else:
    print(f"Error translating: {translate_response.status_code}")
    print(translate_response.text)