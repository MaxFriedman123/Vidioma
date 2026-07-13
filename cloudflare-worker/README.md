# Vidioma caption-list relay (Cloudflare Worker)

A tiny, free Cloudflare Worker that performs the YouTube caption-track **LIST**
call and returns the selected track's signed timedtext URL. It egresses from
Cloudflare's IP ranges (AS13335), which YouTube does **not** IP-block — unlike
datacenter hosts such as Render (measured: Render lists 0/22 hard videos, this
relay's egress lists 22/22). The browser still downloads the timedtext itself
from the user's residential IP.

It is a drop-in for the backend's `POST /api/caption-tracks`: identical request
and response shapes, so the frontend can call it or the backend without change.

## Contract

Request (POST, JSON): `{ "url": "<video url or 11-char id>", "from_lang": "es" }`

Response (200): `{ video_id, url, is_correct_lang, tlang, language_code }`

Errors: 400 (bad id), 404 (no caption tracks), 503 (blocked/unavailable).
CORS is open (`Access-Control-Allow-Origin: *`) so the browser can call it.

## Deploy (free tier, 100k requests/day)

```sh
npm i -g wrangler        # or use npx
wrangler login           # one-time, needs a Cloudflare account (free)
wrangler deploy
```

This prints a URL like `https://vidioma-caption-relay.<you>.workers.dev`. Point
the frontend at it:

```env
REACT_APP_CAPTION_RELAY_URL=https://vidioma-caption-relay.<you>.workers.dev
```

The frontend lists via the relay first, then falls back to the backend's
`/api/caption-tracks`. See `../docs/caption-egress.md` for the full design and
the tested comparison of every $0 method.

## Local check

`wrangler dev --local` runs the Worker on localhost; POST it a `{url, from_lang}`
and confirm it returns a `url`. (Local dev egresses from your own IP, so it only
validates the logic — the Cloudflare-egress-is-clean property holds once
deployed.)
