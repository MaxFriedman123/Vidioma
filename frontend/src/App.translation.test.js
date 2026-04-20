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
jest.mock('react-youtube', () => {
  return function MockYouTube() {
    return <div data-testid="youtube-player">YouTube Player</div>;
  };
});

const axios = require('axios');

import App from './App';
import { AuthProvider } from './AuthContext';

const DASHBOARD_PROGRESS = [
  {
    id: 'progress-alpha',
    current_line_index: 0,
    total_lines: 1,
    transcript_language: 'en',
    translation_language: 'es',
    last_accessed_at: '2026-04-15T10:00:00.000Z',
    videos: {
      youtube_id: 'AAAAAAAAAAA',
      title: 'Alpha video',
      thumbnail_url: 'https://img.youtube.com/vi/AAAAAAAAAAA/hqdefault.jpg',
    },
  },
  {
    id: 'progress-bravo',
    current_line_index: 0,
    total_lines: 1,
    transcript_language: 'en',
    translation_language: 'fr',
    last_accessed_at: '2026-04-15T09:00:00.000Z',
    videos: {
      youtube_id: 'BBBBBBBBBBB',
      title: 'Bravo video',
      thumbnail_url: 'https://img.youtube.com/vi/BBBBBBBBBBB/hqdefault.jpg',
    },
  },
];

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });

  return { promise, resolve, reject };
}

function renderApp() {
  return render(
    <AuthProvider>
      <App />
    </AuthProvider>
  );
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

async function openDashboard() {
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
    expect(screen.getByText('Alpha video')).toBeInTheDocument();
    expect(screen.getByText('Bravo video')).toBeInTheDocument();
  });
}

async function openSavedVideo(title) {
  await act(async () => {
    fireEvent.click(screen.getByText(title));
  });

  await waitFor(() => {
    expect(screen.getByTestId('youtube-player')).toBeInTheDocument();
  });
}

describe('Dashboard resume request isolation', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    setUpAuthSession();

    axios.get.mockImplementation((url) => {
      if (url.endsWith('/api/progress')) {
        return Promise.resolve({ data: { progress: DASHBOARD_PROGRESS } });
      }

      return Promise.resolve({ data: {} });
    });
  });

  test('ignores transcript responses from an older dashboard session', async () => {
    const alphaTranscript = createDeferred();

    axios.post.mockImplementation((url, payload) => {
      if (url.includes('/api/transcript')) {
        if (payload.url.includes('AAAAAAAAAAA')) {
          return alphaTranscript.promise;
        }

        if (payload.url.includes('BBBBBBBBBBB')) {
          return Promise.resolve({
            data: {
              snippets: [{ source: 'Bravo line', start: 0, duration: 2, paragraph: 0 }],
              paragraphs: ['Bravo line'],
            },
          });
        }
      }

      if (url.includes('/api/translate')) {
        return Promise.resolve({
          data: {
            translated_paragraphs: ['Bravo traduit'],
          },
        });
      }

      return Promise.resolve({ data: {} });
    });

    await openDashboard();
    await openSavedVideo('Alpha video');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'My Dashboard' }));
    });

    await waitFor(() => {
      expect(screen.getByText('Bravo video')).toBeInTheDocument();
    });

    await openSavedVideo('Bravo video');

    await waitFor(() => {
      expect(screen.getByText('Bravo line')).toBeInTheDocument();
      expect(screen.getAllByText('Bravo traduit').length).toBeGreaterThan(0);
    });

    await act(async () => {
      alphaTranscript.resolve({
        data: {
          snippets: [{ source: 'Alpha line', start: 0, duration: 2, paragraph: 0 }],
          paragraphs: ['Alpha line'],
        },
      });
      await Promise.resolve();
    });

    expect(screen.queryByText('Alpha line')).not.toBeInTheDocument();
    expect(screen.getByText('Bravo line')).toBeInTheDocument();
    expect(screen.getAllByText('Bravo traduit').length).toBeGreaterThan(0);
  });

  test('ignores stale translation responses when switching dashboard videos', async () => {
    const alphaTranslation = createDeferred();
    const bravoTranslation = createDeferred();

    axios.post.mockImplementation((url, payload) => {
      if (url.includes('/api/transcript')) {
        if (payload.url.includes('AAAAAAAAAAA')) {
          return Promise.resolve({
            data: {
              snippets: [{ source: 'Alpha line', start: 0, duration: 2, paragraph: 0 }],
              paragraphs: ['Alpha line'],
            },
          });
        }

        if (payload.url.includes('BBBBBBBBBBB')) {
          return Promise.resolve({
            data: {
              snippets: [{ source: 'Bravo line', start: 0, duration: 2, paragraph: 0 }],
              paragraphs: ['Bravo line'],
            },
          });
        }
      }

      if (url.includes('/api/translate')) {
        const firstParagraph = payload.paragraphs?.[0];
        if (firstParagraph === 'Alpha line') {
          return alphaTranslation.promise;
        }

        if (firstParagraph === 'Bravo line') {
          return bravoTranslation.promise;
        }
      }

      return Promise.resolve({ data: {} });
    });

    await openDashboard();
    await openSavedVideo('Alpha video');

    await waitFor(() => {
      expect(
        axios.post.mock.calls.some(
          ([requestUrl, body]) =>
            requestUrl.includes('/api/translate') &&
            body.paragraphs?.[0] === 'Alpha line' &&
            body.to_lang === 'es'
        )
      ).toBe(true);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'My Dashboard' }));
    });

    await waitFor(() => {
      expect(screen.getByText('Bravo video')).toBeInTheDocument();
    });

    await openSavedVideo('Bravo video');

    await waitFor(() => {
      expect(screen.getByText('Bravo line')).toBeInTheDocument();
      expect(
        axios.post.mock.calls.some(
          ([requestUrl, body]) =>
            requestUrl.includes('/api/translate') &&
            body.paragraphs?.[0] === 'Bravo line' &&
            body.to_lang === 'fr'
        )
      ).toBe(true);
    });

    await act(async () => {
      alphaTranslation.resolve({
        data: {
          translated_paragraphs: ['Linea alfa'],
        },
      });
      await Promise.resolve();
    });

    expect(screen.queryAllByText('Linea alfa')).toHaveLength(0);
    expect(screen.getByText('Bravo line')).toBeInTheDocument();

    await act(async () => {
      bravoTranslation.resolve({
        data: {
          translated_paragraphs: ['Bravo traduit'],
        },
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.getAllByText('Bravo traduit').length).toBeGreaterThan(0);
    });

    expect(screen.queryAllByText('Linea alfa')).toHaveLength(0);
  });
});
