import React, { useState, useEffect, useRef, useCallback } from 'react';
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

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

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

// Helper function to strip line breaks, punctuation, and extra spaces
const normalizeText = (text) => {
  if (!text) return '';
  return text
    .normalize("NFD") // Normalize accented characters
    .replace(/[\u0300-\u036f]/g, "") // Remove diacritics
    .replace(/[\n\r]+/g, ' ')       // Replace line breaks with spaces
    .replace(/['".,/#!$%^&*;:{}=\-_`´ˆ˜¨~()¡¿?]/g, '') // Remove common punctuation
    .replace(/\s{2,}/g, ' ')        // Replace multiple spaces with a single space
    .trim()                         // Remove leading/trailing spaces
    .toLowerCase();                 // Make it all lowercase
};

// Calculate the Levenshtein distance (number of edits required)
const getLevenshteinDistance = (a, b) => {
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;

  const matrix = [];
  for (let i = 0; i <= b.length; i++) { matrix[i] = [i]; }
  for (let j = 0; j <= a.length; j++) { matrix[0][j] = j; }

  for (let i = 1; i <= b.length; i++) {
    for (let j = 1; j <= a.length; j++) {
      if (b.charAt(i - 1) === a.charAt(j - 1)) {
        matrix[i][j] = matrix[i - 1][j - 1];
      } else {
        matrix[i][j] = Math.min(
          matrix[i - 1][j - 1] + 1, // substitution
          matrix[i][j - 1] + 1,     // insertion
          matrix[i - 1][j] + 1      // deletion
        );
      }
    }
  }
  return matrix[b.length][a.length];
};

// Convert Levenshtein distance into a percentage (0.0 to 1.0)
const getSimilarity = (str1, str2) => {
  const distance = getLevenshteinDistance(str1, str2);
  const maxLength = Math.max(str1.length, str2.length);
  if (maxLength === 0) return 1.0;
  return (maxLength - distance) / maxLength;
};

function App() {
  const { isAuthenticated, loading: authLoading, passwordRecoveryPending, clearPasswordRecovery, userProfile, profileLoading } = useAuth();
  const { saveProgress, loadProgress, flushProgress } = useProgress();

  // ── View state: 'home' | 'player' | 'dashboard' | 'classes' | 'classDetail' ──
  const [view, setView] = useState('home');
  const [dashboardKey, setDashboardKey] = useState(0);
  const [selectedClassId, setSelectedClassId] = useState(null);
  const [classesKey, setClassesKey] = useState(0);
  const [authModalMode, setAuthModalMode] = useState(null); // null | 'login' | 'signup'
  const [guestBannerDismissed, setGuestBannerDismissed] = useState(false);

  const [loadingText, setLoadingText] = useState("Extracting audio...");
  const [isLoading, setIsLoading] = useState(false);
  const [url, setUrl] = useState('');
  const [translatedTranscript, setTranslatedTranscript] = useState({});
  const [translationStatus, setTranslationStatus] = useState({});
  const fetchingRef = useRef(new Set()); // Tracks which lines are currently being fetched
  const inputRef = useRef(null);
  const lastTimeRef = useRef(0); // Tracks the time to detect scrubbing
  const [transcript, setTranscript] = useState([]);
  const [videoId, setVideoId] = useState('');
  const [player, setPlayer] = useState(null);
  const [isPlayerReady, setIsPlayerReady] = useState(false);
  const [isFinished, setIsFinished] = useState(false); // Tracks if the video is done
  const [revealPos, setRevealPos] = useState({ x: -999, y: -999 });
  const revealRef = useRef(null);
  const [needsManualPlay, setNeedsManualPlay] = useState(false);
  const playbackAttemptTimeoutsRef = useRef([]);
  const dashboardStartPromptActiveRef = useRef(false);
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

  // Flush pending progress saves on unmount
  useEffect(() => {
    return () => {
      activePlayerSessionRef.current += 1;
      cancelActivePlayerRequests();
      clearPlaybackAttemptTimers();
      flushProgress();
    };
  }, [flushProgress, cancelActivePlayerRequests, clearPlaybackAttemptTimers]);

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

      const playTimer = setTimeout(() => {
        attemptPlayback({ seekToCurrentLine: true, allowMutedFallback: true });
      }, 500);

      return () => {
        clearTimeout(playTimer);
        clearPlaybackAttemptTimers();
      };
    }

    return undefined;
  }, [player, isPlayerReady, isLoading, transcript, attemptPlayback, clearPlaybackAttemptTimers, playbackLaunchSource]);

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
    setView('player');
    setPlaybackLaunchSource(launchSource);
    dashboardStartPromptActiveRef.current = launchSource === 'dashboard';

    setTranscript([]);
    setTranslatedTranscript({});
    setTranslationStatus({});
    fetchingRef.current.clear();
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
  }, [cancelActivePlayerRequests, clearPlaybackAttemptTimers, player]);

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
    const controller = new AbortController();
    transcriptRequestControllerRef.current = controller;
    setCurrentLineIndex(0); // Reset to start — may be overridden by loadProgress
    try {
      const response = await axios.post(`${API_BASE_URL}/api/transcript`, {
        url: requestUrl,
        from_lang: fromLang,
        to_lang: toLang
       }, {
        signal: controller.signal,
      });
      if (activePlayerSessionRef.current !== sessionId) {
        return;
      }

      setTranscript(response.data.snippets);

      // Try to resume from saved progress
      const savedLine = await loadProgress(extractedId, fromLang, toLang);
      if (activePlayerSessionRef.current !== sessionId) {
        return;
      }

      if (savedLine > 0) {
        setCurrentLineIndex(savedLine);
      }
    } catch (error) {
      if (isCanceledRequestError(error) || activePlayerSessionRef.current !== sessionId) {
        return;
      }

      console.error("Error:", error);
      setIsError(true);
    } finally {
      if (transcriptRequestControllerRef.current === controller) {
        transcriptRequestControllerRef.current = null;
      }

      if (activePlayerSessionRef.current === sessionId) {
        setIsLoading(false);
      }
    }
  };

  // ── Launch directly from Dashboard card ───────────────────────────
  const handleDashboardSelect = async ({ youtubeId, transcriptLanguage, translationLanguage, startLine }) => {
    const requestUrl = `https://www.youtube.com/watch?v=${youtubeId}`;

    setFromLang(transcriptLanguage);
    setToLang(translationLanguage);
    const sessionId = beginPlayerSession({
      nextUrl: requestUrl,
      nextVideoId: youtubeId,
      launchSource: 'dashboard',
    });
    const controller = new AbortController();
    transcriptRequestControllerRef.current = controller;

    try {
      const response = await axios.post(`${API_BASE_URL}/api/transcript`, {
        url: requestUrl,
        from_lang: transcriptLanguage,
        to_lang: translationLanguage,
      }, {
        signal: controller.signal,
      });

      if (activePlayerSessionRef.current !== sessionId) {
        return;
      }

      const snippets = response.data.snippets;
      setTranscript(snippets);
      // Clamp startLine: if finished (>= total), restart from 0
      const safeStart = (startLine && startLine < snippets.length) ? startLine : 0;
      setCurrentLineIndex(safeStart);
    } catch (error) {
      if (isCanceledRequestError(error) || activePlayerSessionRef.current !== sessionId) {
        return;
      }

      console.error('Error:', error);
      setIsError(true);
    } finally {
      if (transcriptRequestControllerRef.current === controller) {
        transcriptRequestControllerRef.current = null;
      }

      if (activePlayerSessionRef.current === sessionId) {
        setIsLoading(false);
      }
    }
  };

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
    setTranslatedTranscript({});
    setTranslationStatus({});
    fetchingRef.current.clear();
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
    setPlaybackLaunchSource('home');
    dashboardStartPromptActiveRef.current = false;
  }, [player, flushProgress, clearPlaybackAttemptTimers, cancelActivePlayerRequests]);

  const handleBack = () => {
    resetPlayerState();
    setView('home');
  };

  const handleManualPlay = () => {
    attemptPlayback({ seekToCurrentLine: true, allowMutedFallback: true, unmuteAfterStart: true });
  };

  // ---------------------------------------------------------
  // LAZY LOADING TRANSLATIONS (Fetch just-in-time)
  // ---------------------------------------------------------
  useEffect(() => {
    if (transcript.length === 0) return;

    const indicesToTranslate = [];
    const snippetsToTranslate = [];

    const LOOKAHEAD = 5; // How many lines to translate in advance
    const endIndex = Math.min(currentLineIndex + LOOKAHEAD, transcript.length);

    for (let i = currentLineIndex; i < endIndex; i++) {
      if (!hasOwn(translatedTranscript, i) && !fetchingRef.current.has(i)) {
        indicesToTranslate.push(i);
        snippetsToTranslate.push({
          'source': transcript[i].source,
          'start': transcript[i].start,
          'duration': transcript[i].duration
        });
        fetchingRef.current.add(i); // Mark as fetching so we don't duplicate requests
      }
    }

    // If we found lines that need translating, send them to our new endpoint
    if (snippetsToTranslate.length > 0) {
      const sessionId = activePlayerSessionRef.current;
      const controller = new AbortController();

      setTranslationStatus(prev => {
        const updated = { ...prev };
        indicesToTranslate.forEach(idx => {
          updated[idx] = 'pending';
        });
        return updated;
      });

      translationRequestControllersRef.current.add(controller);

      axios.post(`${API_BASE_URL}/api/translate`, {
        snippets: snippetsToTranslate,
        from_lang: fromLang,
        to_lang: toLang
      }, {
        signal: controller.signal,
      }).then(response => {
        if (activePlayerSessionRef.current !== sessionId) {
          return;
        }

        const newTranslations = response.data.translated_snippets;

        // Save the new translations into our dictionary object
        setTranslatedTranscript(prev => {
          const updated = { ...prev };
          indicesToTranslate.forEach((idx, i) => {
            updated[idx] = newTranslations[i].source;
          });
          return updated;
        });

        setTranslationStatus(prev => {
          const updated = { ...prev };
          indicesToTranslate.forEach(idx => {
            updated[idx] = 'ready';
            fetchingRef.current.delete(idx);
          });
          return updated;
        });
      }).catch(err => {
        if (isCanceledRequestError(err) || activePlayerSessionRef.current !== sessionId) {
          return;
        }

        console.error("Failed to fetch translation chunk:", err);
        // Remove from the fetching set so it can try again later
        setTranslationStatus(prev => {
          const updated = { ...prev };
          indicesToTranslate.forEach(idx => {
            updated[idx] = 'failed';
            fetchingRef.current.delete(idx);
          });
          return updated;
        });
      }).finally(() => {
        translationRequestControllersRef.current.delete(controller);
      });
    }
  }, [currentLineIndex, transcript, toLang, fromLang, translatedTranscript]);

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
          const endTime =
          currentLineIndex < transcript.length - 1 && currentLine.start + currentLine.duration > transcript[currentLineIndex + 1].start
          ? transcript[currentLineIndex + 1].start - .2
          : Math.min(currentLine.start + currentLine.duration, player.getDuration());

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

        // Save progress after advancing
        const videoTitle = typeof player.getVideoData === 'function' ? player.getVideoData().title : undefined;
        saveProgress({
          youtube_id: videoId,
          transcript_language: fromLang,
          translation_language: toLang,
          current_line_index: nextIndex,
          total_lines: transcript.length,
          title: videoTitle,
        });
      } else {
        setIsFinished(true); // Mark the video as finished
        player.pauseVideo(); // Just to be safe, ensure the video is paused at the end

        // Save final progress
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
    } else {
      const expectedTranslation = translatedTranscript[currentLineIndex];
      if (!expectedTranslation) {
        return;
      }

      if (getSimilarity(normalizeText(userInput), normalizeText(expectedTranslation)) >= 0.6) {
        setAnswered(true); // Mark current line as answered
        setIsError(false); // Clear any previous error state
      } else {
        setIsError(true); // Mark as error to show red border
      }
    }
  }, [answered, currentLineIndex, transcript, player, userInput, translatedTranscript, videoId, fromLang, toLang, saveProgress]);

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

  // GLOBAL MOUSE + TOUCH TRACKER
  useEffect(() => {
    const updateRevealPos = (clientX, clientY) => {
      if (revealRef.current) {
        const rect = revealRef.current.getBoundingClientRect();
        setRevealPos({
          x: clientX - rect.left,
          y: clientY - rect.top,
        });
      }
    };

    const handleGlobalMouseMove = (e) => {
      updateRevealPos(e.clientX, e.clientY);
    };

    const handleTouchMove = (e) => {
      const touch = e.touches[0];
      if (touch) {
        updateRevealPos(touch.clientX, touch.clientY);
      }
    };

    window.addEventListener('mousemove', handleGlobalMouseMove);
    window.addEventListener('touchmove', handleTouchMove, { passive: true });
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
  const currentTranslation = translatedTranscript[safeLineIndex];
  const currentStatus = translationStatus[safeLineIndex];
  const translationPending = transcript.length > 0 && (currentStatus === 'pending' || (!hasOwn(translatedTranscript, safeLineIndex) && currentStatus !== 'failed'));
  const translationFailed = currentStatus === 'failed' && currentTranslation === undefined;

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
          />
        )}

        {/* ── CLASS DETAIL VIEW ──────────────────────────────── */}
        {view === 'classDetail' && isAuthenticated && selectedClassId && (
          <ClassView
            classId={selectedClassId}
            onBack={() => { setClassesKey(k => k + 1); setView('classes'); }}
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
              Back to Search
            </button>
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
                    <button className="tap-to-play-overlay" onClick={handleManualPlay}>
                      <svg viewBox="0 0 24 24" width="48" height="48" fill="#fff">
                        <path d="M8 5v14l11-7z"/>
                      </svg>
                      <span>Tap to Start</span>
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
              ) : transcript.length > 0 && (
                isFinished ? (
                  <div className="focus-card victory-card">
                    <h1 className="victory-title">You Did It!</h1>
                    <p className="victory-subtitle">
                      Awesome job! You successfully translated all <strong>{transcript.length}</strong> lines of this video.
                    </p>
                    <button className="go-button victory-button" onClick={handleBack}>
                      Translate Another Video
                    </button>
                  </div>
                ) : (
                <div className="focus-card">
                  <h2 className="current-text">
                    {currentLine.source}
                  </h2>
                  {/* The Flashlight Reveal Container */}
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
                      <p className="translation-failed-text">Translation delayed. Keep going, we will retry shortly.</p>
                    ) : (
                      <>
                        {/* Layer 1: The Base Text (Blurred unless answered) */}
                        <h2
                          className="current-text"
                          style={{
                            color: answered ? '#a8e6cf' : '#aaa',
                            filter: answered ? 'none' : 'blur(8px)',
                            transition: 'color 0.3s ease',
                            userSelect: answered ? 'auto' : 'none',
                            marginBottom: 0
                          }}
                        >
                          {currentTranslation}
                        </h2>

                        {/* Layer 2: The Clear Text (Masked globally by the mouse position) */}
                        {!answered && (
                          <h2
                            className="current-text clear-flashlight-layer"
                            style={{
                              WebkitMaskImage: `radial-gradient(circle 60px at ${revealPos.x}px ${revealPos.y}px, black 40%, transparent 100%)`,
                              maskImage: `radial-gradient(circle 60px at ${revealPos.x}px ${revealPos.y}px, black 40%, transparent 100%)`
                            }}
                          >
                            {currentTranslation}
                          </h2>
                        )}
                      </>
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
                      <p className="hint">
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
                    Line {currentLineIndex + 1} of {transcript.length}
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
