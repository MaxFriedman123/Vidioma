// ── Client-side YouTube caption fetch ────────────────────────────────────
// Fetches a video's captions in the user's browser instead of from the backend.
// This is the primary, $0 caption source (it replaced a paid rotating proxy —
// see docs/webshare-proxy-removed.md): YouTube IP-blocks datacenter/cloud egress
// (Render, AWS, GCP), but it does NOT block ordinary residential IPs — which is
// exactly what the user's browser has. So the timedtext caption DOWNLOAD (the
// bulk of the data, and the part that must dodge IP-blocking) happens in the
// browser; the snippets are posted to the backend, which keeps its existing
// paragraph-grouping + DeepL translation pipeline unchanged.
//
// Getting the caption track list is the tricky part. There are two ways:
//   A) DIRECT: POST YouTube's innertube /youtubei/v1/player (ANDROID client) from
//      the browser. This works from Google-owned origins but is CORS-BLOCKED from
//      a third-party site like vidioma.app — YouTube sends no
//      Access-Control-Allow-Origin for that endpoint. We still try it first (it's
//      free and works in some contexts / future-proofs against policy changes),
//      but it typically fails cross-origin in production.
//   B) HYBRID (the reliable path): ask OUR backend for the track's signed
//      timedtext URL (POST /api/caption-tracks). The backend does the innertube
//      listing server-to-server (no CORS), and the URL it returns is CORS-open
//      AND not IP-locked (ip=0.0.0.0 in its sparams), so the browser can GET it
//      from the user's residential IP. Listing is a light call, far more likely
//      to survive on a datacenter host than downloading the captions server-side.
// Either way we then GET the timedtext baseUrl with &fmt=json3 (plus &tlang= for
// YouTube auto-translation) and parse it.
//
// Track selection mirrors the backend's attempt_fetch() precedence so
// isCorrectLang means the same thing across the direct, hybrid, and pure-server
// paths. NON-web client is required: the WEB client returns UNPLAYABLE / no
// tracks and its caption URLs carry an "&exp=xpe" PO-token gate.
//
// EVERYTHING is best-effort: any failure resolves to null so the caller falls
// back to the backend's own server-side fetch. This can only ADD a free path;
// it can never turn a would-succeed load into a failure.

// A constant innertube API key. The ?key= param is not tied to any account and
// is ignored by the youtube.com host; it's only needed for the googleapis host
// fallback. This is the same public key youtube-transcript-api ships with.
const INNERTUBE_KEY = 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8';

const INNERTUBE_HOSTS = [
  `https://www.youtube.com/youtubei/v1/player?prettyPrint=false`,
  `https://youtubei.googleapis.com/youtubei/v1/player?key=${INNERTUBE_KEY}&prettyPrint=false`,
];

// ANDROID client context — the client that returns ungated caption baseUrls.
const ANDROID_CONTEXT = {
  client: {
    clientName: 'ANDROID',
    clientVersion: '20.10.38',
    androidSdkVersion: 30,
    hl: 'en',
  },
};

// A single innertube call can be slow; bound it so a stalled request can't hold
// up the whole transcript load (the caller has its own overall timeout too).
const INNERTUBE_TIMEOUT_MS = 12000;
const TIMEDTEXT_TIMEOUT_MS = 15000;

// The Cloudflare Worker relay lists via Cloudflare's egress, which YouTube
// rate-limits per-IP-per-moment: a single call succeeds only ~65-80% of the
// time, but the FAILURES ARE TRANSIENT and IP-specific. Each fresh top-level
// request can route through a different Cloudflare PoP/egress, so retrying at
// this level (with spacing to let the per-IP limit clear) reaches ~100%
// (measured 22/22, avg <2 tries). Spacing matters: back-to-back retries hit the
// same throttled edge. Only 503/blocked responses are retried; hard errors
// (400/404) short-circuit.
const RELAY_LIST_MAX_TRIES = 6;
const RELAY_LIST_RETRY_DELAY_MS = 700;

