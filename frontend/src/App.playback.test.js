import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';

const mockGetSession = jest.fn().mockResolvedValue({ data: { session: null } });
const mockOnAuthStateChange = jest.fn().mockReturnValue({
  data: { subscription: { unsubscribe: jest.fn() } },
});

jest.mock('./supabaseClient', () => ({
  supabase: {
    auth: {
      getSession: (...args) => mockGetSession(...args),
      onAuthStateChange: (...args) => mockOnAuthStateChange(...args),
      signUp: jest.fn(),
      signInWithPassword: jest.fn(),
      signOut: jest.fn(),
    },
  },
}));

jest.mock('axios', () => ({
  get: jest.fn(),
  post: jest.fn(),
}));

let mockLatestYouTubeProps = null;
let mockLatestPlayer = null;

const buildMockPlayer = () => {
  let state = -1;
  return {
    playVideo: jest.fn(),
    pauseVideo: jest.fn(),
    seekTo: jest.fn(),
    mute: jest.fn(),
    unMute: jest.fn(),
    getIframe: jest.fn(() => ({ src: 'https://www.youtube.com/embed/mock' })),
    getPlayerState: jest.fn(() => state),
    getCurrentTime: jest.fn(async () => 0),
    getDuration: jest.fn(() => 500),
    __setState: (nextState) => {
      state = nextState;
      if (mockLatestYouTubeProps?.onStateChange) {
        mockLatestYouTubeProps.onStateChange({ data: nextState });
      }
    },
  };
};

jest.mock('react-youtube', () => {
  const ReactModule = require('react');

  return function MockYouTube(props) {
    mockLatestYouTubeProps = props;

    ReactModule.useEffect(() => {
      mockLatestPlayer = buildMockPlayer();
      if (props.onReady) {
        props.onReady({ target: mockLatestPlayer });
      }
    }, [props.videoId]);

    return <div data-testid="youtube-player">YouTube Player</div>;
  };
});
// The client-side YouTube caption fetch hits the network; mock it to null so
// tests deterministically exercise the server-fetch fallback (the /api/transcript
// axios mock), matching behavior when the browser can't reach YouTube.
jest.mock('./youtubeCaptions', () => ({
  fetchClientCaptions: jest.fn().mockResolvedValue(null),
}));

import App from './App';
import { AuthProvider } from './AuthContext';

const axios = require('axios');

const snippets = [
  { source: 'hello', start: 0, duration: 2, paragraph: 0 },
  { source: 'world', start: 5, duration: 2, paragraph: 0 },
];

const paragraphs = ['hello world'];
const translatedParagraphs = ['hola mundo'];

function createDeferred() {
  let resolve;
  const promise = new Promise((res) => { resolve = res; });
  return { promise, resolve };
}

function renderApp() {
  return render(
    <AuthProvider>
      <App />
    </AuthProvider>
  );
}

function setUpGuestSession() {
  mockGetSession.mockResolvedValue({ data: { session: null } });
  mockOnAuthStateChange.mockReturnValue({
    data: { subscription: { unsubscribe: jest.fn() } },
  });
}

function setUpAuthSession(accessToken = 'dashboard-token') {
  const session = {
    access_token: accessToken,
    user: { id: 'user-uuid', email: 'dashboard@example.com' },
  };

  mockGetSession.mockResolvedValue({ data: { session } });
  mockOnAuthStateChange.mockReturnValue({
    data: { subscription: { unsubscribe: jest.fn() } },
  });
}

function mockVideoApis() {
  axios.post.mockImplementation((url) => {
    if (url.includes('/api/transcript')) {
      return Promise.resolve({ data: { snippets, paragraphs } });
    }
    if (url.includes('/api/translate')) {
      return Promise.resolve({ data: { translated_paragraphs: translatedParagraphs } });
    }
    return Promise.resolve({ data: {} });
  });
}

async function openHomeVideo() {
  setUpGuestSession();
  mockVideoApis();

  await act(async () => {
    renderApp();
  });

  fireEvent.change(screen.getByPlaceholderText('Paste YouTube URL...'), {
    target: { value: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' },
  });

  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'GO' }));
  });

  await waitFor(() => {
    expect(screen.getByTestId('youtube-player')).toBeInTheDocument();
  });

  await waitFor(() => {
    expect(axios.post).toHaveBeenCalledWith(
      expect.stringContaining('/api/transcript'),
      expect.any(Object),
      expect.any(Object)
    );
  });
}

