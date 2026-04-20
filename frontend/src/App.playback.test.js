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

import App from './App';
import { AuthProvider } from './AuthContext';

const axios = require('axios');

const snippets = [
  { source: 'hello', start: 0, duration: 2, paragraph: 0 },
  { source: 'world', start: 5, duration: 2, paragraph: 0 },
];

const paragraphs = ['hello world'];
const translatedParagraphs = ['hola mundo'];

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

async function openDashboardVideo() {
  setUpAuthSession();
  mockVideoApis();
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

    expect(mockLatestPlayer.seekTo).toHaveBeenCalledWith(5, true);

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
});