// Overall ceiling for resolving a caption track, across the direct innertube
// attempt and every relay/backend retry.
//
// The per-call timeouts above bound each request but nothing bounded the SUM: on
// a slow connection where each attempt times out rather than failing fast, the
// worst case was 2 innertube hosts + 6 relay tries + 1 backend try = ~111s of
// waiting BEFORE /api/transcript was even called, on top of that request's own
// 90s budget. On a phone (higher latency, background throttling, network
// switches) that is reached far more easily than on a desktop, which is why this
// presented as "works on my computer but not my phone": the user sat through a
// long spin and then got a failure.
//
// Once this elapses we stop retrying and return null, which falls back to the
// server-side fetch exactly as any other caption failure does.
const CAPTION_RESOLVE_BUDGET_MS = 25000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const baseLang = (code) => (code || '').toLowerCase().split('-')[0];

// Fetch with an abort-based timeout. Returns the Response, or throws on
// timeout/network error (callers treat any throw as "fall back to server").
async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

// POST the innertube player endpoint and return its parsed JSON, trying each
// host in turn. Returns null if none produced a usable (OK, has-tracks) result.
async function fetchPlayerData(videoId, deadline = Infinity) {
  const body = JSON.stringify({ context: ANDROID_CONTEXT, videoId });
  for (const host of INNERTUBE_HOSTS) {
    // This path is CORS-blocked from a third-party origin and normally fails in
    // milliseconds, but if it ever hangs it must not eat the whole budget that
    // the working (relay) path needs.
    if (Date.now() >= deadline) return null;
    try {
      const resp = await fetchWithTimeout(
        host,
        {
          method: 'POST',
          // text/plain keeps this a CORS simple request (no preflight).
          headers: { 'Content-Type': 'text/plain' },
          body,
        },
        Math.max(1, Math.min(INNERTUBE_TIMEOUT_MS, deadline - Date.now())),
      );
      if (!resp.ok) continue;
      const data = await resp.json();
      const status = data?.playabilityStatus?.status;
      // Only OK videos yield captions; anything else (LOGIN_REQUIRED, ERROR,
      // UNPLAYABLE) means we can't help from the client — let the server try.
      if (status && status !== 'OK') continue;
      return data;
    } catch (err) {
      // Network error / timeout / abort — try the next host, then give up.
      continue;
    }
  }
  return null;
}

// Extract the caption track list and the video-level translation-target codes
// from an innertube player response. Returns { tracks, translationTargets }.
function extractCaptions(playerData) {
  const renderer =
    playerData?.captions?.playerCaptionsTracklistRenderer || {};
  const tracks = (renderer.captionTracks || []).map((t) => ({
    languageCode: t.languageCode || '',
    // YouTube marks auto-generated tracks with kind "asr". Mirrors the
    // backend's is_generated flag so sort order matches.
    isGenerated: (t.kind || '') === 'asr',
    isTranslatable: !!t.isTranslatable,
    baseUrl: t.baseUrl || '',
  }));
  // Exact set of language codes YouTube will auto-translate INTO. The backend's
  // transcript.translate(code) does an exact-key lookup against this same list,
  // so we replicate that exact-match semantics (not base-language) to keep
  // isCorrectLang identical between client and server.
  const translationTargets = new Set(
    (renderer.translationLanguages || [])
      .map((l) => (l.languageCode || '').toLowerCase())
      .filter(Boolean),
  );
  return { tracks, translationTargets };
}

// Stable sort key matching the backend's sort_key: prefer manual tracks over
// auto-generated (asr), then by language code. Returns negative/zero/positive.
function trackSortCmp(a, b) {
  if (a.isGenerated !== b.isGenerated) return a.isGenerated ? 1 : -1;
  return a.languageCode.toLowerCase().localeCompare(b.languageCode.toLowerCase());
}

