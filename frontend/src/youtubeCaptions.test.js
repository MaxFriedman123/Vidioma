import { fetchClientCaptions } from './youtubeCaptions';

// Build a fake innertube player response with the given caption tracks and
// optional translation-target language list.
function playerResponse(tracks, translationLanguages) {
  return {
    playabilityStatus: { status: 'OK' },
    captions: {
      playerCaptionsTracklistRenderer: {
        captionTracks: tracks,
        translationLanguages: (translationLanguages || []).map((c) => ({
          languageCode: c,
        })),
      },
    },
  };
}

function track(languageCode, { asr = false, translatable = true } = {}) {
  return {
    languageCode,
    kind: asr ? 'asr' : '',
    isTranslatable: translatable,
    // fmt=srv3 is present on real baseUrls; the module must strip it.
    baseUrl: `https://yt/timedtext?lang=${languageCode}&fmt=srv3`,
  };
}

// A minimal json3 timedtext body: two lines with timing.
function json3Body() {
  return JSON.stringify({
    events: [
      { tStartMs: 0, dDurationMs: 1500, segs: [{ utf8: 'Hello ' }, { utf8: 'world' }] },
      { tStartMs: 1500, dDurationMs: 1500, segs: [{ utf8: 'second line' }] },
      { tStartMs: 3000, dDurationMs: 500, segs: [{ utf8: '  ' }] }, // whitespace-only -> dropped
      { aAppend: 1 }, // window/append event with no segs -> skipped
    ],
  });
}

// Install a fetch mock. `onPlayer` receives the parsed POST body and returns the
// player JSON (or null to make the response not-ok). `onTimedText` receives the
// requested URL and returns the body string.
function installFetch({ onPlayer, onTimedText }) {
  global.fetch = jest.fn(async (url, options) => {
    if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
      const body = JSON.parse(options.body);
      const data = onPlayer ? onPlayer(body) : null;
      if (!data) return { ok: false, status: 500, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => data };
    }
    // timedtext
    const body = onTimedText ? onTimedText(url) : json3Body();
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => JSON.parse(body),
      text: async () => body,
    };
  });
}

afterEach(() => {
  jest.restoreAllMocks();
  delete global.fetch;
});

describe('fetchClientCaptions track selection', () => {
  test('exact language match -> isCorrectLang true, no tlang', async () => {
    let timedUrl = null;
    installFetch({
      onPlayer: () => playerResponse([track('es'), track('en')]),
      onTimedText: (url) => {
        timedUrl = url;
        return json3Body();
      },
    });

    const result = await fetchClientCaptions('vid', 'es');
    expect(result).not.toBeNull();
    expect(result.isCorrectLang).toBe(true);
    expect(result.snippets).toHaveLength(2); // whitespace + append dropped
    expect(result.snippets[0]).toEqual({ text: 'Hello world', start: 0, duration: 1.5 });
    // fmt=srv3 stripped, json3 requested, NO tlang for an exact match.
    expect(timedUrl).toContain('lang=es');
    expect(timedUrl).toContain('&fmt=json3');
    expect(timedUrl).not.toContain('fmt=srv3');
    expect(timedUrl).not.toContain('tlang=');
  });

  test('regional match prefers manual over auto-generated', async () => {
    let timedUrl = null;
    installFetch({
      // requested "en"; only en-US variants exist, one asr one manual.
      onPlayer: () =>
        playerResponse([track('en-US', { asr: true }), track('en-GB', { asr: false })]),
      onTimedText: (url) => {
        timedUrl = url;
        return json3Body();
      },
    });

    const result = await fetchClientCaptions('vid', 'en');
    expect(result.isCorrectLang).toBe(true);
    // Manual en-GB sorts before asr en-US.
    expect(timedUrl).toContain('lang=en-GB');
    expect(timedUrl).not.toContain('tlang=');
  });

  test('auto-translate when fromLang is an offered translation target', async () => {
    let timedUrl = null;
    installFetch({
      // Video has only French subs; German requested; YouTube offers de as a
      // translation target -> client asks for tlang=de, isCorrectLang true.
      onPlayer: () => playerResponse([track('fr')], ['de', 'es', 'it']),
      onTimedText: (url) => {
        timedUrl = url;
        return json3Body();
      },
    });

    const result = await fetchClientCaptions('vid', 'de');
    expect(result.isCorrectLang).toBe(true);
    expect(timedUrl).toContain('tlang=de');
  });

  test('fallback to source track when fromLang not available or translatable', async () => {
    let timedUrl = null;
    installFetch({
      // Only French; German requested; NO translation targets offered ->
      // fall back to the source track, isCorrectLang false (server translates).
      onPlayer: () => playerResponse([track('fr', { translatable: false })], []),
      onTimedText: (url) => {
        timedUrl = url;
        return json3Body();
      },
    });

    const result = await fetchClientCaptions('vid', 'de');
    expect(result.isCorrectLang).toBe(false);
    expect(timedUrl).toContain('lang=fr');
    expect(timedUrl).not.toContain('tlang=');
  });
});

