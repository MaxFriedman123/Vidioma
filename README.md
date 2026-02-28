# Vidioma

Vidioma is a simple web application composed of a Flask backend and a React frontend. This repository contains the API server in `backend/` and the single-page application in `frontend/`.

## Project Structure

- `backend/` — Flask API and backend tests
  - `app.py` — the Flask application entrypoint
  - `test_api.py` — basic API tests
- `frontend/` — React app created with Create React App
  - `package.json`, `src/`, `public/` — frontend source, scripts and assets

## Features

- Minimal REST API powered by Flask
- React-based frontend served by the development server (or built for production)

## Prerequisites

- Python 3.8+ (for backend)
- Node.js 16+ and npm (for frontend)

## Backend — Setup & Run

Open a terminal and run:

Windows (PowerShell):

```powershell
cd backend
python -m venv venv
venv\Scripts\Activate.ps1
# If you have a requirements file: pip install -r requirements.txt
pip install Flask pytest
python app.py
```

Notes:
- If `app.py` does not start the server directly, you can set the Flask entrypoint and run via `flask run`:

```powershell
setx FLASK_APP app.py
flask run --port 5000
```

By default the API will be available at `http://localhost:5000`.

## Frontend — Setup & Run

Open a separate terminal and run:

```powershell
cd frontend
npm install
npm start
```

The development server runs by default at `http://localhost:3000` and should proxy or call the backend at `http://localhost:5000` (adjust the frontend configuration if needed).

To create a production build:

```powershell
cd frontend
npm run build
```

## Tests

Backend tests (pytest):

```powershell
cd backend
pytest -q
```

Frontend tests:

```powershell
cd frontend
npm test
```

## Development Notes

- API endpoints and frontend API base URL should be coordinated; update the frontend API base URL if the backend runs on a different host or port.
- Add a `requirements.txt` in `backend/` if you want reproducible Python installs.

## Contributing

1. Fork the repository.
2. Create a branch for your feature or bugfix.
3. Submit a pull request with a clear description of changes.

## License

Specify a license for this project (e.g., MIT) by adding a `LICENSE` file.

---

If you'd like, I can:

- run the backend and frontend locally to verify everything starts, or
- run the backend tests now, or
- add a `requirements.txt` and a simple `.env.example` file.

Tell me which of these you'd like next.