// Decide which track to fetch and whether it will already be in `fromLang`.
// Mirrors backend attempt_fetch() precedence exactly:
//   1. exact language match          -> isCorrectLang true
//   2. regional/base-language match  -> isCorrectLang true (prefer manual)
//   3. YouTube auto-translate         -> isCorrectLang true (tlang set)
//   4. fallback: best source track    -> isCorrectLang false (server translates)
// Returns { baseUrl, tlang, isCorrectLang } or null when there are no tracks.
function selectTrack(tracks, translationTargets, fromLang) {
  if (!tracks.length) return null;
  const requested = (fromLang || '').toLowerCase();

  // 1. Exact match.
  const exact = tracks.find((t) => t.languageCode.toLowerCase() === requested);
  if (exact) return { baseUrl: exact.baseUrl, tlang: null, isCorrectLang: true };

  // 2. Regional/base-language match (e.g. requested "en" -> "en-US"/"en-GB").
  const regional = tracks
    .filter((t) => baseLang(t.languageCode) === requested)
    .sort(trackSortCmp);
  if (regional.length) {
    return { baseUrl: regional[0].baseUrl, tlang: null, isCorrectLang: true };
  }

  // 3. YouTube auto-translate from any translatable track, only when YouTube
  //    actually offers `fromLang` as a translation target (exact-code match, as
  //    the backend does). Prefer manual source tracks.
  if (translationTargets.has(requested)) {
    const translatable = tracks
      .filter((t) => t.isTranslatable)
      .sort(trackSortCmp);
    if (translatable.length) {
      return {
        baseUrl: translatable[0].baseUrl,
        tlang: fromLang,
        isCorrectLang: true,
      };
    }
  }

  // 4. Fallback: best available source track; the backend will manually
  //    translate it into fromLang (isCorrectLang false).
  const fallback = [...tracks].sort(trackSortCmp)[0];
  return { baseUrl: fallback.baseUrl, tlang: null, isCorrectLang: false };
}

// Fetch and parse a timedtext track as json3 into {text, start, duration}
// snippets. The baseUrl already carries "&fmt=srv3"; we strip any preset fmt
// (the first fmt wins server-side, so appending json3 without stripping would
// be ignored) and request json3, which is easy to parse and preserves timing.
async function fetchTimedText(baseUrl, tlang) {
  let url = baseUrl.replace(/&fmt=[^&]*/g, '') + '&fmt=json3';
  if (tlang) url += '&tlang=' + encodeURIComponent(tlang);

  const resp = await fetchWithTimeout(url, {}, TIMEDTEXT_TIMEOUT_MS);
  if (!resp.ok) throw new Error(`timedtext HTTP ${resp.status}`);
  const data = await resp.json();
  const events = Array.isArray(data?.events) ? data.events : [];

  const snippets = [];
  for (const ev of events) {
    if (!ev || !Array.isArray(ev.segs)) continue; // skip window/append events
    const text = ev.segs.map((s) => (s && s.utf8) || '').join('');
    if (!text.trim()) continue;
    snippets.push({
      text,
      start: (ev.tStartMs || 0) / 1000,
      duration: (ev.dDurationMs || 0) / 1000,
    });
  }
  return snippets;
}

// HYBRID list: POST an endpoint that lists the video's caption tracks
// server-side (no CORS there) and returns the selected track's signed timedtext
// URL — which IS CORS-open + not IP-locked, so the browser can then fetch it.
// `endpoint` is either our own backend's /api/caption-tracks OR a Cloudflare
// Worker relay (identical request/response contract; it egresses from
// Cloudflare's IPs, so it lists even when our backend host is IP-blocked).
//
// `maxTries` retries ONLY transient failures (503/network/timeout), spaced so a
// per-IP rate-limit on one Cloudflare egress can clear before the next fresh
// request routes through a different PoP. A hard failure (400/404 = bad id / no
// captions) returns null immediately without retrying. Returns
// { baseUrl, tlang, isCorrectLang } or null.
async function fetchTrackFromEndpoint(endpoint, videoId, fromLang, maxTries = 1, deadline = Infinity) {
  if (!endpoint) return null;
  for (let attempt = 0; attempt < maxTries; attempt++) {
    // Out of overall budget: stop retrying and let the caller fall back rather
    // than keep a user waiting on attempts that no longer fit.
    if (Date.now() >= deadline) return null;
    let transient = false;
    try {
      const resp = await fetchWithTimeout(
        endpoint,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: videoId, from_lang: fromLang }),
        },
        // Never wait past the overall deadline on a single attempt.
        Math.max(1, Math.min(INNERTUBE_TIMEOUT_MS, deadline - Date.now())),
      );
      if (resp.ok) {
        const data = await resp.json();
        if (data && data.url) {
          // The endpoint already appended &tlang for the auto-translate case, so
          // pass tlang=null downstream to avoid appending it twice.
          return { baseUrl: data.url, tlang: null, isCorrectLang: !!data.is_correct_lang };
        }
        return null; // 200 but no url -> nothing usable, don't retry
      }
      // 503 (blocked/rate-limited egress) is transient and worth retrying on a
      // fresh request; other statuses (400/404) are terminal.
      transient = resp.status === 503;
    } catch (err) {
      transient = true; // network error / timeout -> retry
    }
    if (!transient) return null;
    // Skip the inter-try pause when there is no budget left for another attempt.
    if (attempt < maxTries - 1 && Date.now() + RELAY_LIST_RETRY_DELAY_MS < deadline) {
      await sleep(RELAY_LIST_RETRY_DELAY_MS);
    }
  }
  return null;
}

