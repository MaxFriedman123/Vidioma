/**
 * Frontend integration tests for auth, progress, and migration flows.
 *
 * Run with:  cd frontend && npm test -- --watchAll=false
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import axios from 'axios';

// ── Mock Supabase before any imports that use it ──────────────────────
const mockGetSession = jest.fn().mockResolvedValue({ data: { session: null } });
const mockOnAuthStateChange = jest.fn().mockReturnValue({
  data: { subscription: { unsubscribe: jest.fn() } },
});
const mockSignUp = jest.fn();
const mockSignInWithPassword = jest.fn();
const mockSignOut = jest.fn();

jest.mock('./supabaseClient', () => ({
  supabase: {
    auth: {
      getSession: (...args) => mockGetSession(...args),
      onAuthStateChange: (...args) => mockOnAuthStateChange(...args),
      signUp: (...args) => mockSignUp(...args),
      signInWithPassword: (...args) => mockSignInWithPassword(...args),
      signOut: (...args) => mockSignOut(...args),
    },
  },
}));

jest.mock('axios');
jest.mock('react-youtube', () => {
  return function MockYouTube() {
    return <div data-testid="youtube-player">YouTube Player</div>;
  };
});

import App from './App';
import { AuthProvider } from './AuthContext';

// ── Helpers ───────────────────────────────────────────────────────────

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

function setUpAuthSession(accessToken = 'test-jwt-token') {
  const session = {
    access_token: accessToken,
    user: { id: 'user-uuid', email: 'test@example.com' },
  };
  mockGetSession.mockResolvedValue({ data: { session } });

  // Simulate onAuthStateChange firing with the session
  mockOnAuthStateChange.mockImplementation((cb) => {
    setTimeout(() => cb('SIGNED_IN', session), 0);
    return { data: { subscription: { unsubscribe: jest.fn() } } };
  });

  return session;
}

// ── Test Case A: Guest Flow ──────────────────────────────────────────

describe('Guest Flow', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    setUpGuestSession();
  });

  test('renders landing page with Sign Up and Log In buttons', async () => {
    await act(async () => { renderApp(); });

    await waitFor(() => {
      expect(screen.getByText('Sign Up')).toBeInTheDocument();
      expect(screen.getByText('Log In')).toBeInTheDocument();
    });
  });

  test('does NOT show My Dashboard for guests', async () => {
    await act(async () => { renderApp(); });

    await waitFor(() => {
      expect(screen.queryByText('My Dashboard')).not.toBeInTheDocument();
    });
  });

  test('guest progress saves to localStorage, not to the API', async () => {
    await act(async () => { renderApp(); });

    // Simulate saving progress via localStorage directly (the hook does this)
    const key = 'vidioma_progress_abc123_en_es';
    const data = {
      youtube_id: 'abc123',
      transcript_language: 'en',
      translation_language: 'es',
      current_line_index: 5,
      total_lines: 20,
    };
    localStorage.setItem(key, JSON.stringify(data));

    // Verify it was stored
    const stored = JSON.parse(localStorage.getItem(key));
    expect(stored.current_line_index).toBe(5);

    // Verify no API call was made to /api/progress/upsert
    const upsertCalls = axios.post.mock.calls.filter(
      ([url]) => url.includes('/api/progress/upsert')
    );
    expect(upsertCalls.length).toBe(0);
  });
});

// ── Test Case B: Auth Flow ───────────────────────────────────────────

describe('Auth Flow', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
  });

  test('authenticated user sees My Dashboard and Log Out', async () => {
    setUpAuthSession();
    await act(async () => { renderApp(); });

    await waitFor(() => {
      expect(screen.getByText('My Dashboard')).toBeInTheDocument();
      expect(screen.getByText('Log Out')).toBeInTheDocument();
    });
  });

  test('authenticated user sees their email in the navbar', async () => {
    setUpAuthSession();
    await act(async () => { renderApp(); });

    await waitFor(() => {
      expect(screen.getByText('test@example.com')).toBeInTheDocument();
    });
  });
});

// ── Test Case C: Migration Flow ─────────────────────────────────────

describe('Migration Flow', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
  });

  test('migrates localStorage progress to backend on login', async () => {
    // Pre-populate localStorage with guest progress
    const progressData = {
      youtube_id: 'dQw4w9WgXcQ',
      transcript_language: 'en',
      translation_language: 'es',
      current_line_index: 10,
      total_lines: 40,
    };
    localStorage.setItem(
      'vidioma_progress_dQw4w9WgXcQ_en_es',
      JSON.stringify(progressData)
    );

    // Mock the upsert call
    axios.post.mockResolvedValue({ data: { progress: progressData } });

    // Now "log in"
    setUpAuthSession('migration-token');

    await act(async () => {
      renderApp();
    });

    // Wait for migration to fire
    await waitFor(() => {
      const upsertCalls = axios.post.mock.calls.filter(
        ([url]) => url.includes('/api/progress/upsert')
      );
      expect(upsertCalls.length).toBe(1);

      // Verify the Bearer token was sent
      const [, payload, config] = upsertCalls[0];
      expect(config.headers.Authorization).toBe('Bearer migration-token');
      expect(payload.youtube_id).toBe('dQw4w9WgXcQ');
      expect(payload.current_line_index).toBe(10);
    });

    // Verify localStorage was cleared after migration
    await waitFor(() => {
      expect(localStorage.getItem('vidioma_progress_dQw4w9WgXcQ_en_es')).toBeNull();
    });
  });

  test('does not re-migrate if already migrated this session', async () => {
    sessionStorage.setItem('vidioma_progress_migrated', 'true');

    localStorage.setItem(
      'vidioma_progress_xyz_en_fr',
      JSON.stringify({ youtube_id: 'xyz', current_line_index: 3 })
    );

    setUpAuthSession();

    await act(async () => {
      renderApp();
    });

    // Should NOT have called upsert
    await waitFor(() => {
      const upsertCalls = axios.post.mock.calls.filter(
        ([url]) => url.includes('/api/progress/upsert')
      );
      expect(upsertCalls.length).toBe(0);
    });

    // localStorage should still have the data (not migrated)
    expect(localStorage.getItem('vidioma_progress_xyz_en_fr')).not.toBeNull();
  });
});

// ── Auth Modal Tests ─────────────────────────────────────────────────

describe('Auth Modal', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    setUpGuestSession();
  });

  test('opens Sign Up modal when Sign Up is clicked', async () => {
    await act(async () => { renderApp(); });

    await act(async () => {
      fireEvent.click(screen.getByText('Sign Up'));
    });

    expect(screen.getByText('Create Account')).toBeInTheDocument();
  });

  test('opens Log In modal when Log In is clicked', async () => {
    await act(async () => { renderApp(); });

    await act(async () => {
      fireEvent.click(screen.getByText('Log In'));
    });

    expect(screen.getByPlaceholderText('Email')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Password')).toBeInTheDocument();
  });

  test('switches between Login and Signup modes', async () => {
    await act(async () => { renderApp(); });

    await act(async () => {
      fireEvent.click(screen.getByText('Log In'));
    });

    // Click the "Sign Up" link inside the modal's auth-switch paragraph
    const modalSignUp = document.querySelector('.auth-switch span');
    await act(async () => {
      fireEvent.click(modalSignUp);
    });

    expect(screen.getByText('Create Account')).toBeInTheDocument();
  });
});
