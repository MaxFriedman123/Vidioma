# Vidioma

Vidioma is an interactive language practice app for YouTube videos. You paste a video URL, choose source and target languages, and practice translating subtitle lines while the video pauses line-by-line.

## Features

- Pulls transcript snippets for a YouTube video
- Uses language-aware transcript selection (exact, regional, auto-translate fallback)
- Lazily translates subtitle chunks during playback
- Uses fuzzy answer checking in the frontend for active recall practice
- Caches transcript and translation work to reduce repeated latency

## Stack

### Frontend

- React (`react-scripts`)
- Axios
- `react-youtube`

### Backend

- Flask + CORS
- `youtube-transcript-api`
- `deep-translator`
- Redis (optional cache layer)
- `python-dotenv`

## Repository Layout

```text
Vidioma/
  backend/
    app.py
    manual_api_smoke_test.py
    requirements.txt
  frontend/
    package.json
    src/
    public/
  README.md
```

## Local Development

### 1. Backend

From `backend/`:

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create `backend/.env` manually (there is no `.env.example` currently):

```env
# Optional Redis cache
REDIS_URL=redis://localhost:6379/0
REDIS_TTL_SECONDS=86400

# Optional until direct transcript fetch is blocked;
# required for proxy fallback behavior
WEBSHARE_USERNAME=your_webshare_username
WEBSHARE_PASSWORD=your_webshare_password

# Optional Flask runtime settings
PORT=5000
FLASK_ENV=development
```

Run the backend:

```powershell
python app.py
```

Backend default URL: `http://localhost:5000`

### 2. Frontend

From `frontend/`:

```powershell
npm install
```

Create `frontend/.env` (optional but recommended):

```env
REACT_APP_API_URL=http://localhost:5000
```

Run the frontend:

```powershell
npm start
```

Frontend default URL: `http://localhost:3000`

## API

### `POST /api/transcript`

Fetches and cleans transcript snippets for a YouTube video in `from_lang`.

Request:

```json
{
  "url": "https://www.youtube.com/watch?v=YICiHiU2GBU",
  "from_lang": "es"
}
```

Success response:

```json
{
  "video_id": "YICiHiU2GBU",
  "snippets": [
    {
      "source": "Hola a todos",
      "start": 12.34,
      "duration": 1.8
    }
  ],
  "from_lang": "es"
}
```

### `POST /api/translate`

Translates snippet objects from `from_lang` to `to_lang`.

Request:

```json
{
  "snippets": [
    {
      "source": "Hello world",
      "start": 1.2,
      "duration": 1.5
    }
  ],
  "from_lang": "en",
  "to_lang": "es"
}
```

Success response:

```json
{
  "translated_snippets": [
    {
      "source": "Hola mundo",
      "start": 1.2,
      "duration": 1.5
    }
  ],
  "cache_hit": false
}
```

## Caching Behavior

- In-memory LRU cache is used for transcript fetch and processed transcript snippets.
- Redis cache (if available) is used for `/api/translate` responses.
- If Redis is unavailable, the backend continues without Redis caching.

## Smoke Test Script

`backend/manual_api_smoke_test.py` is a manual latency/smoke check for:

- `POST /api/transcript`
- `POST /api/translate`

Run it after the backend is running:

```powershell
cd backend
python manual_api_smoke_test.py
```

## Current Limitations

- No automated test suite yet (only manual smoke testing)
- Error responses are basic and can be improved
- Input validation can be hardened further for malformed JSON payloads

## License

No license file is currently included.