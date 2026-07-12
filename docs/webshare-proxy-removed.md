# Webshare rotating-proxy: removed (record for re-adding)

Removed 2026-07-12. This document is a complete record of the Webshare
rotating-residential-proxy integration that the backend used to fetch YouTube
transcripts, so it can be re-added verbatim if ever needed.

## Why it existed, and why it was removed

YouTube IP-blocks datacenter/cloud egress (Render, AWS, GCP, ...), so the
server-side transcript fetch (`youtube-transcript-api`) failed with
IpBlocked/RequestBlocked in production. Webshare's rotating **residential**
proxy routed the fetch through non-datacenter IPs, which YouTube does not block.

It was removed because captions are now fetched **in the user's browser** (a
residential IP), which is a $0 replacement that covers the same scenarios. See
`README.md` -> "Caption Fetching" and `frontend/src/youtubeCaptions.js`. The
server still does a **direct** (proxy-free) fetch as a fallback; only the paid
proxy hop was deleted.

No secret scrubbing was needed: `backend/.env` is git-ignored and was never
committed, and the live credentials lived only in the host's environment
(Render). The Webshare subscription was CANCELED on 2026-07-12, which
invalidates those old credentials — so a re-add needs a **new** Webshare
account, not the previous username/password. Also remove the stale
`WEBSHARE_USERNAME`/`WEBSHARE_PASSWORD` vars from the Render environment if they
are still set there.

## How to re-add

1. Sign up for a fresh Webshare rotating-residential-proxy plan (the old
   credentials no longer work).
2. `pip` extra: change `requests` back to `requests[socks]` in
   `backend/requirements.txt` (SOCKS support was only needed for the proxy).
3. Set the credentials in the backend environment (Render env or `backend/.env`,
   which is git-ignored):
   ```env
   WEBSHARE_USERNAME=your_webshare_username
   WEBSHARE_PASSWORD=your_webshare_password
   # Optional: force the proxy path and skip the always-failing direct attempt
   # on datacenter hosts. RENDER is set automatically on Render.
   FORCE_PROXY=1
   ```
4. Re-apply the code below to `backend/app.py` and re-add the test.

### a) Imports (top of `app.py`, after the `YouTubeTranscriptApi` import)

```python
from youtube_transcript_api.proxies import GenericProxyConfig
try:
    # Purpose-built config for Webshare rotating RESIDENTIAL proxies: uses the
    # correct HTTP endpoint (p.webshare.io:80), rotates IPs automatically, and
    # retries when an IP is blocked — the reliable way past YouTube's
    # datacenter-IP blocking on hosts like Render.
    from youtube_transcript_api.proxies import WebshareProxyConfig
except Exception:  # pragma: no cover - older library versions
    WebshareProxyConfig = None
```

### b) `get_cached_transcript` proxy path

`get_cached_transcript(video_id, from_lang)` keeps its inner `attempt_fetch`
helper. Re-add the `proxy_fetch` inner function and restore the main execution
flow to try direct first, then fall back to the proxy.