// `translatePromise` lets a caller hold the paragraph translation pending for the
// whole load, which is the state the Tap to Start gate cares about.
async function openDashboardVideo({ translatePromise } = {}) {
  setUpAuthSession();
  mockVideoApis();
  if (translatePromise) {
    axios.post.mockImplementation((url) => {
      if (url.includes('/api/transcript')) {
        return Promise.resolve({ data: { snippets, paragraphs } });
      }
      if (url.includes('/api/translate')) {
        return translatePromise;
      }
      return Promise.resolve({ data: {} });
    });
  }
  axios.get.mockImplementation((url) => {
    if (url.endsWith('/api/progress')) {
      return Promise.resolve({
        data: {
          progress: [
            {
              id: 'progress-1',
              current_line_index: 1,
              total_lines: snippets.length,
              transcript_language: 'en',
              translation_language: 'es',
              last_accessed_at: '2026-04-14T12:00:00.000Z',
              videos: {
                youtube_id: 'dQw4w9WgXcQ',
                title: 'Saved video',
                thumbnail_url: 'https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg',
              },
            },
          ],
        },
      });
    }

    return Promise.resolve({ data: {} });
  });

  await act(async () => {
    renderApp();
  });

  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'My Dashboard' })).toBeInTheDocument();
  });

  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'My Dashboard' }));
  });

  await waitFor(() => {
    expect(screen.getByText('Saved video')).toBeInTheDocument();
  });

  await act(async () => {
    fireEvent.click(screen.getByText('Saved video'));
  });

  await waitFor(() => {
    expect(screen.getByTestId('youtube-player')).toBeInTheDocument();
  });

  await waitFor(() => {
    expect(axios.post).toHaveBeenCalledWith(
      expect.stringContaining('/api/transcript'),
      expect.objectContaining({
        url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        from_lang: 'en',
        to_lang: 'es',
      }),
      expect.any(Object)
    );
  });
}

