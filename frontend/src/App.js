import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import axios from 'axios';
import YouTube from 'react-youtube';
import './App.css';
import { useAuth } from './AuthContext';
import { useProgress } from './useProgress';
import Navbar from './components/Navbar';
import AuthModal from './components/AuthModal';
import Dashboard from './components/Dashboard';
import NamePromptModal from './components/NamePromptModal';
import ClassDashboard from './components/ClassDashboard';
import ClassView from './components/ClassView';
import CreateAssignment from './components/CreateAssignment';
import AssignmentDetail from './components/AssignmentDetail';
import { fetchClientCaptions } from './youtubeCaptions';
import { splitParagraphToLines, getBestWindowSimilarity } from './answerMatching';
import { useTranslations } from './useTranslations';
import { useTranscriptLoader } from './useTranscriptLoader';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');
// Optional Cloudflare Worker caption-list relay. When set, the browser lists
// caption tracks through it (its Cloudflare egress isn't YouTube-IP-blocked)
// before falling back to the backend's /api/caption-tracks. See
// docs/caption-egress.md and cloudflare-worker/.
const CAPTION_RELAY_URL = (process.env.REACT_APP_CAPTION_RELAY_URL || '').replace(/\/$/, '');

// Try to fetch this video's captions in the browser and, on success, attach
// them to the /api/transcript request body. YouTube IP-blocks the backend's
// datacenter host (Render) but not the user's residential browser, so fetching
// client-side is the primary, $0 caption source (it replaced a paid rotating
// proxy). On ANY failure this returns the body unchanged and the backend falls
// back to its own direct fetch, so the load never regresses.
const attachClientCaptions = async (videoId, fromLang, body) => {
  try {
    const captions = await fetchClientCaptions(videoId, fromLang, API_BASE_URL, CAPTION_RELAY_URL);
    if (captions && Array.isArray(captions.snippets) && captions.snippets.length) {
      return {
        ...body,
        client_snippets: captions.snippets,
        client_is_correct_lang: captions.isCorrectLang,
      };
    }
  } catch (_) {
    // fall through to the unmodified body (server-side fetch fallback)
  }
  return body;
};

// Hard client-side timeout for the transcript request. Fetching + (when the
// video lacks native subtitles in the chosen language) fully translating the
// transcript can be slow, but it must never hang forever — without this the
// loading spinner could spin indefinitely if the backend stalls. The backend
// caps its own translation work well under this, so exceeding it means
// something is genuinely stuck and the user should retry.
const TRANSCRIPT_REQUEST_TIMEOUT_MS = 90000;
const TRANSCRIPT_TIMEOUT_MESSAGE =
  "This video took too long to load — building the transcript can be slow for long videos or less common languages. Please try again.";

// Paragraph translations had no client timeout at all, so a stalled request left
// the paragraph 'pending' forever: the translation area kept its skeleton and the
// autostart gate below never got a translation to start on. A bounded request
// instead fails fast, marks the paragraph 'failed', and lets the auto-retry take
// another run at it. This is what keeps the (deliberately unbounded) autostart
// gate from waiting on a request that will never answer.
const TRANSLATE_REQUEST_TIMEOUT_MS = 45000;

// Languages Array with Flag Image URLs
const languages = [
  { code: 'en', name: 'English', icon: 'https://flagcdn.com/w40/us.png' },
  { code: 'es', name: 'Spanish', icon: 'https://flagcdn.com/w40/es.png' },
  { code: 'fr', name: 'French', icon: 'https://flagcdn.com/w40/fr.png' },
  { code: 'de', name: 'German', icon: 'https://flagcdn.com/w40/de.png' },
  { code: 'iw', name: 'Hebrew', icon: 'https://flagcdn.com/w40/il.png' },
  { code: 'it', name: 'Italian', icon: 'https://flagcdn.com/w40/it.png' },
  { code: 'pt', name: 'Portuguese', icon: 'https://flagcdn.com/w40/br.png' },
  { code: 'ja', name: 'Japanese', icon: 'https://flagcdn.com/w40/jp.png' },
  { code: 'ko', name: 'Korean', icon: 'https://flagcdn.com/w40/kr.png' },
  { code: 'zh-CN', name: 'Chinese', icon: 'https://flagcdn.com/w40/cn.png' },
  { code: 'ru', name: 'Russian', icon: 'https://flagcdn.com/w40/ru.png' },
];