```python
    def proxy_fetch():
        """Fetch through the rotating residential proxy with a bounded retry
        loop. YouTube blocks datacenter IPs (e.g. Render), so this is the path
        that actually works in prod. Extracted so it can be called both as the
        fallback after a failed direct attempt AND as the sole path when direct
        is skipped — the creds check and retry loop are never dropped."""
        proxy_username = os.environ.get("WEBSHARE_USERNAME")
        proxy_password = os.environ.get("WEBSHARE_PASSWORD")

        if not proxy_username or not proxy_password:
            raise ValueError("Proxy credentials are not configured and direct fetch failed.")

        if WebshareProxyConfig is not None:
            # Rotating residential proxies over Webshare's HTTP endpoint
            # (p.webshare.io:80). This auto-rotates IPs and retries when an IP
            # is blocked. Passing the raw username is correct — the config adds
            # the "-rotate" suffix itself.
            proxy_config = WebshareProxyConfig(
                proxy_username=proxy_username,
                proxy_password=proxy_password,
            )
        else:
            # Fallback for older library versions without WebshareProxyConfig:
            # use the HTTP rotating endpoint (NOT socks5:1080, which targets the
            # wrong product/port and does not rotate).
            http_url = f"http://{proxy_username}-rotate:{proxy_password}@p.webshare.io:80/"
            proxy_config = GenericProxyConfig(http_url=http_url, https_url=http_url)

        proxy_api = _install_fast_fetcher(YouTubeTranscriptApi(proxy_config=proxy_config))

        # Bounded server-side retry on the proxy path. The Webshare rotating
        # config already retries a blocked IP internally, but other transient
        # upstream errors (connection resets, sporadic 5xx, a cold first proxy
        # handshake) can still surface on the first try and then succeed on the
        # next — which is exactly the "fails once, works on retry" symptom.
        # Retrying here means the user usually doesn't have to.
        import time
        last_exc = None
        for attempt in range(3):
            try:
                return attempt_fetch(proxy_api)
            except Exception as proxy_exc:
                last_exc = proxy_exc
                print(f"Proxy fetch attempt {attempt + 1} failed: {proxy_exc}")
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
        raise last_exc

    # --- Main execution flow: Direct fetch first, Proxy fallback second ---

    # On hosts where YouTube blocks the server IP (Render and other datacenters),
    # the direct attempt fails ~100% of the time, so paying for it just adds a
    # guaranteed-failing round-trip before the proxy path that actually works.
    # Skip it when FORCE_PROXY=1 or RENDER is set. Default (unset) keeps today's
    # exact direct-first behavior so local dev / non-prod never regresses.
    _force_proxy = os.environ.get("FORCE_PROXY") == "1" or os.environ.get("RENDER")
    if _force_proxy:
        return proxy_fetch()

    # 1. ATTEMPT FAST DIRECT CONNECTION FIRST
    try:
        print(f"Attempting direct fetch for {video_id} in {from_lang}...")
        direct_api = _install_fast_fetcher(YouTubeTranscriptApi())
        return attempt_fetch(direct_api)

    except Exception as e:
        print(f"Direct fetch failed, falling back to proxy: {e}")
        # 2. FALLBACK TO ROTATING RESIDENTIAL PROXY IF BLOCKED OR FAILED.
        return proxy_fetch()
```

### c) `/api/transcript` error branch

Re-add this branch to the `except Exception as e:` block in `get_transcript()`
(before the generic "blocked" branch):

```python
        if "proxy credentials are not configured" in low:
            return jsonify({
                "error": "Transcript fetching is temporarily unavailable (server proxy not configured).",
            }), 503
```

### d) Test (`backend/test_fast_fetch.py`)

```python
class TestForceProxyGating(unittest.TestCase):
    """FORCE_PROXY/RENDER skips the always-failing direct attempt in prod; unset
    keeps direct-first behavior. Proxy path still requires creds."""

    def test_force_proxy_skips_direct_and_requires_creds(self):
        import os
        had = os.environ.get("FORCE_PROXY")
        os.environ["FORCE_PROXY"] = "1"
        # Ensure no proxy creds so the proxy path raises immediately (proving we
        # went straight to proxy_fetch without attempting the direct fetch).
        saved = {k: os.environ.pop(k, None) for k in ("WEBSHARE_USERNAME", "WEBSHARE_PASSWORD")}
        try:
            app.get_cached_transcript.cache_clear()
            with self.assertRaises(Exception):
                app.get_cached_transcript("dQw4w9WgXcQ", "en")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            if had is None:
                os.environ.pop("FORCE_PROXY", None)
            else:
                os.environ["FORCE_PROXY"] = had
            app.get_cached_transcript.cache_clear()
```

### Notes

- `_install_fast_fetcher` / `_FastTranscriptListFetcher` are the cold-load
  latency optimization and are NOT part of Webshare — they work with or without
  a proxy (they reuse whatever `proxy_config` the `YouTubeTranscriptApi` was
  built with, which is `None` when there's no proxy). They were kept.
- The `youtube-transcript-api` library supports proxies out of the box via
  `YouTubeTranscriptApi(proxy_config=...)`; no other backend change is required.