describe('fetchClientCaptions failure handling', () => {
  test('no captions -> null (server fallback)', async () => {
    installFetch({ onPlayer: () => playerResponse([]) });
    expect(await fetchClientCaptions('vid', 'en')).toBeNull();
  });

  test('non-OK playability -> null', async () => {
    installFetch({
      onPlayer: () => ({ playabilityStatus: { status: 'LOGIN_REQUIRED' } }),
    });
    expect(await fetchClientCaptions('vid', 'en')).toBeNull();
  });

  test('network error -> null', async () => {
    global.fetch = jest.fn(async () => {
      throw new Error('network down');
    });
    expect(await fetchClientCaptions('vid', 'en')).toBeNull();
  });

  test('empty video id -> null without fetching', async () => {
    global.fetch = jest.fn();
    expect(await fetchClientCaptions('', 'en')).toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test('timedtext returns no usable lines -> null', async () => {
    installFetch({
      onPlayer: () => playerResponse([track('en')]),
      onTimedText: () => JSON.stringify({ events: [{ aAppend: 1 }] }),
    });
    expect(await fetchClientCaptions('vid', 'en')).toBeNull();
  });
});

describe('fetchClientCaptions hybrid (backend list) path', () => {
  const API = 'https://api.example.test';

  // Simulate production: the direct innertube POST is CORS-blocked (throws),
  // but our backend /api/caption-tracks returns a signed timedtext URL, and the
  // browser can then download the CORS-open timedtext.
  function installHybridFetch({ backendResponse, backendOk = true, onTimedText }) {
    global.fetch = jest.fn(async (url, options) => {
      if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
        throw new TypeError('Failed to fetch'); // CORS block
      }
      if (typeof url === 'string' && url.includes('/api/caption-tracks')) {
        return {
          ok: backendOk,
          status: backendOk ? 200 : 503,
          json: async () => backendResponse,
        };
      }
      // timedtext download
      const body = onTimedText ? onTimedText(url) : json3Body();
      return {
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => JSON.parse(body),
        text: async () => body,
      };
    });
  }

  test('falls back to backend list when direct innertube is CORS-blocked', async () => {
    let timedUrl = null;
    installHybridFetch({
      backendResponse: {
        video_id: 'vid',
        url: 'https://www.youtube.com/api/timedtext?v=vid&lang=es&fmt=srv3',
        is_correct_lang: true,
        tlang: null,
        language_code: 'es',
      },
      onTimedText: (url) => { timedUrl = url; return json3Body(); },
    });

    const result = await fetchClientCaptions('vid', 'es', API);
    expect(result).not.toBeNull();
    expect(result.isCorrectLang).toBe(true);
    expect(result.snippets).toHaveLength(2);
    // Backend URL used; fmt normalized to json3 in the browser.
    expect(timedUrl).toContain('lang=es');
    expect(timedUrl).toContain('&fmt=json3');
    expect(timedUrl).not.toContain('fmt=srv3');
  });

  test('backend already appended tlang -> browser does not double-append', async () => {
    let timedUrl = null;
    installHybridFetch({
      backendResponse: {
        video_id: 'vid',
        url: 'https://www.youtube.com/api/timedtext?v=vid&lang=es&fmt=srv3&tlang=en',
        is_correct_lang: true,
        tlang: 'en',
        language_code: 'es',
      },
      onTimedText: (url) => { timedUrl = url; return json3Body(); },
    });

    const result = await fetchClientCaptions('vid', 'en', API);
    expect(result.isCorrectLang).toBe(true);
    // Exactly one tlang=en (from the backend URL), not two.
    expect((timedUrl.match(/tlang=/g) || []).length).toBe(1);
    expect(timedUrl).toContain('tlang=en');
  });

  test('backend list fails (blocked) -> null so caller uses server fetch', async () => {
    installHybridFetch({ backendResponse: { error: 'blocked' }, backendOk: false });
    expect(await fetchClientCaptions('vid', 'es', API)).toBeNull();
  });

  test('no apiBaseUrl and direct blocked -> null (no hybrid attempt)', async () => {
    global.fetch = jest.fn(async (url) => {
      if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
        throw new TypeError('Failed to fetch');
      }
      throw new Error('should not fetch anything else');
    });
    // Called without apiBaseUrl -> hybrid is skipped, returns null.
    expect(await fetchClientCaptions('vid', 'es')).toBeNull();
  });
});

