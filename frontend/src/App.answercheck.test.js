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
    getVideoData: jest.fn(() => ({ title: 'Mock video' })),
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

jest.mock('./youtubeCaptions', () => ({
  fetchClientCaptions: jest.fn().mockResolvedValue(null),
}));

import App from './App';
import { AuthProvider } from './AuthContext';

const axios = require('axios');

// The reported bug was only visible through the real submit path: type the
// expected translation, press Enter, and the input still went red. These tests
// drive that path so a regression in the wiring (not just in the matching
// module) is caught too.
const SOURCE_LINES = ['I have been waiting for you all day', 'Lets go now'];

function renderApp() {
  return render(
    <AuthProvider>
      <App />
    </AuthProvider>
  );
}

// Opens a video whose transcript is SOURCE_LINES and whose paragraph
// translation is `translated`, then waits until the answer input is on screen.
async function openVideoWithTranslation(translated) {
  mockGetSession.mockResolvedValue({ data: { session: null } });
  mockOnAuthStateChange.mockReturnValue({
    data: { subscription: { unsubscribe: jest.fn() } },
  });

  const snippets = SOURCE_LINES.map((source, i) => ({
    source,
    start: i * 5,
    duration: 2,
    paragraph: 0,
  }));

  axios.post.mockImplementation((url) => {
    if (url.includes('/api/transcript')) {
      return Promise.resolve({ data: { snippets, paragraphs: [SOURCE_LINES.join(' ')] } });
    }
    if (url.includes('/api/translate')) {
      return Promise.resolve({ data: { translated_paragraphs: [translated] } });
    }
    return Promise.resolve({ data: {} });
  });
  axios.get.mockResolvedValue({ data: {} });

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

  // Drive playback past the first line's end so the app pauses and shows the
  // input, which is the state the user submits from.
  await act(async () => {
    mockLatestPlayer.__setState(1);
  });
  mockLatestPlayer.getCurrentTime.mockImplementation(async () => 3);
  await act(async () => {
    jest.advanceTimersByTime(500);
  });

  await waitFor(() => {
    expect(screen.getByPlaceholderText('Type translation...')).toBeInTheDocument();
  });

  return screen.getByPlaceholderText('Type translation...');
}

async function submit(input, answer) {
  fireEvent.change(input, { target: { value: answer } });
  await act(async () => {
    fireEvent.keyDown(input, { key: 'Enter' });
  });
}

describe('submitting the exact expected translation', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    mockLatestYouTubeProps = null;
    mockLatestPlayer = null;
    localStorage.clear();
    sessionStorage.clear();
  });

  afterEach(() => {
    act(() => {
      jest.runOnlyPendingTimers();
    });
    jest.useRealTimers();
  });

  // One case per script family. The translation of line 1 is submitted while the
  // app holds the whole-paragraph translation, exactly as in the bug report.
  const cases = [
    ['Chinese', '我等了你一整天我们现在走吧', '我等了你一整天'],
    ['Japanese', '一日中あなたを待っていました今すぐ行きましょう', '一日中あなたを待っていました'],
    ['Thai', 'ฉันรอคุณมาทั้งวันไปกันเลย', 'ฉันรอคุณมาทั้งวัน'],
    ['Korean', '나는 하루 종일 너를 기다렸어 지금 가자', '나는 하루 종일 너를 기다렸어'],
    ['Arabic', 'كنت أنتظرك طوال اليوم لنذهب الآن', 'كنت أنتظرك طوال اليوم'],
    ['Hebrew', 'חיכיתי לך כל היום בוא נלך עכשיו', 'חיכיתי לך כל היום'],
    ['Hindi', 'मैं दिन भर तुम्हारा इंतजार कर रहा था अब चलें', 'मैं दिन भर तुम्हारा इंतजार कर रहा था'],
    ['Spanish', 'te he estado esperando todo el dia vamonos ahora', 'te he estado esperando todo el dia'],
  ];

  cases.forEach(([label, paragraphTranslation, exactAnswer]) => {
    test(`${label} is accepted`, async () => {
      const input = await openVideoWithTranslation(paragraphTranslation);

      await submit(input, exactAnswer);

      expect(input.className).not.toContain('input-error');
    });
  });

  test('a wrong answer is still marked with the error state', async () => {
    const input = await openVideoWithTranslation('我等了你一整天我们现在走吧');

    await submit(input, '完全不同的句子内容');

    expect(input.className).toContain('input-error');
  });
});

