import { useCallback, useRef, useEffect } from 'react';
import axios from 'axios';
import { useAuth } from './AuthContext';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

const DEBOUNCE_MS = 2000; // 2-second debounce for server saves

/**
 * Hook that provides progress save/load helpers.
 * - Guest users: progress is persisted to localStorage.
 * - Authenticated users: progress is debounced-sent to the Flask API.
 */
export function useProgress() {
  const { accessToken, isAuthenticated } = useAuth();
  const debounceTimerRef = useRef(null);
  const latestPayloadRef = useRef(null);

  // ── Build localStorage key ─────────────────────────────────────────
  const _storageKey = useCallback(
    (youtubeId, transcriptLang, translationLang) =>
      `vidioma_progress_${youtubeId}_${transcriptLang}_${translationLang}`,
    []
  );

  // ── Save progress (dual-path) ─────────────────────────────────────
  const saveProgress = useCallback(
    (payload) => {
      // payload: { youtube_id, transcript_language, translation_language, current_line_index, total_lines, title? }

      // Always save to localStorage (cheap insurance for both guests and auth users)
      const key = _storageKey(
        payload.youtube_id,
        payload.transcript_language,
        payload.translation_language
      );
      localStorage.setItem(key, JSON.stringify(payload));

      // If authenticated, also debounce-send to the backend
      if (isAuthenticated && accessToken) {
        latestPayloadRef.current = payload;

        if (debounceTimerRef.current) {
          clearTimeout(debounceTimerRef.current);
        }

        debounceTimerRef.current = setTimeout(() => {
          const data = latestPayloadRef.current;
          if (!data) return;

          axios
            .post(`${API_BASE_URL}/api/progress/upsert`, data, {
              headers: { Authorization: `Bearer ${accessToken}` },
            })
            .then(() => {
              // Clear only if nothing newer arrived while the request was in
              // flight — otherwise a later save would be dropped.
              if (latestPayloadRef.current === data) latestPayloadRef.current = null;
            })
            .catch((err) => console.error('Progress save failed:', err));
        }, DEBOUNCE_MS);
      }
    },
    [isAuthenticated, accessToken, _storageKey]
  );

  // If the auth token changes (logout / refresh / account switch), cancel any
  // pending debounced save so it can't fire against a stale/invalidated token.
  useEffect(() => {
    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
    };
  }, [accessToken]);

  // ── Load progress (for resume) ────────────────────────────────────
  const loadProgress = useCallback(
    async (youtubeId, transcriptLang, translationLang) => {
      // Read the local value first — it may be fresher than the server if the
      // last debounced/flush save didn't reach the backend.
      let localLine = 0;
      const key = _storageKey(youtubeId, transcriptLang, translationLang);
      try {
        const stored = JSON.parse(localStorage.getItem(key));
        localLine = stored?.current_line_index || 0;
      } catch {
        localLine = 0;
      }

      // For authenticated users, also consult the server, then resume from
      // whichever is further along so a failed server save doesn't cost the
      // user their most recent local progress.
      if (isAuthenticated && accessToken) {
        try {
          const resp = await axios.get(
            `${API_BASE_URL}/api/progress/${youtubeId}`,
            {
              params: {
                transcript_language: transcriptLang,
                translation_language: translationLang,
              },
              headers: { Authorization: `Bearer ${accessToken}` },
            }
          );
          if (resp.data?.progress) {
            const serverLine = resp.data.progress.current_line_index || 0;
            return Math.max(serverLine, localLine);
          }
        } catch (err) {
          console.error('Failed to load server progress:', err);
        }
      }

      return localLine;
    },
    [isAuthenticated, accessToken, _storageKey]
  );

  // ── Flush pending save immediately (e.g. on unmount) ──────────────
  const flushProgress = useCallback(() => {
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }

    const data = latestPayloadRef.current;
    if (data && isAuthenticated && accessToken) {
      // Clear the ref up front so a subsequent flush (e.g. unmount right after
      // pagehide) doesn't re-POST the same payload.
      latestPayloadRef.current = null;
      // Fire-and-forget
      axios
        .post(`${API_BASE_URL}/api/progress/upsert`, data, {
          headers: { Authorization: `Bearer ${accessToken}` },
        })
        .catch(() => {});
    }
  }, [isAuthenticated, accessToken]);

  // Flush a pending debounced save when the tab is hidden/closed so the last
  // couple of seconds of progress aren't lost on refresh or navigation.
  useEffect(() => {
    const handlePageHide = () => flushProgress();
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') flushProgress();
    };
    window.addEventListener('pagehide', handlePageHide);
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      window.removeEventListener('pagehide', handlePageHide);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [flushProgress]);

  return { saveProgress, loadProgress, flushProgress };
}