describe('fetchClientCaptions Cloudflare Worker relay path', () => {
  const API = 'https://api.example.test';
  const RELAY = 'https://relay.example.workers.dev';

  test('relay is tried before backend when direct is CORS-blocked', async () => {
    const seen = [];
    global.fetch = jest.fn(async (url, options) => {
      if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
        throw new TypeError('Failed to fetch'); // CORS block
      }
      if (url === RELAY) {
        seen.push('relay');
        return {
          ok: true, status: 200,
          json: async () => ({
            video_id: 'vid',
            url: 'https://www.youtube.com/api/timedtext?v=vid&lang=es&fmt=srv3',
            is_correct_lang: true, tlang: null, language_code: 'es',
          }),
        };
      }
      if (typeof url === 'string' && url.includes('/api/caption-tracks')) {
        seen.push('backend'); // must NOT be reached: relay already succeeded
        return { ok: true, status: 200, json: async () => ({}) };
      }
      return {
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => JSON.parse(json3Body()),
        text: async () => json3Body(),
      };
    });

    const result = await fetchClientCaptions('vid', 'es', API, RELAY);
    expect(result).not.toBeNull();
    expect(result.isCorrectLang).toBe(true);
    expect(seen).toEqual(['relay']); // backend not consulted
  });

  test('backend is used when the relay fails', async () => {
    const seen = [];
    global.fetch = jest.fn(async (url) => {
      if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
        throw new TypeError('Failed to fetch');
      }
      if (url === RELAY) {
        seen.push('relay');
        return { ok: false, status: 503, json: async () => ({ error: 'blocked' }) };
      }
      if (typeof url === 'string' && url.includes('/api/caption-tracks')) {
        seen.push('backend');
        return {
          ok: true, status: 200,
          json: async () => ({
            video_id: 'vid',
            url: 'https://www.youtube.com/api/timedtext?v=vid&lang=es&fmt=srv3',
            is_correct_lang: true, tlang: null, language_code: 'es',
          }),
        };
      }
      return {
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => JSON.parse(json3Body()),
        text: async () => json3Body(),
      };
    });

    const result = await fetchClientCaptions('vid', 'es', API, RELAY);
    expect(result).not.toBeNull();
    // Relay is retried on 503 (transient egress rate-limit), then we fall back to
    // the backend, which succeeds. Assert the relay was retried >1x and the
    // backend was consulted exactly once, last.
    const relayCount = seen.filter((s) => s === 'relay').length;
    expect(relayCount).toBeGreaterThan(1);
    expect(seen.filter((s) => s === 'backend')).toEqual(['backend']);
    expect(seen[seen.length - 1]).toBe('backend');
  }, 15000);

  test('relay 503 then success on a later retry -> no backend fallback', async () => {
    const seen = [];
    let relayHits = 0;
    global.fetch = jest.fn(async (url) => {
      if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
        throw new TypeError('Failed to fetch');
      }
      if (url === RELAY) {
        relayHits += 1;
        seen.push('relay');
        // First two calls 503 (throttled egress), third succeeds (fresh PoP).
        if (relayHits < 3) {
          return { ok: false, status: 503, json: async () => ({ error: 'blocked' }) };
        }
        return {
          ok: true, status: 200,
          json: async () => ({
            video_id: 'vid',
            url: 'https://www.youtube.com/api/timedtext?v=vid&lang=es&fmt=srv3',
            is_correct_lang: true, tlang: null, language_code: 'es',
          }),
        };
      }
      if (typeof url === 'string' && url.includes('/api/caption-tracks')) {
        seen.push('backend');
        return { ok: true, status: 200, json: async () => ({}) };
      }
      return {
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => JSON.parse(json3Body()),
        text: async () => json3Body(),
      };
    });

    const result = await fetchClientCaptions('vid', 'es', API, RELAY);
    expect(result).not.toBeNull();
    expect(result.isCorrectLang).toBe(true);
    expect(relayHits).toBe(3); // recovered on the 3rd relay attempt
    expect(seen).not.toContain('backend'); // backend never needed
  }, 15000);
});

