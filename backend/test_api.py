import requests
url = "http://127.0.0.1:5000/api/transcript"
payload = {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}

try:
    response = requests.post(url, json=payload)
    print("Status Code:", response.status_code)
    print("Response Data:", response.json())
except Exception as e:
    print("Is your server running? Error:", e)