// Custom Dropdown Component to handle images
const CustomSelect = ({ value, onChange, options }) => {
  const [isOpen, setIsOpen] = useState(false);
  const selectedOption = options.find(opt => opt.code === value);

  return (
    <div
      className="custom-select-container"
      tabIndex={0}
      onBlur={() => setIsOpen(false)}
    >
      <div className="custom-select-trigger" onClick={() => setIsOpen(!isOpen)}>
        <img src={selectedOption.icon} alt={selectedOption.name} className="flag-icon" />
        <svg className="chevron" viewBox="0 0 24 24" width="18" height="18">
          <path d="M7 10l5 5 5-5z" fill="#333"/>
        </svg>
      </div>
      {isOpen && (
        <div className="custom-select-dropdown">
          {options.map(opt => (
            <div
              key={opt.code}
              className="custom-select-option"
              onMouseDown={() => {
                onChange(opt.code);
                setIsOpen(false);
              }}
            >
              <img src={opt.icon} alt={opt.name} className="flag-icon" />
              <span>{opt.name}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

// Extracts the 11-character video ID from any standard YouTube URL
const extractVideoId = (url) => {
  const regExp = /^.*(youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=|&v=)([^#&?]*).*/;
  const match = url.match(regExp);
  return (match && match[2].length === 11) ? match[2] : null;
};

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

const isCanceledRequestError = (error) =>
  (typeof axios.isCancel === 'function' && axios.isCancel(error)) ||
  error?.code === 'ERR_CANCELED' ||
  error?.name === 'CanceledError';

// A request that exceeded its axios `timeout` (distinct from an intentional
// AbortController cancel) so we can show a "took too long, retry" message.
const isTimeoutError = (error) =>
  error?.code === 'ECONNABORTED' || error?.code === 'ETIMEDOUT';

function App() {
  const { isAuthenticated, loading: authLoading, passwordRecoveryPending, clearPasswordRecovery, userProfile, profileLoading, accessToken } = useAuth();
  const { saveProgress, flushProgress } = useProgress();
  // Mirror the access token into a ref so fire-and-forget assignment saves in
  // callbacks/intervals always use the current token without re-subscribing.
  const accessTokenRef = useRef(accessToken);
  useEffect(() => { accessTokenRef.current = accessToken; }, [accessToken]);
  // Mirror flushProgress into a ref too. flushProgress gets a new identity every
  // time the Supabase access token changes (initial resolve, refresh on tab
  // focus, periodic refresh). The unmount cleanup below must NOT re-run just
  // because that identity changed — doing so would abort the in-flight
  // transcript request and strand the loading spinner (looks like a failed
  // load; a manual retry then works). Calling through the ref lets the cleanup
  // depend only on stable callbacks, so it truly runs only on unmount.
  const flushProgressRef = useRef(flushProgress);
  useEffect(() => { flushProgressRef.current = flushProgress; }, [flushProgress]);

  // ── View state: 'home' | 'player' | 'dashboard' | 'classes' | 'classDetail' | 'createAssignment' | 'assignmentDetail' ──
  const [view, setView] = useState('home');
  const [dashboardKey, setDashboardKey] = useState(0);
  const [selectedClassId, setSelectedClassId] = useState(null);
  const [selectedAssignmentId, setSelectedAssignmentId] = useState(null);
  const [classesKey, setClassesKey] = useState(0);
  const [authModalMode, setAuthModalMode] = useState(null); // null | 'login' | 'signup'
  const [guestBannerDismissed, setGuestBannerDismissed] = useState(false);

  // Assignment playback: when a student launches an assignment, the player runs
  // in "assignment mode" — progress is saved to the assignment (not the personal
  // dashboard) and the student cannot skip ahead of their furthest-reached line.
  const activeAssignmentRef = useRef(null); // { assignmentId, maxLineReached } or null
  const [assignmentBanner, setAssignmentBanner] = useState(null); // { title, dueDate } shown in player

  const [loadingText, setLoadingText] = useState("Extracting audio...");
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState(null); // transcript-fetch failure message (null = no error)
  const [url, setUrl] = useState('');
  const [paragraphs, setParagraphs] = useState([]); // Source-language paragraphs
  const inputRef = useRef(null);
  const lastTimeRef = useRef(0); // Tracks the time to detect scrubbing
  const [transcript, setTranscript] = useState([]);

  const [videoId, setVideoId] = useState('');
  const [player, setPlayer] = useState(null);
  const [isPlayerReady, setIsPlayerReady] = useState(false);
  const [isFinished, setIsFinished] = useState(false); // Tracks if the video is done
  // Raw viewport cursor position; each translation line computes its own local
  // mask coordinates from this so the flashlight reveals whichever line(s) the
  // cursor is near — prev, current, or next — independently.
  const [cursorViewport, setCursorViewport] = useState({ x: -9999, y: -9999 });
  const revealRef = useRef(null);
  const prevLineWrapRef = useRef(null);
  const currentLineWrapRef = useRef(null);
  const nextLineWrapRef = useRef(null);
  const [needsManualPlay, setNeedsManualPlay] = useState(false);
  const playbackAttemptTimeoutsRef = useRef([]);
  const dashboardStartPromptActiveRef = useRef(false);
  // Guards the one-time initial autostart so it fires exactly once per video
  // session, only after the starting paragraph's translation is ready (see the
  // autostart effect). Reset when a new player session begins.
  const initialAutoStartDoneRef = useRef(false);
  // Re-runs the autostart effect while it polls for the starting paragraph's
  // translation. There is no max-wait counter: the video never autostarts before
  // its translation has loaded, so there is nothing to time out.
  const [playbackWaitTick, setPlaybackWaitTick] = useState(0);
  const [playbackLaunchSource, setPlaybackLaunchSource] = useState('home');
  const activePlayerSessionRef = useRef(0);
  const transcriptRequestControllerRef = useRef(null);
  const translationRequestControllersRef = useRef(new Set());

  // TRACKING STATE
  const [currentLineIndex, setCurrentLineIndex] = useState(0);
  const [userInput, setUserInput] = useState('');
  const [showInput, setShowInput] = useState(false);
  const [answered, setAnswered] = useState(false);
  const [isError, setIsError] = useState(false); // Tracks wrong answers
  const [fromLang, setFromLang] = useState('en'); // Default to English
  const [toLang, setToLang] = useState('es');   // Default to Spanish
  // Paragraph translations live in their own hook (see useTranslations.js): the
  // translated text, per-line chunks, per-paragraph status, in-flight guards and
  // the retry path. Declared here because it needs `transcript` and `paragraphs`.
  const {
    translatedParagraphs,
    translatedLinesByParagraph,
    translationStatus,
    retryFailedTranslations,
    resetTranslations,
  } = useTranslations({
    transcript,
    paragraphs,
    currentLineIndex,
    fromLang,
    toLang,
    apiBaseUrl: API_BASE_URL,
    requestTimeoutMs: TRANSLATE_REQUEST_TIMEOUT_MS,
    activePlayerSessionRef,
    translationRequestControllersRef,
    isCanceledRequestError,
  });

  // The three launch paths (home GO, dashboard card, assignment) all load a
  // transcript the same way; the sequence lives in useTranscriptLoader.js so it
  // isn't written out three times.
  const loadTranscript = useTranscriptLoader({
    attachClientCaptions,
    apiBaseUrl: API_BASE_URL,
    requestTimeoutMs: TRANSCRIPT_REQUEST_TIMEOUT_MS,
    timeoutMessage: TRANSCRIPT_TIMEOUT_MESSAGE,
    activePlayerSessionRef,
    transcriptRequestControllerRef,
    isCanceledRequestError,
    isTimeoutError,
    setTranscript,
    setParagraphs,
    setCurrentLineIndex,
    setLoadError,
    setIsLoading,
  });

  const clearPlaybackAttemptTimers = useCallback(() => {
    playbackAttemptTimeoutsRef.current.forEach((timerId) => clearTimeout(timerId));
    playbackAttemptTimeoutsRef.current = [];
  }, []);

  const cancelActivePlayerRequests = useCallback(() => {
    if (transcriptRequestControllerRef.current) {
      transcriptRequestControllerRef.current.abort();
      transcriptRequestControllerRef.current = null;
    }

    translationRequestControllersRef.current.forEach((controller) => controller.abort());
    translationRequestControllersRef.current.clear();
  }, []);

  // Stable refs to the request/timer cleanup callbacks so the unmount-only
  // effect can invoke the latest versions without listing them as deps.
  const cancelActivePlayerRequestsRef = useRef(cancelActivePlayerRequests);
  useEffect(() => { cancelActivePlayerRequestsRef.current = cancelActivePlayerRequests; }, [cancelActivePlayerRequests]);
  const clearPlaybackAttemptTimersRef = useRef(clearPlaybackAttemptTimers);
  useEffect(() => { clearPlaybackAttemptTimersRef.current = clearPlaybackAttemptTimers; }, [clearPlaybackAttemptTimers]);

  const isPlaybackStarted = (state) => state === 1 || state === 3;
  const shouldPromptDashboardManualStart =
    playbackLaunchSource === 'dashboard' && dashboardStartPromptActiveRef.current;

  const attemptPlayback = useCallback(({ seekToCurrentLine = false, allowMutedFallback = false, unmuteAfterStart = false } = {}) => {
    if (!player || !isPlayerReady || typeof player.playVideo !== 'function') {
      setNeedsManualPlay(shouldPromptDashboardManualStart);
      return;
    }

    clearPlaybackAttemptTimers();

    if (seekToCurrentLine && currentLineIndex > 0 && currentLineIndex < transcript.length && typeof player.seekTo === 'function') {
      try {
        player.seekTo(transcript[currentLineIndex].start, true);
      } catch (_) {}
    }

    let mutedFallbackUsed = false;

    const finalizeIfStarted = () => {
      try {
        const state = typeof player.getPlayerState === 'function' ? player.getPlayerState() : -1;
        if (isPlaybackStarted(state)) {
          setNeedsManualPlay(false);
          if (unmuteAfterStart && typeof player.unMute === 'function') {
            try { player.unMute(); } catch (_) {}
          }
          return true;
        }
      } catch (_) {}
      return false;
    };

    const scheduleCheck = (attemptNumber) => {
      const delay = attemptNumber === 0 ? 350 : 900;
      const timerId = setTimeout(() => {
        if (finalizeIfStarted()) return;

        let currentState = -1;
        try {
          currentState = typeof player.getPlayerState === 'function' ? player.getPlayerState() : -1;
        } catch (_) {}

        if (allowMutedFallback && !mutedFallbackUsed && (currentState === -1 || currentState === 2 || currentState === 5)) {
          mutedFallbackUsed = true;
          try { if (typeof player.mute === 'function') player.mute(); } catch (_) {}
          try { player.playVideo(); } catch (_) {}
          scheduleCheck(attemptNumber + 1);
          return;
        }

        if (attemptNumber < 2) {
          try { player.playVideo(); } catch (_) {}
          scheduleCheck(attemptNumber + 1);
          return;
        }

        setNeedsManualPlay(shouldPromptDashboardManualStart);
      }, delay);

      playbackAttemptTimeoutsRef.current.push(timerId);
    };

    try {
      player.playVideo();
      scheduleCheck(0);
    } catch (_) {
      setNeedsManualPlay(shouldPromptDashboardManualStart);
    }
  }, [player, isPlayerReady, currentLineIndex, transcript, clearPlaybackAttemptTimers, shouldPromptDashboardManualStart]);

  // Open reset-password modal when a recovery link is followed
  useEffect(() => {
    if (passwordRecoveryPending) {
      setAuthModalMode('reset');
    }
  }, [passwordRecoveryPending]);

  // Flush pending progress saves on unmount. Depends on NOTHING so it runs only
  // when the App actually unmounts — not on every access-token refresh (which
  // would otherwise abort the in-flight transcript request mid-load). We reach
  // the current callbacks through refs so the empty dep array is safe.
  useEffect(() => {
    return () => {
      activePlayerSessionRef.current += 1;
      cancelActivePlayerRequestsRef.current();
      clearPlaybackAttemptTimersRef.current();
      flushProgressRef.current();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
  let interval;
  if (isLoading) {
    const phrases = [
      "Extracting subtitles...",
      "Analyzing timing...",
      `Translating to ${languages.find(l => l.code === toLang)?.name || 'target language'}...`,
      "Syncing video timelines...",
      "Finalizing the magic...",
      "Almost there..."
    ];
    let currentPhraseIndex = -1;

    interval = setInterval(() => {
      currentPhraseIndex = (currentPhraseIndex + 1) % phrases.length;
      setLoadingText(phrases[currentPhraseIndex]);
    }, 2000); // Changes text every 2 seconds
  }
  return () => clearInterval(interval);
}, [isLoading, toLang]);

  useEffect(() => {
    // Ensure we have a player, we aren't loading, it's ready, and we have a transcript.
    if (player && isPlayerReady && !isLoading && transcript.length > 0) {
      clearPlaybackAttemptTimers();

      if (playbackLaunchSource === 'dashboard' && dashboardStartPromptActiveRef.current) {
        setNeedsManualPlay(true);
        return undefined;
      }

      // Only auto-start once per session. After the first start the user drives
      // playback line-by-line, so re-running this effect (e.g. when a lookahead
      // translation lands and flips translatedParagraphs) must not re-trigger it.
      if (initialAutoStartDoneRef.current) return undefined;

      // Hold the initial autostart until the STARTING paragraph's translation has
      // actually LOADED. The answer box rejects input until that translation
      // exists (processInputSubmit), so starting sooner drops the user onto a
      // playing line they cannot answer while the translation area still shows a
      // skeleton.
      //
      // "Loaded" means the text is present. Deliberately NOT satisfied by:
      //   - status 'failed'  : the auto-retry below is about to run, and starting
      //                        now would begin playback on an unanswerable line
      //   - a wait timeout   : there is no time limit here at all, so the video
      //                        can never start without its translation
      // Neither can strand the user silently: the translation area shows a
      // skeleton while pending and a "we'll retry automatically" message with a
      // Retry now button on failure, and playback is one tap away either way.
      const startLine = transcript[currentLineIndex];
      const startParagraphIdx = startLine?.paragraph ?? 0;
      const startTranslationLoaded = hasOwn(translatedParagraphs, startParagraphIdx);

      if (!startTranslationLoaded) {
        // Poll again shortly. The effect also re-runs when translationStatus or
        // translatedParagraphs change, so a translation that lands sooner starts
        // playback without waiting for the next tick; this timer only covers the
        // case where nothing in the dependency list moves.
        const RECHECK_MS = 400;
        const recheck = setTimeout(() => setPlaybackWaitTick((t) => t + 1), RECHECK_MS);
        return () => clearTimeout(recheck);
      }

      const playTimer = setTimeout(() => {
        initialAutoStartDoneRef.current = true;
        attemptPlayback({ seekToCurrentLine: true, allowMutedFallback: true });
      }, 500);

      return () => {
        clearTimeout(playTimer);
        clearPlaybackAttemptTimers();
      };
    }

    return undefined;
  }, [player, isPlayerReady, isLoading, transcript, attemptPlayback, clearPlaybackAttemptTimers, playbackLaunchSource, currentLineIndex, translationStatus, translatedParagraphs, playbackWaitTick]);

  const beginPlayerSession = useCallback(({ nextUrl, nextVideoId, launchSource }) => {
    activePlayerSessionRef.current += 1;
    const sessionId = activePlayerSessionRef.current;

    cancelActivePlayerRequests();
    clearPlaybackAttemptTimers();

    if (player) {
      try { player.pauseVideo(); } catch (_) {}
    }

    setUrl(nextUrl);
    setVideoId(nextVideoId);
    setIsLoading(true);
    setLoadError(null);
    setView('player');
    setPlaybackLaunchSource(launchSource);
    dashboardStartPromptActiveRef.current = launchSource === 'dashboard';
    initialAutoStartDoneRef.current = false;

    setTranscript([]);
    setParagraphs([]);
    resetTranslations();
    lastTimeRef.current = 0;
    setCurrentLineIndex(0);
    setShowInput(false);
    setUserInput('');
    setAnswered(false);
    setIsError(false);
    setIsFinished(false);
    setPlayer(null);
    setIsPlayerReady(false);
    setNeedsManualPlay(false);

    return sessionId;
  }, [cancelActivePlayerRequests, clearPlaybackAttemptTimers, player, resetTranslations]);

  const handleSubmit = async (e) => {
    e.preventDefault();

    // Extract ID immediately
    const extractedId = extractVideoId(url);
    if (!extractedId) {
      alert("Please enter a valid YouTube URL");
      return;
    }

    const requestUrl = url;
    const sessionId = beginPlayerSession({
      nextUrl: requestUrl,
      nextVideoId: extractedId,
      launchSource: 'home',
    });
    // Entering a video always starts from the beginning, even if it's been
    // watched before. (Saved progress is still tracked as a farthest-reached
    // high-water mark for the dashboard, but we no longer resume from it.)
    setCurrentLineIndex(0);
    await loadTranscript({
      sessionId,
      requestUrl,
      videoId: extractedId,
      fromLang,
      toLang,
      errorMessage: "We couldn't load this video. Check the URL and your connection, then try again.",
    });
  };

  // ── Launch directly from Dashboard card ───────────────────────────
  const handleDashboardSelect = async ({ youtubeId, transcriptLanguage, translationLanguage }) => {
    const requestUrl = `https://www.youtube.com/watch?v=${youtubeId}`;

    setFromLang(transcriptLanguage);
    setToLang(translationLanguage);
    const sessionId = beginPlayerSession({
      nextUrl: requestUrl,
      nextVideoId: youtubeId,
      launchSource: 'dashboard',
    });
    // Always start from the beginning, even when relaunched from the dashboard.
    await loadTranscript({
      sessionId,
      requestUrl,
      videoId: youtubeId,
      fromLang: transcriptLanguage,
      toLang: translationLanguage,
      errorMessage: "We couldn't load this video. Please try again.",
    });
  };

  // ── Launch an assignment into the player (student, no-skip mode) ──────
  const handleStartAssignment = async (assignment) => {
    const youtubeId = assignment.youtube_id || assignment.videos?.youtube_id;
    if (!youtubeId) return;
    const requestUrl = `https://www.youtube.com/watch?v=${youtubeId}`;
    const tLang = assignment.transcript_language || 'en';
    const trLang = assignment.translation_language || 'es';

    setFromLang(tLang);
    setToLang(trLang);
    // Mark assignment mode BEFORE the session begins so save/skip logic applies.
    const startedMax = assignment.progress?.max_line_reached ?? 0;
    // lastActiveAt anchors the active-practice-time delta sent with each save;
    // pendingAttempts accumulates answer submissions since the last save.
    activeAssignmentRef.current = {
      assignmentId: assignment.assignment_id,
      maxLineReached: startedMax,
      lastActiveAt: Date.now(),
      pendingAttempts: 0,
    };
    setAssignmentBanner({
      title: assignment.title || assignment.videos?.title || 'Assignment',
      dueDate: assignment.due_date || null,
    });

    const sessionId = beginPlayerSession({
      nextUrl: requestUrl,
      nextVideoId: youtubeId,
      launchSource: 'assignment',
    });
    await loadTranscript({
      sessionId,
      requestUrl,
      videoId: youtubeId,
      fromLang: tLang,
      toLang: trLang,
      errorMessage: "We couldn't load this assignment. Please try again.",
      // Resume from the assignment's own saved progress (not personal progress).
      startLineFor: (snippets) => {
        const resume = assignment.progress?.current_line_index ?? 0;
        return (resume > 0 && resume < snippets.length) ? resume : 0;
      },
    });
  };

  // Persist progress for the active assignment (kept out of the personal
  // dashboard). Also advances the local no-skip ceiling.
  const saveAssignmentProgress = useCallback((lineIndex, totalLines) => {
    const active = activeAssignmentRef.current;
    if (!active) return;
    if (lineIndex > active.maxLineReached) active.maxLineReached = lineIndex;
    if (!accessTokenRef.current) return;
    // Active practice time: seconds since the last save. The backend caps each
    // delta, so a long idle gap between lines can't inflate the total.
    const now = Date.now();
    const elapsedSeconds = Math.max(0, Math.round((now - (active.lastActiveAt ?? now)) / 1000));
    active.lastActiveAt = now;
    // Submission attempts made since the last save; reset once handed off.
    const attempts = active.pendingAttempts ?? 0;
    active.pendingAttempts = 0;
    axios.post(`${API_BASE_URL}/api/assignments/${active.assignmentId}/progress`, {
      current_line_index: lineIndex,
      total_lines: totalLines,
      elapsed_seconds: elapsedSeconds,
      attempts,
    }, { headers: { Authorization: `Bearer ${accessTokenRef.current}` } })
      .catch((err) => console.error('Assignment progress save failed:', err));
  }, []);

  // ---------------------------------------------------------
  // Reset all player/transcript state to defaults
  // ---------------------------------------------------------
  const resetPlayerState = useCallback(() => {
    flushProgress();
    activePlayerSessionRef.current += 1;
    cancelActivePlayerRequests();
    clearPlaybackAttemptTimers();

    // Safely pause — player may have been destroyed if view changed
    if (player) {
      try { player.pauseVideo(); } catch (_) {}
    }

    setVideoId('');
    setUrl('');
    setTranscript([]);
    setParagraphs([]);
    resetTranslations();
    lastTimeRef.current = 0;
    setCurrentLineIndex(0);
    setShowInput(false);
    setUserInput('');
    setAnswered(false);
    setIsError(false);
    setLoadError(null);
    // Bumping the session above means the in-flight transcript request's finally
    // will skip its own setIsLoading(false) (session mismatch), so clear it here
    // — otherwise leaving the player mid-load leaves the home GO button disabled.
    setIsLoading(false);
    setIsFinished(false);
    setPlayer(null);
    setIsPlayerReady(false);
    setNeedsManualPlay(false);
    setPlaybackLaunchSource('home');
    dashboardStartPromptActiveRef.current = false;
    initialAutoStartDoneRef.current = false;
    activeAssignmentRef.current = null;
    setAssignmentBanner(null);
  }, [player, flushProgress, clearPlaybackAttemptTimers, cancelActivePlayerRequests, resetTranslations]);

  const handleBack = () => {
    // Capture assignment context before reset clears it, so we can return the
    // student to where they launched from (assignment detail or their classes).
    const wasAssignment = !!activeAssignmentRef.current;
    resetPlayerState();
    if (wasAssignment) {
      setView(selectedAssignmentId ? 'assignmentDetail' : 'classes');
    } else {
      setView('home');
    }
  };

  const handleManualPlay = () => {
    // A manual start counts as the initial start, so the autostart effect won't
    // also fire (and won't sit waiting on a translation the user chose to skip).
    initialAutoStartDoneRef.current = true;
    attemptPlayback({ seekToCurrentLine: true, allowMutedFallback: true, unmuteAfterStart: true });
  };

  // Retry translations that previously failed: drop their 'failed' status and
  // clear the in-flight guard so the lazy-loading effect re-requests them.

  // ---------------------------------------------------------
  // THE BRAKE PEDAL (Auto-Pause & Sync Logic)
  // ---------------------------------------------------------
  useEffect(() => {
    let interval;
    if (player && transcript.length > 0) {
      interval = setInterval(async () => {
        const currentTime = await player.getCurrentTime();

        // 1. Detect if the user scrubbed the timeline (jumped > 0.2 seconds)
        if (Math.abs(currentTime - lastTimeRef.current) > 0.2) {

          // Find the correct transcript line for the new time.
          // We want the FIRST line whose "effective end time" is AFTER the current time.
          let actualIndex = transcript.findIndex((line, index) => {
            const nextLine = transcript[index + 1];

            // Calculate when this line effectively ends
            let effectiveEndTime = line.start + line.duration;

            // Account for overlaps: if this line bleeds into the next line, cap its end time
            if (nextLine && effectiveEndTime > nextLine.start) {
              effectiveEndTime = nextLine.start;
            }

            return effectiveEndTime > currentTime;
          });

          // If they rewind to the very beginning before the first subtitle, default to 0
          if (actualIndex === -1 && currentTime < transcript[0].start) {
            actualIndex = 0;
          }

          // NO-SKIP (assignment mode): the student may rewind freely but may not
          // jump PAST the furthest line they've legitimately reached. If they
          // seek ahead, snap the video and UI back to that ceiling.
          const active = activeAssignmentRef.current;
          if (active && actualIndex !== -1 && actualIndex > active.maxLineReached) {
            const capIndex = Math.min(active.maxLineReached, transcript.length - 1);
            const capLine = transcript[capIndex];
            if (capLine && typeof player.seekTo === 'function') {
              try { player.seekTo(capLine.start, true); } catch (_) {}
            }
            lastTimeRef.current = capLine ? capLine.start : currentTime;
            if (capIndex !== currentLineIndex) {
              setCurrentLineIndex(capIndex);
              setShowInput(false);
              setUserInput('');
              setAnswered(false);
            }
            return; // ignore the forward seek entirely
          }

          // If they jumped to a completely different line, resync the UI!
          if (actualIndex !== -1 && actualIndex !== currentLineIndex) {
            setCurrentLineIndex(actualIndex);
            setShowInput(false);
            setUserInput('');
            setAnswered(false);
          }

          if (actualIndex === currentLineIndex) {
            // If they scrubbed but stayed within the same line, just hide the input box and reset state
            setShowInput(false);
            setUserInput('');
            setAnswered(false);
          }
        }

        // Update the tracker for the next loop
        lastTimeRef.current = currentTime;

        // 2. The Auto-Pause Logic (Only runs if we aren't waiting for input)
        if (!showInput) {
          const currentLine = transcript[currentLineIndex];
          // currentLineIndex can briefly outrun transcript.length during
          // transcript swaps or after the video finishes — bail out of this
          // tick rather than crash on undefined.
          if (!currentLine) return;

          // Always cap end-of-line by the next line's start. YouTube fragment
          // durations are approximate and sometimes overshoot into the next
          // line or run past silence; without this cap the 100ms tick can fly
          // past a fragment boundary before pausing, so the user misses the
          // chance to translate a line.
          const nextLineData = transcript[currentLineIndex + 1];
          const fragmentEnd = currentLine.start + currentLine.duration;
          const videoDuration = (typeof player.getDuration === 'function') ? player.getDuration() : Infinity;
          const nextStartCap = nextLineData ? nextLineData.start - 0.1 : Infinity;
          const endTime = Math.min(fragmentEnd, nextStartCap, videoDuration);

          if (currentTime >= endTime) {
            player.pauseVideo();
            setShowInput(true);
          }
        }
      }, 100);
    }
    return () => clearInterval(interval);
  }, [player, transcript, currentLineIndex, showInput]);

  // ---------------------------------------------------------
  // THE GAS PEDAL (Go to next line)
  // ---------------------------------------------------------
  const processInputSubmit = useCallback(() => {
    if (answered) {
      // Move to next line if available
      if (currentLineIndex < transcript.length - 1) {
        const nextIndex = currentLineIndex + 1;
        setCurrentLineIndex(nextIndex);
        setUserInput('');       // Clear text
        setShowInput(false);    // Hide box
        setAnswered(false);    // Reset answered state
        player.playVideo();     // Resume Video

        // Save progress after advancing. Assignment mode saves to the assignment
        // (never the personal dashboard); otherwise the normal dashboard path.
        if (activeAssignmentRef.current) {
          saveAssignmentProgress(nextIndex, transcript.length);
        } else {
          const videoTitle = typeof player.getVideoData === 'function' ? player.getVideoData().title : undefined;
          saveProgress({
            youtube_id: videoId,
            transcript_language: fromLang,
            translation_language: toLang,
            current_line_index: nextIndex,
            total_lines: transcript.length,
            title: videoTitle,
          });
        }
      } else {
        setIsFinished(true); // Mark the video as finished
        player.pauseVideo(); // Just to be safe, ensure the video is paused at the end

        // Save final progress (marks the assignment complete on the server).
        if (activeAssignmentRef.current) {
          saveAssignmentProgress(transcript.length, transcript.length);
        } else {
          const videoTitle = typeof player.getVideoData === 'function' ? player.getVideoData().title : undefined;
          saveProgress({
            youtube_id: videoId,
            transcript_language: fromLang,
            translation_language: toLang,
            current_line_index: transcript.length,
            total_lines: transcript.length,
            title: videoTitle,
          });
        }
      }
    } else {
      const currentLine = transcript[currentLineIndex];
      const paragraphIdx = currentLine?.paragraph ?? 0;
      const paragraphTranslation = translatedParagraphs[paragraphIdx];
      if (!paragraphTranslation) {
        return;
      }

      // Count this submission as an attempt for assignment analytics. It's sent
      // as a delta with the next progress save (see saveAssignmentProgress).
      if (activeAssignmentRef.current) {
        activeAssignmentRef.current.pendingAttempts =
          (activeAssignmentRef.current.pendingAttempts ?? 0) + 1;
      }

      const score = getBestWindowSimilarity(userInput, paragraphTranslation, currentLine?.source);
      if (score >= 0.6) {
        setAnswered(true); // Mark current line as answered
        setIsError(false); // Clear any previous error state
      } else {
        setIsError(true); // Mark as error to show red border
      }
    }
  }, [answered, currentLineIndex, transcript, player, userInput, translatedParagraphs, videoId, fromLang, toLang, saveProgress, saveAssignmentProgress]);

  const handleInputSubmit = (e) => {
    if (e.key === 'Enter') {
      processInputSubmit();
    }
  };

  // Auto-focus the input box when it appears
  useEffect(() => {
    if (showInput && inputRef.current) {
      inputRef.current.focus();
    }
  }, [showInput]);

  // Auto-retry a failed translation for the paragraph the user is currently on,
  // so a transient provider blip doesn't permanently strand them on a line.
  useEffect(() => {
    const currentLine = transcript[currentLineIndex];
    if (!currentLine) return;
    const pIdx = currentLine.paragraph ?? 0;
    if (translationStatus[pIdx] !== 'failed') return;

    const retryTimer = setTimeout(() => {
      retryFailedTranslations();
    }, 4000);
    return () => clearTimeout(retryTimer);
  }, [transcript, currentLineIndex, translationStatus, retryFailedTranslations]);

  // GLOBAL MOUSE + TOUCH TRACKER — store raw viewport coordinates. Each line's
  // reveal layer converts these into its own local box coordinates at render,
  // so proximity to any individual line (prev/current/next) lights just that
  // line rather than all three sharing one container-relative position.
  useEffect(() => {
    const handleGlobalMouseMove = (e) => {
      setCursorViewport({ x: e.clientX, y: e.clientY });
    };

    const handleTouchMove = (e) => {
      const touch = e.touches[0];
      if (!touch) return;
      setCursorViewport({ x: touch.clientX, y: touch.clientY });

      // A drag over the translation area is the flashlight gesture, so it must
      // NOT also scroll the page: on a phone the reveal slid out from under the
      // finger as the page moved with it, which made the translations effectively
      // unreadable. Only that region is claimed, so the rest of the player still
      // scrolls normally.
      const box = revealRef.current?.getBoundingClientRect();
      if (!box) return;
      const insideReveal =
        touch.clientX >= box.left && touch.clientX <= box.right &&
        touch.clientY >= box.top && touch.clientY <= box.bottom;
      if (insideReveal && e.cancelable) e.preventDefault();
    };

    window.addEventListener('mousemove', handleGlobalMouseMove);
    // NOT passive: a passive listener makes preventDefault() a silent no-op, which
    // is why the drag used to scroll and reveal at the same time.
    window.addEventListener('touchmove', handleTouchMove, { passive: false });
    return () => {
      window.removeEventListener('mousemove', handleGlobalMouseMove);
      window.removeEventListener('touchmove', handleTouchMove);
    };
  }, []);

  // ---------------------------------------------------------
  // RENDER
  // ---------------------------------------------------------
  const safeLineIndex = (transcript.length > 0 && currentLineIndex >= transcript.length) ? 0 : currentLineIndex;
  const currentLine = transcript[safeLineIndex];
  const currentParagraphIdx = currentLine?.paragraph ?? 0;
  const currentParagraphTranslation = translatedParagraphs[currentParagraphIdx];
  const currentStatus = translationStatus[currentParagraphIdx];
  const translationPending = transcript.length > 0 && (currentStatus === 'pending' || (!hasOwn(translatedParagraphs, currentParagraphIdx) && currentStatus !== 'failed'));
  const translationFailed = currentStatus === 'failed' && currentParagraphTranslation === undefined;
  const prevLine = safeLineIndex > 0 ? transcript[safeLineIndex - 1] : null;
  const nextLine = safeLineIndex < transcript.length - 1 ? transcript[safeLineIndex + 1] : null;

  // Per-line translated chunks. Prefer backend-aligned chunks (DP over per-line
  // anchor translations — robust to word-order differences across languages).
  // Fall back to a proportional word split only if the server didn't return
  // alignment for a paragraph.
  const translatedLines = useMemo(() => {
    const result = new Array(transcript.length).fill('');
    if (transcript.length === 0) return result;

    let i = 0;
    while (i < transcript.length) {
      const pIdx = transcript[i].paragraph ?? 0;
      let j = i;
      while (j < transcript.length && (transcript[j].paragraph ?? 0) === pIdx) {
        j += 1;
      }
      const lineCount = j - i;
      const aligned = translatedLinesByParagraph[pIdx];
      if (Array.isArray(aligned) && aligned.length === lineCount) {
        for (let k = 0; k < lineCount; k++) {
          result[i + k] = aligned[k] || '';
        }
      } else {
        const paragraphText = translatedParagraphs[pIdx];
        if (paragraphText) {
          const sourceLines = transcript.slice(i, j).map((s) => s.source);
          const chunks = splitParagraphToLines(paragraphText, sourceLines);
          for (let k = 0; k < chunks.length; k++) {
            result[i + k] = chunks[k];
          }
        }
      }
      i = j;
    }
    return result;
  }, [transcript, translatedParagraphs, translatedLinesByParagraph]);

  const currentLineTranslation = translatedLines[safeLineIndex] || '';
  const prevLineTranslation = safeLineIndex > 0 ? (translatedLines[safeLineIndex - 1] || '') : '';
  const nextLineTranslation = safeLineIndex < transcript.length - 1 ? (translatedLines[safeLineIndex + 1] || '') : '';

  // Flashlight radius (px). Must match the `circle <R>px` in the mask below.
  const REVEAL_RADIUS = 60;

  // Build a radial-gradient mask for one line layer, using coordinates LOCAL to
  // that layer's own box (converted from the viewport cursor). When the cursor
  // is farther than the reveal radius from the box vertically, the spotlight
  // simply falls outside the line and reveals nothing — so a cursor sitting low
  // lights only the next line, etc. Returns undefined when the ref isn't
  // mounted yet (mask omitted -> layer hidden until first paint).
  const buildRevealMask = (wrapRef) => {
    const el = wrapRef.current;
    if (!el) return undefined;
    const rect = el.getBoundingClientRect();
    const localX = cursorViewport.x - rect.left;
    const localY = cursorViewport.y - rect.top;
    // Cheap cull: if the cursor is well outside this box vertically, don't
    // reveal anything for it (keeps only the nearby line lit).
    if (localY < -REVEAL_RADIUS || localY > rect.height + REVEAL_RADIUS) {
      return `radial-gradient(circle ${REVEAL_RADIUS}px at -9999px -9999px, black 40%, transparent 100%)`;
    }
    return `radial-gradient(circle ${REVEAL_RADIUS}px at ${localX}px ${localY}px, black 40%, transparent 100%)`;
  };

  if (authLoading) return null; // Wait for auth to initialise

  return (
    <div className="App">
      {/* Auth Modal */}
      {authModalMode && (
        <AuthModal
          mode={authModalMode}
          onClose={() => {
            setAuthModalMode(null);
            if (passwordRecoveryPending) clearPasswordRecovery();
          }}
        />
      )}

      <header className="App-header">
        {/* Navbar */}
        <Navbar
          onDashboard={() => { resetPlayerState(); setDashboardKey(k => k + 1); setView('dashboard'); }}
          onHome={() => { resetPlayerState(); setView('home'); }}
          onOpenAuth={(mode) => setAuthModalMode(mode)}
          onClasses={() => { resetPlayerState(); setClassesKey(k => k + 1); setView('classes'); }}
        />

        {view === 'home' && (
          <a
            href="https://docs.google.com/forms/d/e/1FAIpQLSdy2zTXJ3pJ9GxIzCJFyUhZggE-sN2nrHZ1go6KFFM1JHonEw/viewform?usp=sf_link"
            target="_blank"
            rel="noopener noreferrer"
            className="feedback-link"
          >
            Feedback
          </a>
        )}

        {/* Guest banner */}
        {!isAuthenticated && !guestBannerDismissed && view === 'player' && (
          <div className="guest-banner">
            <span>Sign up to save your progress across sessions.</span>
            <button className="guest-banner-btn" onClick={() => setAuthModalMode('signup')}>
              Sign Up
            </button>
            <button className="guest-banner-dismiss" onClick={() => setGuestBannerDismissed(true)}>
              &times;
            </button>
          </div>
        )}

        {/* ── NAME PROMPT (blocks interaction until profile is complete) ── */}
        {isAuthenticated && !profileLoading && (!userProfile || !userProfile.user_name) && (
          <NamePromptModal />
        )}

        {/* ── DASHBOARD VIEW ──────────────────────────────────── */}
        {view === 'dashboard' && isAuthenticated && (
          <Dashboard key={dashboardKey} onSelectVideo={handleDashboardSelect} />
        )}

        {/* ── CLASSES VIEW ───────────────────────────────────── */}
        {view === 'classes' && isAuthenticated && (
          <ClassDashboard
            key={classesKey}
            onSelectClass={(classId) => { setSelectedClassId(classId); setView('classDetail'); }}
            onCreateAssignment={() => setView('createAssignment')}
          />
        )}

        {/* ── CLASS DETAIL VIEW ──────────────────────────────── */}
        {view === 'classDetail' && isAuthenticated && selectedClassId && (
          <ClassView
            classId={selectedClassId}
            onBack={() => { setClassesKey(k => k + 1); setView('classes'); }}
            onStartAssignment={handleStartAssignment}
            onOpenAssignment={(assignmentId) => { setSelectedAssignmentId(assignmentId); setView('assignmentDetail'); }}
          />
        )}

        {/* ── CREATE ASSIGNMENT VIEW (teacher) ───────────────── */}
        {view === 'createAssignment' && isAuthenticated && (
          <CreateAssignment
            onBack={() => setView('classes')}
            onCreated={() => { setClassesKey(k => k + 1); setView('classes'); }}
          />
        )}

        {/* ── ASSIGNMENT DETAIL VIEW ─────────────────────────── */}
        {view === 'assignmentDetail' && isAuthenticated && selectedAssignmentId && (
          <AssignmentDetail
            assignmentId={selectedAssignmentId}
            onBack={() => { if (selectedClassId) { setView('classDetail'); } else { setView('classes'); } }}
            onStartAssignment={handleStartAssignment}
          />
        )}

        {/* LANDING PAGE UI */}
        {view === 'home' && (
          <div className="landing-container">
            <h1 className="landing-title">Vidioma</h1>

            <form onSubmit={handleSubmit} className="modern-search-bar">
              <div className="search-bar-top">
                {/* Link Icon */}
                <div className="input-icon">
                  <svg viewBox="0 0 24 24" width="20" height="20" fill="#888">
                    <path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/>
                  </svg>
                </div>

                {/* URL Input */}
                <input
                  type="text"
                  placeholder="Paste YouTube URL..."
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  required
                />

                {/* Vertical Divider */}
                <div className="divider"></div>

                {/* Language Selectors */}
                <div className="landing-lang-group">
                  <CustomSelect
                    value={fromLang}
                    onChange={setFromLang}
                    options={languages}
                  />

                  <span className="lang-arrow">&rarr;</span>

                  <CustomSelect
                    value={toLang}
                    onChange={setToLang}
                    options={languages}
                  />
                </div>
              </div>

              {/* Submit Button */}
              <button type="submit" className="go-button" disabled={isLoading}>
                {isLoading ? <div className="button-spinner"></div> : 'GO'}
              </button>
            </form>
          </div>
        )}

        {/* TRANSCRIPT & VIDEO UI (Hides title and search bar when active) */}
        {view === 'player' && videoId && (
          <>
            {/* The New Back Button */}
            <button className="back-button" onClick={handleBack}>
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
                <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
              </svg>
              {assignmentBanner ? 'Back to Assignments' : 'Back to Search'}
            </button>
            {/* Assignment banner — signals no-skip mode + shows the due date */}
            {assignmentBanner && (
              <div className="assignment-player-banner">
                <span className="assignment-player-tag">Assignment</span>
                <span className="assignment-player-title">{assignmentBanner.title}</span>
                {assignmentBanner.dueDate && (
                  <span className="assignment-player-due">
                    Due {new Date(assignmentBanner.dueDate).toLocaleDateString()}
                  </span>
                )}
                <span className="assignment-player-note">Complete every line — you can't skip ahead.</span>
              </div>
            )}
            <div className="content-area">
              {/* Video Player */}
              <div className="video-section">
                <div className="video-wrapper">
                  <YouTube
                    videoId={videoId}
                    opts={{
                      height: '390',
                      width: '640',
                      playerVars: {
                        rel: 0,
                        modestbranding: 1,
                        autoplay: 0,
                        playsinline: 1,
                      }
                    }}
                    onReady={(event) => {
                      setPlayer(event.target);
                      setIsPlayerReady(true);
                    }}
                    onStateChange={(event) => {
                      const state = event?.data;
                      if (isPlaybackStarted(state)) {
                        clearPlaybackAttemptTimers();
                        dashboardStartPromptActiveRef.current = false;
                        setNeedsManualPlay(false);
                      }
                    }}
                    onError={() => {
                      clearPlaybackAttemptTimers();
                      setNeedsManualPlay(playbackLaunchSource === 'dashboard' && dashboardStartPromptActiveRef.current);
                    }}
                  />
                  {needsManualPlay && !isLoading && transcript.length > 0 && (
                    // Disabled until the starting paragraph's translation is in.
                    // Mobile browsers block autoplay, so this button (not the
                    // autostart effect) is how playback begins on a phone, and
                    // starting here while the translation was still pending
                    // dropped the user onto a playing line whose answer box
                    // rejects input. Same rule as the autostart gate above.
                    <button
                      className="tap-to-play-overlay"
                      onClick={handleManualPlay}
                      disabled={translationPending}
                    >
                      <svg viewBox="0 0 24 24" width="48" height="48" fill="#fff">
                        <path d="M8 5v14l11-7z"/>
                      </svg>
                      <span>{translationPending ? 'Preparing translation...' : 'Tap to Start'}</span>
                    </button>
                  )}
                </div>
              </div>

              {/* Focus Mode Display */}
              {isLoading ? (
                <div className="focus-card skeleton-card">
                  <div className="loader-animation">
                    {/* You can style these dots in CSS to pulse */}
                    <div className="pulsing-dot"></div>
                    <div className="pulsing-dot" style={{animationDelay: '0.2s'}}></div>
                    <div className="pulsing-dot" style={{animationDelay: '0.4s'}}></div>
                  </div>
                  <h2 className="current-text" style={{ color: '#888', marginTop: '20px' }}>
                    {loadingText}
                  </h2>
                </div>
              ) : loadError ? (
                <div className="focus-card">
                  <h2 className="current-text" style={{ color: '#e06c6c' }}>
                    Couldn't load this video
                  </h2>
                  <p className="victory-subtitle" style={{ marginTop: 12 }}>
                    {loadError}
                  </p>
                  <div className="input-row" style={{ justifyContent: 'center', gap: 12, marginTop: 16 }}>
                    <button className="go-button" onClick={handleSubmit}>
                      Try Again
                    </button>
                    <button className="go-button victory-button" onClick={handleBack}>
                      Back to Search
                    </button>
                  </div>
                </div>
              ) : transcript.length > 0 && (
                isFinished ? (
                  <div className="focus-card victory-card">
                    <h1 className="victory-title">You Did It!</h1>
                    <p className="victory-subtitle">
                      Awesome job! You successfully translated all <strong>{transcript.length}</strong> sentences of this video.
                    </p>
                    <button className="go-button victory-button" onClick={handleBack}>
                      Translate Another Video
                    </button>
                  </div>
                ) : (
                <div className="focus-card">
                  <div className="scroll-window" key={safeLineIndex}>
                    <div className="scroll-line scroll-line-prev">
                      {prevLine ? prevLine.source : '\u00A0'}
                    </div>
                    <h2 className="scroll-line scroll-line-current current-text">
                      {currentLine.source}
                    </h2>
                    <div className="scroll-line scroll-line-next">
                      {nextLine ? nextLine.source : '\u00A0'}
                    </div>
                  </div>
                  {/* Translation display — mirrors the source scroll with prev/current/next
                      lines. The full paragraph is translated for context, then split per
                      source line for display. */}
                  <div
                    className="reveal-container"
                    ref={revealRef}
                  >
                    {translationPending ? (
                      <div className="translation-loading-block">
                        <div className="translation-skeleton" aria-hidden="true">
                          <span className="translation-skeleton-line" />
                        </div>
                      </div>
                    ) : translationFailed ? (
                      <div className="translation-loading-block">
                        <p className="translation-failed-text">Translation delayed. We'll retry automatically.</p>
                        <button
                          type="button"
                          className="guest-banner-btn"
                          onClick={retryFailedTranslations}
                        >
                          Retry now
                        </button>
                      </div>
                    ) : (
                      <div className="scroll-window translation-scroll-window" key={`t-${safeLineIndex}`}>
                        {/* Prev: blurred base + flashlight reveal layer with its OWN local mask */}
                        <div className="scroll-line scroll-line-prev translation-line-wrap" ref={prevLineWrapRef}>
                          <div className="translation-line">
                            {prevLineTranslation || '\u00A0'}
                          </div>
                          {prevLineTranslation && (
                            <div
                              className="translation-line clear-flashlight-layer"
                              style={{
                                WebkitMaskImage: buildRevealMask(prevLineWrapRef),
                                maskImage: buildRevealMask(prevLineWrapRef),
                              }}
                            >
                              {prevLineTranslation}
                            </div>
                          )}
                        </div>

                        {/* Current line: blurred base + flashlight reveal layer with its OWN local mask */}
                        <div className="scroll-line scroll-line-current translation-current-wrap" ref={currentLineWrapRef}>
                          <h2
                            className="current-text"
                            style={{
                              color: answered ? '#a8e6cf' : '#aaa',
                              filter: answered ? 'none' : 'blur(8px)',
                              transition: 'color 0.3s ease',
                              userSelect: answered ? 'auto' : 'none',
                              marginBottom: 0,
                            }}
                          >
                            {currentLineTranslation || '\u00A0'}
                          </h2>

                          {!answered && currentLineTranslation && (
                            <h2
                              className="current-text clear-flashlight-layer"
                              style={{
                                WebkitMaskImage: buildRevealMask(currentLineWrapRef),
                                maskImage: buildRevealMask(currentLineWrapRef),
                              }}
                            >
                              {currentLineTranslation}
                            </h2>
                          )}
                        </div>

                        <div className="scroll-line scroll-line-next translation-line-wrap" ref={nextLineWrapRef}>
                          <div className="translation-line">
                            {nextLineTranslation || '\u00A0'}
                          </div>
                          {nextLineTranslation && (
                            <div
                              className="translation-line clear-flashlight-layer"
                              style={{
                                WebkitMaskImage: buildRevealMask(nextLineWrapRef),
                                maskImage: buildRevealMask(nextLineWrapRef),
                              }}
                            >
                              {nextLineTranslation}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>

                  {showInput ? (
                    <div className="input-container">
                      <div className="input-row">
                        <input
                          ref={inputRef}
                          type="text"
                          className={`big-input ${isError ? 'input-error' : ''}`}
                          placeholder="Type translation..."
                          value={userInput}
                          onChange={(e) => {
                            setUserInput(e.target.value);
                            if (isError) setIsError(false);
                          }}
                          onKeyDown={handleInputSubmit}
                          readOnly={answered}
                        />
                        <button
                          className="mobile-submit-btn"
                          onClick={processInputSubmit}
                          aria-label={answered ? "Next" : "Check"}
                        >
                          {answered ? (
                            <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                              <path d="M12 4l-1.41 1.41L16.17 11H4v2h12.17l-5.58 5.59L12 20l8-8z"/>
                            </svg>
                          ) : (
                            <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                              <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
                            </svg>
                          )}
                        </button>
                      </div>
                      {/* aria-live so a screen reader announces the grading result.
                          Without it, "Correct!" / "Not quite!" changed silently and a
                          non-sighted user got no feedback at all on their answer. */}
                      <p className="hint" role="status" aria-live="polite">
                        {answered
                          ? "Correct! Press Enter or tap arrow to continue."
                          : translationPending
                            ? "Preparing translation... try again in a moment."
                            : translationFailed
                              ? "Translation is delayed. Try again in a moment."
                          : isError
                            ? "Not quite! Give it another try."
                            : "Press Enter or tap check to submit."}
                      </p>
                    </div>
                  ) : (
                    <p className="listening-indicator">Listening...</p>
                  )}

                  <div className="progress">
                    Sentence {Math.min(safeLineIndex + 1, transcript.length)} of {transcript.length}
                  </div>
                </div>
              )
            )}
            </div>
          </>
        )}
      </header>
    </div>
  );
}

export default App;
