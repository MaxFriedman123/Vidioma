# Vidioma

Vidioma is an interactive language practice app for YouTube videos. You paste a video URL, choose source and target languages, and practice translating subtitle lines while the video pauses line-by-line.

## Features

- Pulls transcript snippets for a YouTube video
- Uses language-aware transcript selection (exact, regional, auto-translate fallback)
- Lazily translates subtitle chunks during playback
- Uses fuzzy answer checking in the frontend for active recall practice
- Caches transcript and translation work to reduce repeated latency
- Optional accounts (Supabase): saved per-video progress and a resume dashboard
- Optional classes: teachers create classes and share a join code; students enroll
- Works fully anonymously when Supabase is not configured (progress kept in localStorage)

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

# Optional: DeepL is used as the primary translator when a key is present.
# A key ending in ':fx' uses the free tier endpoint.
DEEPL_API_KEY=your_deepl_key

# Optional: Supabase powers auth, saved progress, and classes. If unset, those
# features are disabled and the app still works for anonymous line-by-line practice.
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your_service_role_key
SUPABASE_JWT_SECRET=your_hs256_jwt_secret   # only needed for HS256 fallback

# Optional: enables POST /api/admin/clear-translation-cache. When unset, that
# endpoint is disabled (404). Send the value in the X-Admin-Api-Key header.
ADMIN_API_KEY=some_long_random_secret

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

# Optional: enables the login / signup / dashboard / classes UI. Must point at
# the same Supabase project the backend uses. If unset, auth features are
# disabled and the app runs in anonymous practice mode.
REACT_APP_SUPABASE_URL=https://your-project.supabase.co
REACT_APP_SUPABASE_ANON_KEY=your_anon_key
```

Run the frontend:

```powershell
npm start
```

Frontend default URL: `http://localhost:3000`

## API

### `POST /api/transcript`

Fetches and cleans transcript snippets for a YouTube video in `from_lang`, and
groups them into paragraphs (used as the translation unit for cross-line
context). Accepts standard `watch?v=`, `youtu.be/`, `/embed/`, `/shorts/`, and
`/v/` URLs, plus bare 11-character video IDs.

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
      "duration": 1.8,
      "paragraph": 0
    }
  ],
  "paragraphs": ["Hola a todos ..."],
  "from_lang": "es"
}
```

Errors: `400` (missing/unparseable URL or bad `from_lang`), `404` (no usable
subtitles), `502` (transcript provider failed).

### `POST /api/translate`

Translates a list of paragraph strings from `from_lang` to `to_lang`. Optionally
accepts `lines` (a nested list of the source lines per paragraph); when its shape
matches `paragraphs`, the response additionally includes `translated_lines` with
per-line aligned chunks.

Request:

```json
{
  "paragraphs": ["Hello world"],
  "from_lang": "en",
  "to_lang": "es"
}
```

Success response:

```json
{
  "translated_paragraphs": ["Hola mundo"],
  "cache_hit": false
}
```

With alignment (`lines` supplied), the response also contains
`"translated_lines": [["Hola", "mundo"]]`.

Errors: `400` when the body is not a JSON object, `paragraphs` is missing/not a
list of strings, or `from_lang`/`to_lang` are not strings.

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