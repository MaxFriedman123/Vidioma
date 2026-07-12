// ── Client-side YouTube caption fetch ────────────────────────────────────
// Fetches a video's captions directly from the user's browser instead of from
// the backend. This is the primary, $0 caption source (it replaced a paid
// rotating proxy — see docs/webshare-proxy-removed.md): YouTube IP-blocks
// datacenter/cloud egress (Render, AWS, GCP), but it does NOT block ordinary
// residential IPs — which is exactly what the user's browser has. So the
// browser fetches the caption snippets and posts them to the backend, which
// keeps its existing paragraph-grouping + DeepL translation pipeline unchanged.
//
// How it works (all verified working cross-origin against live YouTube):
//   1. POST the innertube /youtubei/v1/player endpoint with the ANDROID client
//      context. We MUST use a non-web client: the WEB client returns
//      playabilityStatus UNPLAYABLE / no tracks, and the web player's own
//      caption URLs carry an "&exp=xpe" PO-token gate that returns an empty
//      body. The ANDROID client's baseUrls are ungated.
//      The request uses Content-Type: text/plain so it stays a CORS "simple
//      request" (no preflight) — YouTube's innertube endpoint doesn't send
//      Access-Control-Allow-Headers for a JSON content-type preflight.
//   2. Select a caption track mirroring the backend's attempt_fetch() logic so
//      the resulting isCorrectLang means the same thing on both sides.
//   3. GET the track's timedtext baseUrl with &fmt=json3 (the signed URL is not
//      IP-locked — ip=0.0.0.0 in its sparams — so the browser can fetch it),
//      optionally with &tlang=<lang> for YouTube's own auto-translation.
//
// EVERYTHING here is best-effort: any failure (YouTube shape change, network
// error, a video the browser can't reach) resolves to null so the caller
// transparently falls back to the backend's server-side fetch. This can only
// ever ADD a fast, free path; it can never turn a would-succeed load into a
// failure.

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
async function fetchPlayerData(videoId) {
  const body = JSON.stringify({ context: ANDROID_CONTEXT, videoId });
  for (const host of INNERTUBE_HOSTS) {
    try {
      const resp = await fetchWithTimeout(
        host,
        {
          method: 'POST',
          // text/plain keeps this a CORS simple request (no preflight).
          headers: { 'Content-Type': 'text/plain' },
          body,
        },
        INNERTUBE_TIMEOUT_MS,
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

/**
 * Fetch a video's captions entirely in the browser.
 *
 * @param {string} videoId  11-char YouTube video id.
 * @param {string} fromLang transcript language code the user requested.
 * @returns {Promise<{snippets: Array, isCorrectLang: boolean}|null>}
 *          The snippets ({text,start,duration}) plus whether they are already
 *          in fromLang, or null if the browser couldn't get usable captions
 *          (caller must then fall back to the backend's server fetch).
 */
export async function fetchClientCaptions(videoId, fromLang) {
  if (!videoId) return null;
  try {
    const playerData = await fetchPlayerData(videoId);
    if (!playerData) return null;

    const { tracks, translationTargets } = extractCaptions(playerData);
    const choice = selectTrack(tracks, translationTargets, fromLang);
    if (!choice || !choice.baseUrl) return null;

    const snippets = await fetchTimedText(choice.baseUrl, choice.tlang);
    // No usable lines (e.g. an empty/gated track slipped through) — fall back.
    if (!snippets.length) return null;

    return { snippets, isCorrectLang: choice.isCorrectLang };
  } catch (err) {
    // Any failure is non-fatal: the server fetch is the fallback.
    console.warn('Client caption fetch failed; falling back to server:', err);
    return null;
  }
}
