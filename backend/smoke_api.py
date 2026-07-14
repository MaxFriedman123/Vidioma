"""Manual smoke test against a LOCALLY RUNNING backend (not a unit test).

Start the server first (`python app.py`), then run `python smoke_api.py`. It
hits the real /api/transcript + /api/translate endpoints and prints latency.
Guarded under __main__ so importing/collecting it never fires network calls
(it used to break `pytest` collection by running requests at import time).
"""
import time

import requests

BASE_URL = "http://127.0.0.1:5000"
VIDEO_URL = "https://www.youtube.com/watch?v=YICiHiU2GBU"
FROM_LANG = "es"
TO_LANG = "en"


def main():
    print("--- 1. Testing Transcript Latency ---")
    start_time = time.time()
    response = requests.post(
        f"{BASE_URL}/api/transcript",
        json={"url": VIDEO_URL, "from_lang": FROM_LANG},
    )
    elapsed_time = time.time() - start_time

    if response.status_code == 200:
        body = response.json()
        transcript_data = body.get("snippets", [])
        paragraphs = body.get("paragraphs", [])
        print(f"Fetched {len(transcript_data)} snippets / {len(paragraphs)} paragraphs "
              f"successfully! (Took {elapsed_time:.2f} seconds)")
    else:
        print(f"Error fetching transcript: {response.status_code}")
        print(response.text)
        return

    print("\n--- 2. Testing Translation Endpoint ---")
    if not paragraphs:
        print("No paragraphs available to translate. Exiting.")
        return

    paragraphs_to_translate = paragraphs[:5]
    start_trans_time = time.time()
    translate_response = requests.post(
        f"{BASE_URL}/api/translate",
        json={
            "paragraphs": paragraphs_to_translate,
            "from_lang": FROM_LANG,
            "to_lang": TO_LANG,
        },
    )
    elapsed_trans = time.time() - start_trans_time

    if translate_response.status_code == 200:
        translation_data = translate_response.json()
        translated_paragraphs = translation_data.get("translated_paragraphs", [])
        print(f"Translation Success! Got {len(translated_paragraphs)} translated "
              f"paragraphs. (Took {elapsed_trans:.2f} seconds)")
    else:
        print(f"Error translating: {translate_response.status_code}")
        print(translate_response.text)


if __name__ == "__main__":
    main()
