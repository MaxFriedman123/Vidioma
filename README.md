# Vidioma

Vidioma is an interactive language practice app for YouTube videos. You paste a video URL, choose source and target languages, and practice translating subtitle lines while the video pauses line-by-line.

## Features

- Pulls transcript snippets for a YouTube video
- Fetches captions in the user's browser by default (no server-side proxy needed)
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
  cloudflare-worker/           caption-list relay (see its README)
  cloudflare-worker-keepalive/ Supabase anti-pause cron (see its README)
  docs/
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

## Caption Fetching

YouTube blocks caption requests coming from datacenter/cloud IPs (Render, AWS,
GCP, ...), which is why a paid rotating-proxy (Webshare) was once required in
production. It does not block ordinary residential IPs — and the app already
runs a React frontend in the user's residential browser. So captions are fetched
**client-side**, and the proxy was removed (see
`docs/webshare-proxy-removed.md` if it ever needs to come back):

1. The browser POSTs YouTube's innertube `/youtubei/v1/player` endpoint with the
   ANDROID client context (using `Content-Type: text/plain` so it stays a CORS
   simple request with no preflight). This returns the caption track list with
   ungated `baseUrl`s. The WEB client is deliberately not used — its caption
   URLs are PO-token-gated and return an empty body.
2. The browser picks a track mirroring the backend's selection logic (exact →
   regional → YouTube auto-translate → source-language fallback) and GETs the
   track's `timedtext` URL as `fmt=json3` (optionally `&tlang=` for YouTube's
   own auto-translation).
3. It posts the resulting snippets to `POST /api/transcript` as
   `client_snippets`; the backend runs its existing cleaning + paragraph
   grouping + (when needed) DeepL translation pipeline on them, unchanged.

This is a zero-cost replacement for the proxy. It is also fault-tolerant:
`frontend/src/youtubeCaptions.js` returns `null` on any failure, and the backend
falls back to a direct (proxy-free) server-side fetch when a request arrives
without valid `client_snippets`. So an old client or a malformed payload still
degrades gracefully — though on a datacenter host that direct fallback can be
IP-blocked by YouTube, which is exactly the case the browser path now covers.

## API

### `POST /api/transcript`

Fetches and cleans transcript snippets for a YouTube video in `from_lang`, and
groups them into paragraphs (used as the translation unit for cross-line
context). Accepts standard `watch?v=`, `youtu.be/`, `/embed/`, `/shorts/`, and
`/v/` URLs, plus bare 11-character video IDs.

Captions may be supplied by the client (preferred; see "Caption Fetching"). When
`client_snippets` is present and valid, the server processes those instead of
fetching from YouTube itself. Both fields are optional; omitting them makes the
server fetch the transcript directly.

Request (client-supplied captions):

```json
{
  "url": "https://www.youtube.com/watch?v=YICiHiU2GBU",
  "from_lang": "es",
  "client_snippets": [
    { "text": "Hola a todos", "start": 12.34, "duration": 1.8 }
  ],
  "client_is_correct_lang": true
}
```

`client_is_correct_lang` tells the server whether the client-supplied snippets
are already in `from_lang` (`true`: native/regional/YouTube-auto-translated) or
in another language that the server must translate (`false`).

Request (server-side fetch, no client captions):

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

### `GET|POST /api/db-keepalive`

Touches the database so Supabase counts the project as active (see [Keeping
Supabase Awake](#keeping-supabase-awake)). Takes no body and needs no auth: the
caller is a cron job with no user identity. Does one indexed read (`videos`,
one column, one row) and writes nothing.

```json
{ "ok": true, "pinged_at": "2026-07-24T06:12:03.114221+00:00" }
```

Errors: `503` when Supabase is not configured, `502` when the read failed. Both
mean the project was **not** touched. Rate-limited to 60 requests per hour.

## Caching Behavior

- In-memory LRU cache is used for the server-side transcript fetch and processed transcript snippets.
- Redis cache (if available) is used for processed transcript snippets (already-in-language only) and `/api/translate` responses. The processed-snippet cache is shared by the client-caption and server-fetch paths, keyed on `(video_id, from_lang)`, so a browser fetch for an already-processed video is a free hit.
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

## Keeping Supabase Awake

A Supabase free-plan project pauses after 7 consecutive days without activity,
which takes down auth, saved progress, classes, and assignments until it is
manually restored from the dashboard. `cloudflare-worker-keepalive/` is a cron
Worker that calls `GET|POST /api/db-keepalive` every 2 days; the endpoint does one
cheap indexed read, which is enough to reset Supabase's idle clock. The Worker
stores no credentials; the backend uses the service key it already has. See that
directory's README for setup.

## Current Limitations

- Error responses are basic and can be improved
- Client-side caption fetching depends on YouTube's innertube/timedtext contract; if YouTube changes it, the server-side fetch fallback still applies

## Tests

- Backend: `cd backend && pip install -r requirements-dev.txt && python -m pytest -q`
- Frontend: `cd frontend && CI=true npx react-scripts test --watchAll=false`

Both suites (plus the frontend production build) run automatically on every push
and pull request via GitHub Actions (`.github/workflows/ci.yml`), gating deploys
to `main`. `smoke_api.py` is a manual latency check against a locally running
server, not part of the automated suite.

## License

No license file is currently included.