describe('overall caption-resolution budget (slow-network / mobile)', () => {
  const API = 'https://api.example.test';
  const RELAY = 'https://relay.example.workers.dev';

  // On a phone the per-call timeouts used to stack: 2 innertube hosts + 6 relay
  // retries + 1 backend try could burn ~111s BEFORE /api/transcript was called,
  // which showed up as a long spinner followed by a failure. A single overall
  // budget has to cap that.
  // The budget is wall-clock, and this Jest version has no
  // advanceTimersByTimeAsync, so instead of stalling each attempt for its full
  // timeout (which would make this test take ~40s) the stub reports the
  // transient failure immediately and we assert on the ATTEMPT COUNT: the budget
  // is what stops the retry loop early.
  test('stops retrying the relay once the budget is spent', async () => {
    {
      let relayHits = 0;
      let backendHits = 0;
      // Each relay attempt "costs" this much of the budget, simulating a slow
      // mobile connection without actually waiting.
      const SIMULATED_COST_MS = 9000;
      let clock = Date.now();
      jest.spyOn(Date, 'now').mockImplementation(() => clock);

      global.fetch = jest.fn((url) => {
        if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
          return Promise.reject(new TypeError('Failed to fetch')); // CORS block
        }
        if (url === RELAY) {
          relayHits += 1;
          clock += SIMULATED_COST_MS;
          // 503 = the transient, retryable egress rate-limit.
          return Promise.resolve({ ok: false, status: 503, json: async () => ({}) });
        }
        if (typeof url === 'string' && url.includes('/api/caption-tracks')) {
          backendHits += 1;
          clock += SIMULATED_COST_MS;
          return Promise.resolve({ ok: false, status: 503, json: async () => ({}) });
        }
        return Promise.resolve({ ok: false, status: 500 });
      });

      await expect(fetchClientCaptions('vid', 'es', API, RELAY)).resolves.toBeNull();

      // Without the budget every one of the 6 relay tries would run. At 9s of
      // simulated cost each, the 25s budget must stop it after ~3.
      expect(relayHits).toBeGreaterThan(0);
      expect(relayHits).toBeLessThan(6);
      // The backend still gets its one last-chance attempt.
      expect(backendHits).toBe(1);

      Date.now.mockRestore();
    }
  });

  test('a fast relay success is unaffected by the budget', async () => {
    global.fetch = jest.fn(async (url) => {
      if (typeof url === 'string' && url.includes('/youtubei/v1/player')) {
        throw new TypeError('Failed to fetch');
      }
      if (url === RELAY) {
        return {
          ok: true, status: 200,
          json: async () => ({
            video_id: 'vid',
            url: 'https://www.youtube.com/api/timedtext?v=vid&lang=es&fmt=srv3',
            is_correct_lang: true, tlang: null, language_code: 'es',
          }),
        };
      }
      return {
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => JSON.parse(json3Body()),
        text: async () => json3Body(),
      };
    });

    const result = await fetchClientCaptions('vid', 'es', API, RELAY);
    expect(result).not.toBeNull();
    expect(result.isCorrectLang).toBe(true);
  });
});
