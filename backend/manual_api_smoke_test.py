import requests

BASE_URL = "https://vidioma.onrender.com"

print("--- Step 1: Testing /api/transcript ---")
transcript_payload = {
    "url": "https://www.youtube.com/watch?v=FD3cN1rUOYo",
    "from_lang": "en",
    "to_lang": "es"
}

try:
    print(f"Pinging {BASE_URL}/api/transcript...")
    transcript_response = requests.post(f"{BASE_URL}/api/transcript", json=transcript_payload)
    print("Transcript Status Code:", transcript_response.status_code)
    transcript_data = transcript_response.json()

    if "snippets" in transcript_data and len(transcript_data["snippets"]) > 0:
        print(f"Success! Retrieved {len(transcript_data['snippets'])} lines.")

        texts_to_translate = [snippet["source"] for snippet in transcript_data["snippets"][:3]]

        print("\n--- Step 2: Testing /api/translate ---")
        translate_payload = {
            "text": texts_to_translate,
            "from_lang": "en",
            "to_lang": "zh-CN"
        }

        print(f"Pinging {BASE_URL}/api/translate...")
        translate_response = requests.post(f"{BASE_URL}/api/translate", json=translate_payload)
        print("Translate Status Code:", translate_response.status_code)
        translate_data = translate_response.json()

        if "translated_text" in translate_data:
            print("Success! Translations received:\n")
            for i, original in enumerate(texts_to_translate):
                print(f"Original:   {original}")
                print(f"Translated: {translate_data['translated_text'][i]}\n")
        else:
            print("Translation Response Data:", translate_data)
    else:
        print("Transcript Response Data:", transcript_data)

except requests.exceptions.ConnectionError:
    print("Connection Error: Could not reach Render. Is the server URL correct?")
except Exception as e:
    print("An error occurred:", e)
