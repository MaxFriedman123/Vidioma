import { useCallback, useRef } from 'react';
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
            .catch((err) => console.error('Progress save failed:', err));
        }, DEBOUNCE_MS);
      }
    },
    [isAuthenticated, accessToken, _storageKey]
  );

  // ── Load progress (for resume) ────────────────────────────────────
  const loadProgress = useCallback(
    async (youtubeId, transcriptLang, translationLang) => {
      // Try server first for auth users
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
            return resp.data.progress.current_line_index || 0;
          }
        } catch (err) {
          console.error('Failed to load server progress:', err);
        }
      }

      // Fallback to localStorage
      const key = _storageKey(youtubeId, transcriptLang, translationLang);
      try {
        const stored = JSON.parse(localStorage.getItem(key));
        return stored?.current_line_index || 0;
      } catch {
        return 0;
      }
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
      // Fire-and-forget
      axios
        .post(`${API_BASE_URL}/api/progress/upsert`, data, {
          headers: { Authorization: `Bearer ${accessToken}` },
        })
        .catch(() => {});
    }
  }, [isAuthenticated, accessToken]);

  return { saveProgress, loadProgress, flushProgress };
}
