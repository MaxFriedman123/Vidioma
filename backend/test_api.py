import requests

BASE_URL = "http://127.0.0.1:5000"

print("--- Step 1: Testing /api/transcript ---")
transcript_payload = {
    "url": "https://www.youtube.com/watch?v=FD3cN1rUOYo", 
    "from_lang": "es"
}

try:
    # 1. Fetch the raw transcript
    transcript_response = requests.post(f"{BASE_URL}/api/transcript", json=transcript_payload)
    print("Transcript Status Code:", transcript_response.status_code)
    transcript_data = transcript_response.json()
    
    if "snippets" in transcript_data and len(transcript_data["snippets"]) > 0:
        print(f"Success! Retrieved {len(transcript_data['snippets'])} lines.")
        
        # Grab the first 3 lines to test the translation endpoint
        texts_to_translate = [snippet['source'] for snippet in transcript_data["snippets"][:3]]
        
        print("\n--- Step 2: Testing /api/translate ---")
        translate_payload = {
            "text": texts_to_translate,
            "from_lang": "es",
            "to_lang": "en"
        }
        
        # 2. Fetch the translations for those specific lines
        translate_response = requests.post(f"{BASE_URL}/api/translate", json=translate_payload)
        print("Translate Status Code:", translate_response.status_code)
        translate_data = translate_response.json()
        
        if "translations" in translate_data:
            print("Success! Translations received:\n")
            for i, original in enumerate(texts_to_translate):
                print(f"Original:   {original}")
                print(f"Translated: {translate_data['translations'][i]}\n")
        else:
            print("Translation Response Data:", translate_data)
            
    else:
        print("Transcript Response Data:", transcript_data)

except requests.exceptions.ConnectionError:
    print("Connection Error: Is your Flask server currently running?")
except Exception as e:
    print("An error occurred:", e)