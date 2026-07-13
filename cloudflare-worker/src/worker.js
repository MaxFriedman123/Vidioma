// Cloudflare Worker: YouTube caption-track LIST relay.
//
// WHY: YouTube IP-blocks datacenter egress (Render/AWS/GCP) for the innertube
// /player caption listing. Cloudflare's own egress IPs (AS13335) are NOT blocked
// (empirically verified: WARP, which egresses from these same ranges, lists
// 22/22 hard videos where Render lists 0/22). A Worker runs ON Cloudflare, so
// its outbound fetch() egresses from those clean IPs -- WITHOUT needing WireGuard
// / UDP (which PaaS hosts like Render often block) and WITHOUT WARP's per-IP
// rotation problem.
//
// It mirrors the backend select_caption_track_url() precedence EXACTLY so
// is_correct_lang means the same thing everywhere. The browser still DOWNLOADS
// the timedtext from the user's residential IP (CORS-open, not IP-locked); this
// Worker only does the light LIST call.
//
// Deploy: `wrangler deploy` (free tier: 100k req/day). Point the frontend's
// caption-track fetch at this Worker URL, or have the Flask backend call it as
// its egress for select_caption_track_url.

const INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8";
const INNERTUBE_HOSTS = [
  "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
  `https://youtubei.googleapis.com/youtubei/v1/player?key=${INNERTUBE_KEY}&prettyPrint=false`,
];
const ANDROID_CONTEXT = {
  client: { clientName: "ANDROID", clientVersion: "20.10.38", androidSdkVersion: 30, hl: "en" },
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const baseLang = (c) => (c || "").toLowerCase().split("-")[0];

function extractVideoId(input) {
  if (!input) return null;
  if (/^[a-zA-Z0-9_-]{11}$/.test(input)) return input;
  const m =
    input.match(/[?&]v=([a-zA-Z0-9_-]{11})/) ||
    input.match(/youtu\.be\/([a-zA-Z0-9_-]{11})/) ||
    input.match(/\/(?:embed|shorts|live)\/([a-zA-Z0-9_-]{11})/);
  return m ? m[1] : null;
}

async function listPlayerOnce(videoId) {
  const body = JSON.stringify({ context: ANDROID_CONTEXT, videoId });
  for (const host of INNERTUBE_HOSTS) {
    try {
      const r = await fetch(host, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "User-Agent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11)",
        },
        body,
      });
      if (!r.ok) continue;
      const data = await r.json();
      const status = data?.playabilityStatus?.status;
      if (status && status !== "OK") continue;
      return data;
    } catch (e) {
      continue;
    }
  }
  return null;
}

// A single Worker invocation is effectively pinned to one Cloudflare PoP's
// egress IP for its lifetime, so if that IP is momentarily rate-limited by
// YouTube, retrying WITHIN the invocation just re-hits the same throttled IP and
// doesn't help (measured: extra in-invocation retries didn't lift the success
// rate). The reliable recovery is retrying at the CALLER level: each fresh
// top-level request can route through a different PoP/egress. The browser does
// that (see youtubeCaptions.js fetchTrackFromEndpoint retry) and reaches 22/22.
// We keep ONE cheap extra attempt here to smooth over a transient single miss;
// delays are I/O wait (not CPU), so this stays within the free-tier budget.
const MAX_LIST_TRIES = 2;
const LIST_RETRY_DELAY_MS = 500;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function listPlayer(videoId) {
  for (let i = 0; i < MAX_LIST_TRIES; i++) {
    const data = await listPlayerOnce(videoId);
    if (data) return data;
    if (i < MAX_LIST_TRIES - 1) await sleep(LIST_RETRY_DELAY_MS);
  }
  return null;
}

// Mirror of backend select_caption_track_url precedence.
function selectTrack(playerData, fromLang) {
  const renderer = playerData?.captions?.playerCaptionsTracklistRenderer || {};
  const raw = renderer.captionTracks || [];
  if (!raw.length) return null;
  const tracks = raw.map((t) => ({
    languageCode: t.languageCode || "",
    isGenerated: (t.kind || "") === "asr",
    isTranslatable: !!t.isTranslatable,
    baseUrl: t.baseUrl || "",
  }));
  const targets = new Set(
    (renderer.translationLanguages || [])
      .map((l) => (l.languageCode || "").toLowerCase())
      .filter(Boolean)
  );
  const requested = (fromLang || "").toLowerCase();
  const cmp = (a, b) =>
    a.isGenerated !== b.isGenerated
      ? a.isGenerated
        ? 1
        : -1
      : a.languageCode.toLowerCase().localeCompare(b.languageCode.toLowerCase());

  const exact = tracks.find((t) => t.languageCode.toLowerCase() === requested);
  if (exact) return { url: exact.baseUrl, tlang: null, is_correct_lang: true, language_code: exact.languageCode };

  const regional = tracks.filter((t) => baseLang(t.languageCode) === requested).sort(cmp);
  if (regional.length)
    return { url: regional[0].baseUrl, tlang: null, is_correct_lang: true, language_code: regional[0].languageCode };

  if (targets.has(requested)) {
    const translatable = tracks.filter((t) => t.isTranslatable).sort(cmp);
    if (translatable.length) {
      const t = translatable[0];
      const url = t.baseUrl + "&tlang=" + encodeURIComponent(fromLang);
      return { url, tlang: fromLang, is_correct_lang: true, language_code: t.languageCode };
    }
  }

  const fallback = [...tracks].sort(cmp)[0];
  return { url: fallback.baseUrl, tlang: null, is_correct_lang: false, language_code: fallback.languageCode };
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (request.method !== "POST")
      return new Response(JSON.stringify({ error: "POST only" }), { status: 405, headers: { ...CORS, "Content-Type": "application/json" } });

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ error: "Body must be JSON" }, 400);
    }
    const videoId = extractVideoId(payload?.url);
    const fromLang = (payload?.from_lang || "en").toString();
    if (!videoId) return json({ error: "Could not parse a YouTube video ID" }, 400);

    const player = await listPlayer(videoId);
    if (!player) return json({ error: "blocked or unavailable" }, 503);
    const sel = selectTrack(player, fromLang);
    if (!sel) return json({ error: "no caption tracks" }, 404);

    return json({
      video_id: videoId,
      url: sel.url,
      is_correct_lang: sel.is_correct_lang,
      tlang: sel.tlang,
      language_code: sel.language_code,
    });
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}