describe('Playback overlay behavior', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    mockLatestYouTubeProps = null;
    mockLatestPlayer = null;
    localStorage.clear();
    sessionStorage.clear();
    axios.get.mockResolvedValue({ data: {} });
    axios.post.mockResolvedValue({ data: {} });
    setUpGuestSession();
  });

  afterEach(() => {
    act(() => {
      jest.runOnlyPendingTimers();
    });
    jest.useRealTimers();
  });

  test('does not show Tap to Start for videos opened from the home page', async () => {
    await openHomeVideo();

    await act(async () => {
      jest.advanceTimersByTime(3200);
    });

    act(() => {
      mockLatestYouTubeProps?.onError?.();
    });

    expect(screen.queryByText('Tap to Start')).not.toBeInTheDocument();
  });

  test('shows Tap to Start automatically for saved videos opened from the dashboard', async () => {
    await openDashboardVideo();

    await waitFor(() => {
      expect(screen.getByText('Tap to Start')).toBeInTheDocument();
    });

    expect(mockLatestPlayer.playVideo).not.toHaveBeenCalled();
  });

  test('manual tap keeps dashboard overlay visible if playback is still blocked', async () => {
    await openDashboardVideo();

    fireEvent.click(await screen.findByText('Tap to Start'));

    // Videos always start from the beginning now (line 0), so a manual tap
    // attempts playback but does not seek to any saved resume position.
    expect(mockLatestPlayer.playVideo).toHaveBeenCalled();
    expect(mockLatestPlayer.seekTo).not.toHaveBeenCalled();

    await act(async () => {
      jest.advanceTimersByTime(3200);
    });

    expect(screen.getByText('Tap to Start')).toBeInTheDocument();
  });

  test('manual tap clears dashboard overlay once playback state changes to playing', async () => {
    await openDashboardVideo();

    fireEvent.click(await screen.findByText('Tap to Start'));

    act(() => {
      mockLatestPlayer.__setState(1);
    });

    await act(async () => {
      jest.advanceTimersByTime(400);
    });

    expect(screen.queryByText('Tap to Start')).not.toBeInTheDocument();
  });

  test('Tap to Start is disabled until the starting translation is ready', async () => {
    // Mobile browsers block autoplay, so on a phone this button is how playback
    // begins, bypassing the autostart gate entirely. Tapping it while the
    // paragraph translation was still pending dropped the user onto a playing
    // line whose answer box refuses input.
    const translation = createDeferred();
    await openDashboardVideo({ translatePromise: translation.promise });

    const pending = await screen.findByText('Preparing translation...');
    const button = pending.closest('button');
    expect(button).toBeDisabled();

    // Tapping while pending must not start playback.
    await act(async () => { fireEvent.click(button); });
    expect(mockLatestPlayer.playVideo).not.toHaveBeenCalled();

    // Once the translation lands the button becomes usable.
    await act(async () => {
      translation.resolve({ data: { translated_paragraphs: translatedParagraphs, translated_lines: [['hola', 'mundo']] } });
    });
    const ready = await screen.findByText('Tap to Start');
    expect(ready.closest('button')).not.toBeDisabled();

    await act(async () => { fireEvent.click(ready); });
    expect(mockLatestPlayer.playVideo).toHaveBeenCalled();
  });

  test('home autostart waits for the starting paragraph translation before playing', async () => {
    // Transcript resolves immediately (e.g. server had it cached), but the
    // translation is deferred — the regression: the video used to start playing
    // while the translation was still pending, stranding the user on a line the
    // answer box refuses (no paragraph translation yet).
    setUpGuestSession();
    const translation = createDeferred();
    axios.post.mockImplementation((url) => {
      if (url.includes('/api/transcript')) {
        return Promise.resolve({ data: { snippets, paragraphs } });
      }
      if (url.includes('/api/translate')) {
        return translation.promise;
      }
      return Promise.resolve({ data: {} });
    });

    await act(async () => { renderApp(); });
    fireEvent.change(screen.getByPlaceholderText('Paste YouTube URL...'), {
      target: { value: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'GO' }));
    });
    await waitFor(() => {
      expect(screen.getByTestId('youtube-player')).toBeInTheDocument();
    });

    // Let the autostart effect's 500ms timer and a couple of recheck ticks fire.
    await act(async () => { jest.advanceTimersByTime(2000); });

    // Translation still pending -> the video must NOT have started.
    expect(mockLatestPlayer.playVideo).not.toHaveBeenCalled();

    // Translation lands -> autostart is now allowed to fire.
    await act(async () => {
      translation.resolve({ data: { translated_paragraphs: translatedParagraphs, translated_lines: [['hola', 'mundo']] } });
    });
    await act(async () => { jest.advanceTimersByTime(600); });

    expect(mockLatestPlayer.playVideo).toHaveBeenCalled();
  });

  test('home autostart never plays while the translation is unresolved', async () => {
    // The video must not start until the starting paragraph's translation has
    // loaded, with no time limit: there used to be a ~12s safety valve that
    // started playback anyway, which dropped the user onto a line the answer box
    // refuses. Waiting is visible (skeleton / retry message), never silent.
    setUpGuestSession();
    const translation = createDeferred(); // never resolved
    axios.post.mockImplementation((url) => {
      if (url.includes('/api/transcript')) {
        return Promise.resolve({ data: { snippets, paragraphs } });
      }
      if (url.includes('/api/translate')) {
        return translation.promise;
      }
      return Promise.resolve({ data: {} });
    });

    await act(async () => { renderApp(); });
    fireEvent.change(screen.getByPlaceholderText('Paste YouTube URL...'), {
      target: { value: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'GO' }));
    });
    await waitFor(() => {
      expect(screen.getByTestId('youtube-player')).toBeInTheDocument();
    });

    // Well past the old 12s valve (40 rechecks x 400ms + the 500ms start timer).
    // Each recheck is a state update that schedules the next timer, so drive them
    // one at a time with a React flush between, the way they'd fire in real time.
    for (let i = 0; i < 40; i++) {
      await act(async () => { jest.advanceTimersByTime(400); });
    }
    await act(async () => { jest.advanceTimersByTime(600); });
    expect(mockLatestPlayer.playVideo).not.toHaveBeenCalled();

    // And it starts as soon as the translation finally lands.
    await act(async () => {
      translation.resolve({ data: { translated_paragraphs: translatedParagraphs, translated_lines: [['hola', 'mundo']] } });
    });
    await act(async () => { jest.advanceTimersByTime(600); });
    expect(mockLatestPlayer.playVideo).toHaveBeenCalled();
  });

  test('home autostart stays held when the translation FAILS', async () => {
    // A failed translation used to count as "settled" and start playback. The
    // answer box rejects input without a translation, so playback stays held
    // while the auto-retry runs.
    setUpGuestSession();
    let translateCalls = 0;
    axios.post.mockImplementation((url) => {
      if (url.includes('/api/transcript')) {
        return Promise.resolve({ data: { snippets, paragraphs } });
      }
      if (url.includes('/api/translate')) {
        translateCalls += 1;
        // Fail the first attempt, succeed once the auto-retry fires.
        if (translateCalls === 1) return Promise.reject(new Error('provider down'));
        return Promise.resolve({ data: { translated_paragraphs: translatedParagraphs, translated_lines: [['hola', 'mundo']] } });
      }
      return Promise.resolve({ data: {} });
    });

    await act(async () => { renderApp(); });
    fireEvent.change(screen.getByPlaceholderText('Paste YouTube URL...'), {
      target: { value: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'GO' }));
    });
    await waitFor(() => {
      expect(screen.getByTestId('youtube-player')).toBeInTheDocument();
    });

    // The failure has landed; playback must still be held.
    await act(async () => { jest.advanceTimersByTime(1500); });
    expect(mockLatestPlayer.playVideo).not.toHaveBeenCalled();

    // The auto-retry (4s) succeeds, and only then does the video start.
    await act(async () => { jest.advanceTimersByTime(4000); });
    await act(async () => { jest.advanceTimersByTime(600); });
    expect(translateCalls).toBeGreaterThan(1);
    expect(mockLatestPlayer.playVideo).toHaveBeenCalled();
  });
});
