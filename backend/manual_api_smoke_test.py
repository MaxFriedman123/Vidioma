import requests
import time

BASE_URL = "http://127.0.0.1:5000" 
video_url = "https://www.youtube.com/watch?v=YICiHiU2GBU" 
from_lang = "en"
to_lang = "es"

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
    print("RAW SERVER RESPONSE:")
    print(response.text[:500])  # Prints the first 500 characters
    transcript_data = response.json().get("snippets", [])
    # ---------------------------
    
    print(f"Fetched {len(transcript_data)} snippets.")
    print(f"Total Time Taken: {elapsed_time:.2f} seconds")
else:
    print(f"Error fetching transcript: {response.status_code}")
    print(response.text)
    exit()

print("\n--- 2. Testing Translation Endpoint ---")
if not transcript_data:
    print("No transcript data available to translate. Exiting.")
    exit()

snippets_to_translate = transcript_data[:5]
print(f"Sending the first {len(snippets_to_translate)} snippets to the translator...")

start_trans_time = time.time()
translate_response = requests.post(
    f"{BASE_URL}/api/translate",
    json={
        "snippets": snippets_to_translate,
        "from_lang": from_lang,
        "to_lang": to_lang
    }
)
end_trans_time = time.time()

if translate_response.status_code == 200:
    translation_data = translate_response.json()
    translated_snippets = translation_data.get("translated_snippets", [])
    
    print(f"Translation Success! (Took {end_trans_time - start_trans_time:.2f} seconds)")
    for i, translated_snippet in enumerate(translated_snippets):
        original = snippets_to_translate[i]['source']
        translated = translated_snippet['source']
        print(f"EN: {original}")
        print(f"ES: {translated}\n")
else:
    print(f"Error translating: {translate_response.status_code}")
    print(translate_response.text)