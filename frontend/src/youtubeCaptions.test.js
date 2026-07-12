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