describe('dragging over the translations does not scroll the page', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    mockLatestYouTubeProps = null;
    mockLatestPlayer = null;
    localStorage.clear();
    sessionStorage.clear();
  });

  afterEach(() => {
    act(() => { jest.runOnlyPendingTimers(); });
    jest.useRealTimers();
  });

  // On a phone, dragging to reveal a translation also scrolled the page, so the
  // reveal slid out from under the finger. The touchmove listener has to be
  // non-passive (a passive one makes preventDefault a silent no-op) and must
  // claim only the reveal area, leaving the rest of the player scrollable.
  const dispatchTouchMove = (x, y) => {
    // jsdom has no TouchEvent constructor; build the shape the handler reads.
    const event = new Event('touchmove', { bubbles: true, cancelable: true });
    event.touches = [{ clientX: x, clientY: y }];
    window.dispatchEvent(event);
    return event;
  };

  test('a drag inside the reveal area is prevented, outside it is not', async () => {
    const input = await openVideoWithTranslation('te he estado esperando todo el dia');
    expect(input).toBeInTheDocument();

    const reveal = document.querySelector('.reveal-container');
    expect(reveal).not.toBeNull();

    // jsdom reports zero-size boxes, so give the reveal area a real rect.
    jest.spyOn(reveal, 'getBoundingClientRect').mockReturnValue({
      left: 20, right: 300, top: 400, bottom: 500, width: 280, height: 100,
      x: 20, y: 400, toJSON: () => {},
    });

    const inside = dispatchTouchMove(160, 450);
    expect(inside.defaultPrevented).toBe(true);

    // A drag over the video (well above the reveal box) must still scroll.
    const outside = dispatchTouchMove(160, 120);
    expect(outside.defaultPrevented).toBe(false);
  });

  test('a touchmove with no touches is ignored', async () => {
    await openVideoWithTranslation('te he estado esperando todo el dia');

    const event = new Event('touchmove', { bubbles: true, cancelable: true });
    event.touches = [];
    window.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
  });
});

describe('accessibility of the translation reveal', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    mockLatestYouTubeProps = null;
    mockLatestPlayer = null;
    localStorage.clear();
    sessionStorage.clear();
  });

  afterEach(() => {
    act(() => { jest.runOnlyPendingTimers(); });
    jest.useRealTimers();
  });

  // The flashlight reveal is driven by pointer position, so without a toggle the
  // translations are unreachable for anyone using a keyboard or a screen reader.
  test('a keyboard-reachable toggle reveals the translation', async () => {
    await openVideoWithTranslation('te he estado esperando todo el dia');

    const toggle = screen.getByRole('button', { name: /show translation/i });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');

    await act(async () => { fireEvent.click(toggle); });

    const pressed = screen.getByRole('button', { name: /hide translation/i });
    expect(pressed).toHaveAttribute('aria-pressed', 'true');
  });

  test('the reveal collapses again when toggled off', async () => {
    await openVideoWithTranslation('te he estado esperando todo el dia');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /show translation/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /hide translation/i }));
    });

    expect(screen.getByRole('button', { name: /show translation/i }))
      .toHaveAttribute('aria-pressed', 'false');
  });

  test('the answer result is announced to assistive tech', async () => {
    const input = await openVideoWithTranslation('te he estado esperando todo el dia');

    // The hint is the element that reports Correct / Not quite, so it has to be
    // a live region or the outcome is silent for a screen-reader user.
    const hint = document.querySelector('.hint');
    expect(hint).toHaveAttribute('aria-live', 'polite');
    expect(hint).toHaveAttribute('role', 'status');

    await submit(input, 'te he estado esperando todo el dia');
    expect(hint.textContent).toMatch(/correct/i);
  });
});