/**
 * Fetch a video's captions in the browser.
 *
 * Tries the DIRECT innertube listing first (works from some origins), then the
 * HYBRID path (backend lists, browser downloads) which is what works from a
 * third-party production origin. Either way the timedtext download happens in
 * the browser (residential IP).
 *
 * @param {string} videoId    11-char YouTube video id.
 * @param {string} fromLang   transcript language code the user requested.
 * @param {string} apiBaseUrl our backend base URL (for the hybrid list call).
 * @param {string} [relayUrl] optional Cloudflare Worker relay URL. When set, the
 *          browser lists tracks via the relay FIRST (its Cloudflare egress isn't
 *          YouTube-blocked, so it lists even when our backend host is), falling
 *          back to the backend's /api/caption-tracks.
 * @returns {Promise<{snippets: Array, isCorrectLang: boolean}|null>}
 *          The snippets ({text,start,duration}) plus whether they are already
 *          in fromLang, or null if neither path could get usable captions
 *          (caller then falls back to the backend's server fetch).
 */
export async function fetchClientCaptions(videoId, fromLang, apiBaseUrl, relayUrl) {
  if (!videoId) return null;

  // One budget for the whole track-resolution phase, so a slow network can't
  // stack every per-call timeout into a multi-minute wait before the caller's
  // own transcript request even starts.
  const deadline = Date.now() + CAPTION_RESOLVE_BUDGET_MS;

  // Resolve the track to download: DIRECT innertube first, then HYBRID via a
  // relay/backend. `choice` is { baseUrl, tlang, isCorrectLang }.
  let choice = null;
  try {
    const playerData = await fetchPlayerData(videoId, deadline);
    if (playerData) {
      const { tracks, translationTargets } = extractCaptions(playerData);
      const direct = selectTrack(tracks, translationTargets, fromLang);
      if (direct && direct.baseUrl) choice = direct;
    }
  } catch (err) {
    // Direct listing failed (typically CORS in production) — try the hybrid.
  }

  // Hybrid listing: Cloudflare Worker relay first (clean egress), then our own
  // backend. Both share the same request/response contract. The relay gets
  // several spaced retries because its per-call success is only ~65-80% (each
  // fresh request can land on a different, un-throttled Cloudflare egress); the
  // backend gets a single try (its egress isn't the bottleneck being retried).
  if (!choice && relayUrl) {
    choice = await fetchTrackFromEndpoint(relayUrl, videoId, fromLang, RELAY_LIST_MAX_TRIES, deadline);
  }
  if (!choice && apiBaseUrl) {
    // Always give the backend one attempt even if the relay used up the budget:
    // it's the last chance at a client-side caption path, and skipping it would
    // force the server-side fetch (which YouTube IP-blocks on our host).
    const backendDeadline = Math.max(deadline, Date.now() + INNERTUBE_TIMEOUT_MS);
    choice = await fetchTrackFromEndpoint(
      `${apiBaseUrl}/api/caption-tracks`, videoId, fromLang, 1, backendDeadline,
    );
  }
  if (!choice || !choice.baseUrl) return null;

  try {
    const snippets = await fetchTimedText(choice.baseUrl, choice.tlang);
    // No usable lines (e.g. an empty/gated track slipped through) — fall back.
    if (!snippets.length) return null;
    return { snippets, isCorrectLang: choice.isCorrectLang };
  } catch (err) {
    // Timedtext download failed — the server fetch is the fallback.
    console.warn('Client caption download failed; falling back to server:', err);
    return null;
  }
}
