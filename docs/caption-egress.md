# Caption egress: $0 replacements for the Webshare proxy (tested)

The backend's caption **LIST** call (YouTube innertube `/youtubei/v1/player`) is
IP-blocked from datacenter hosts. This doc records every $0 alternative that was
built and tested end-to-end against a fixed set of 22 real videos (Spanish
native, English, and cross-language auto-translate cases), and the resulting
recommendation. The paid Webshare rotating-residential proxy was removed
2026-07-12; this is what replaces it for free.

## The two calls, and why the split matters

1. **LIST** — POST innertube `/player` (ANDROID client) to get the signed
   timedtext `baseUrl`. This is the call YouTube IP-blocks on datacenter hosts.
   It is **not** possible from the browser: the endpoint returns HTTP 403 the
   moment an `Origin` header is present (verified), so a browser (which always
   sends `Origin` cross-origin) can never call it. Something server-side must
   list.
2. **DOWNLOAD** — GET the signed timedtext `baseUrl` (`&fmt=json3`, optional
   `&tlang=`). This URL **is** CORS-open and **not** IP-locked (`ip=0.0.0.0` in
   its sparams), so the user's browser downloads it from their residential IP.

So only the light LIST call needs a clean egress IP. That is the whole problem.

## Measured results (22-video set, 2026-07-12)

| Method | LIST success | Notes |
|---|---|---|
| Local residential IP (control) | **22/22** | Confirms the set + that residential isn't blocked |
| **Deployed Render (datacenter, direct)** | **0/22** | The problem. Total block (worse than the ~1/10 seen weeks earlier) |
| **Cloudflare WARP (wgcf + wireproxy)** | **22/22** | Highest raw reliability. Stable across 3 rounds + ~40 min sustained. Egresses from Cloudflare's *consumer WARP* range (104.28.x), which YouTube treats as clean. Needs outbound UDP (see caveat) |
| **Cloudflare Worker relay (DEPLOYED)** | single call ~65-80%; **spaced-retry 22/22** | **Shipped.** Egresses from Cloudflare's *edge* ranges (104.16/172.64), which YouTube rate-limits per-IP-per-moment (fails are transient + IP-specific, and which videos fail is random per run). Each fresh call can route through a different edge, so retrying across separate calls (the browser does this, 6× spaced 700ms) reaches ~22/22. No UDP needed |
| Free public proxies (rotation) | ~21–22/22 per live proxy | 308/600 alive; ~135 YouTube-usable. But latency 11s–628s, ephemeral, unknown operators — a fragile fallback, not a dependency |
| Tor (SOCKS5 exit) | **1/22** | 21/22 `LOGIN_REQUIRED`; exits are bot-gated. Circuit rotation gets ~25% usable exits at ~5s each — too slow/fragile |
| innertube client rotation | 1/6 blocked IPs rescued | Block is IP-based, not client-based. Near-useless alone |
| Public Invidious / Piped | effectively 0 | Invidious serves empty caption bodies (known unfixed bug iv-org#5571); the one live Piped instance is itself YouTube-IP-blocked for uncached videos |

The DOWNLOAD call, run from a residential IP (the browser), is **22/22** for
every method above whenever LIST succeeds — including the `&tlang=`
auto-translate downloads, which sometimes HTTP-429 when hammered through a single
shared WARP IP but always succeed from the user's own IP.

## Recommendation: Cloudflare Worker relay (shipped) + browser for the download

The browser hybrid (already shipped) handles the download. For the LIST call the
browser lists through a **Cloudflare Worker relay** whose Cloudflare egress isn't
IP-blocked like the backend host is. A single relay call succeeds only ~65-80%
(the Worker's edge egress is rate-limited per-IP-per-moment), but the failures
are transient and IP-specific, so the browser retries across separate calls (each
can land on a different, un-throttled edge) and reaches ~22/22. This needs no
outbound UDP and no backend change — it was chosen over WARP for deployment
because WARP requires outbound UDP that some PaaS hosts (possibly Render) block.

**WARP (`backend/warp/`, `CAPTION_PROXY_URL`) remains the higher-raw-reliability
option** (22/22 single-shot, stable under load, from Cloudflare's cleaner
consumer range) — use it if your host allows outbound UDP to
`engage.cloudflareclient.com:2408` and you'd rather fix it server-side.

### Layered design (each layer is independent and degrades safely)

1. **Browser direct** — try the innertube LIST in the browser. Usually
   CORS-blocked in production, but free when it works.
2. **Cloudflare Worker relay** (optional, `REACT_APP_CAPTION_RELAY_URL`) — the
   browser lists via the Worker, whose Cloudflare egress isn't blocked.
3. **Backend `/api/caption-tracks`** — the backend lists. With
   `CAPTION_PROXY_URL` pointing at a WARP sidecar, this succeeds even on Render.
4. **Backend direct server fetch** — final fallback (works on residential hosts /
   local dev; expected to fail on a blocked datacenter host).

Any one working layer serves the video. Nothing regresses if a layer is
unconfigured or fails.

## How to enable

### Option A — WARP sidecar on the backend (recommended)

`backend/warp/start-warp.sh` brings up WARP as a userspace SOCKS5 proxy (no root,
no NET_ADMIN). Run it alongside gunicorn and set:

```env
CAPTION_PROXY_URL=socks5h://127.0.0.1:25344
```

`requirements.txt` already pins `requests[socks]` for SOCKS support. The app
routes **only** the caption LIST (`get_cached_transcript` +
`select_caption_track_url`) through this proxy; everything else is unchanged.
Unset `CAPTION_PROXY_URL` → direct connection (prior behavior).

**Host requirement:** WARP/WireGuard needs **outbound UDP to
`engage.cloudflareclient.com:2408`** (fallbacks 500/1701/4500). Verify the host
allows outbound UDP before relying on this. If it doesn't (some PaaS, possibly
Render), use Option B.

**Durability:** WARP IPs are shared and can be transiently rate-limited under
load; a fresh `wgcf register` draws a new IP (sampled 4/4 clean). The start
script re-registers on boot; add a re-register-on-repeated-block escape hatch if
you see sustained blocks.

### Option B — Cloudflare Worker relay (no UDP needed)

`cloudflare-worker/` is a complete, tested Worker with the **same
request/response contract** as `/api/caption-tracks`. Deploy on the free tier
(100k req/day):

```sh
cd cloudflare-worker && npx wrangler deploy   # needs a Cloudflare login
```

Then point the frontend at it:

```env
REACT_APP_CAPTION_RELAY_URL=https://vidioma-caption-relay.<you>.workers.dev
```

The browser will list via the Worker first, falling back to the backend. This
needs no UDP and has no per-IP rotation problem, at the cost of one free CF
account + deploy.

## What was ruled out, and why

- **Tor** — exits are bot-gated (`LOGIN_REQUIRED`); 1/22, and rotation is slow.
- **innertube client rotation** — the block is by IP, not client; 1/6.
- **Public Invidious/Piped** — Invidious caption bodies are empty (unfixed
  upstream bug); the surviving Piped instance is itself IP-blocked. Not viable.
- **Free public proxy rotation** — surprisingly capable for LIST (many list
  22/22), but latency is wild (up to 10 min), proxies die within minutes, and
  operators are unknown (privacy/traffic-tampering risk). Acceptable only as a
  last-ditch fallback layer, never the primary. No truly-free *residential*
  proxies exist; residential is always paid.
- **Re-subscribing to Webshare** — works, but it's the paid thing we removed.
