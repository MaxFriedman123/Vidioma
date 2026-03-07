# Vidioma

Vidioma is a language-learning web app that turns YouTube subtitles into an interactive translation exercise. Paste in a YouTube URL, pick a source and target language, and the app pauses line by line so the user can type the translation before continuing.

## What it does

- Fetches a YouTube transcript from the backend
- Translates subtitle lines on demand instead of translating the entire video up front
- Pauses playback at each subtitle line for active recall practice
- Checks user answers with fuzzy text matching rather than exact string matching
- Supports multiple language pairs through Google Translate

## Tech stack

### Frontend
- React
- Axios
- `react-youtube`
- Custom CSS

### Backend
- Flask
- `flask-cors`
- `youtube-transcript-api`
- `deep-translator`
- `python-dotenv`

## Project structure

```text
Vidioma/
├─ backend/
│  ├─ app.py
│  ├─ requirements.txt
│  ├─ .env.example
│  └─ manual_api_smoke_test.py
├─ frontend/
│  ├─ package.json
│  ├─ .env.example
│  ├─ public/
│  └─ src/
└─ README.md
```

## Local setup

### 1. Backend

From the `backend` folder:

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Add your Webshare proxy credentials to `backend/.env`:

```env
WEBSHARE_USERNAME=your_webshare_username
WEBSHARE_PASSWORD=your_webshare_password
```

Then start the API:

```powershell
python app.py
```

The backend runs at `http://localhost:5000` by default.

### 2. Frontend

From the `frontend` folder:

```powershell
npm install
copy .env.example .env
```

Set the frontend API URL in `frontend/.env`:

```env
REACT_APP_API_URL=http://localhost:5000
```

Then start the frontend:

```powershell
npm start
```

The frontend runs at `http://localhost:3000` by default.

## Environment variables

### Backend

- `WEBSHARE_USERNAME` — Webshare proxy username
- `WEBSHARE_PASSWORD` — Webshare proxy password

### Frontend

- `REACT_APP_API_URL` — base URL for the Flask API

## API routes

### `POST /api/transcript`

Fetches transcript lines for a YouTube video.

Example request body:

```json
{
  "url": "https://www.youtube.com/watch?v=example",
  "from_lang": "en"
}
```

### `POST /api/translate`

Translates one or more transcript lines.

Example request body:

```json
{
  "text": ["Hello world", "How are you?"],
  "from_lang": "en",
  "to_lang": "es"
}
```

## Notes

- The frontend uses `REACT_APP_API_URL` and falls back to `http://localhost:5000` if it is not set.
- The backend expects valid Webshare credentials in the environment.
- `manual_api_smoke_test.py` is currently a simple manual smoke script, not a full automated test suite.

## Future improvements

- Replace the manual backend smoke script with real automated tests
- Add cleaner error handling and user-facing error states
- Add better metadata and screenshots for deployment/portfolio presentation
- Support more transcript edge cases and unavailable subtitle scenarios

## License

No license has been added yet.