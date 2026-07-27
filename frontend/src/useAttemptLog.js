import { useCallback, useEffect, useRef } from 'react';
import axios from 'axios';

// Records every graded answer so the app stops throwing away its own data.
//
// Before this, a submitted answer produced a boolean and nothing else: whether a
// learner failed a line, what they typed, and which line it was all vanished. The
// only persisted signal was one running attempt counter per assignment, which
// cannot distinguish "stalled on one hard line" from "struggled with everything".
//
// Attempts are BUFFERED and flushed in batches rather than posted per submission.
// A learner retrying a hard line six times would otherwise fire six requests into
// the same per-user rate-limit budget the transcript and translation calls share.

// Flush when this many attempts are buffered, or when the timer below elapses,
// whichever comes first. Small enough that a browser close loses little, large
// enough that a burst of retries is one request.
const FLUSH_AT_COUNT = 5;
const FLUSH_AFTER_MS = 15000;
// Hard cap so a very long session cannot grow the buffer without bound if every
// flush is failing (offline, backend down).
const MAX_BUFFER = 100;

/**
 * @param {object} args
 * @param {string} args.apiBaseUrl
 * @param {React.MutableRefObject<string|null>} args.accessTokenRef
 *        Read at flush time, not at hook creation: the Supabase token is
 *        refreshed periodically and a captured value would go stale.
 */
export function useAttemptLog({ apiBaseUrl, accessTokenRef }) {
  const bufferRef = useRef([]);
  const timerRef = useRef(null);

  const flush = useCallback(async () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const batch = bufferRef.current;
    if (!batch.length) return;
    // Clear before awaiting so attempts recorded during the request are not lost
    // to the splice, and are not double-sent if this one fails.
    bufferRef.current = [];

    const token = accessTokenRef?.current;
    // Anonymous practice has nowhere to store attempts: the table is keyed on a
    // real user. Drop them rather than queue forever.
    if (!token) return;

    try {
      await axios.post(`${apiBaseUrl}/api/attempts`, { attempts: batch }, {
        headers: { Authorization: `Bearer ${token}` },
      });
    } catch (_) {
      // Attempt logging is analytics, never the practice loop. A failure must not
      // surface to the learner or block anything, so the batch is simply dropped.
    }
  }, [apiBaseUrl, accessTokenRef]);

  const recordAttempt = useCallback((attempt) => {
    if (!attempt || !accessTokenRef?.current) return;
    if (bufferRef.current.length >= MAX_BUFFER) return;
    bufferRef.current.push(attempt);

    if (bufferRef.current.length >= FLUSH_AT_COUNT) {
      flush();
      return;
    }
    if (!timerRef.current) {
      timerRef.current = setTimeout(() => flush(), FLUSH_AFTER_MS);
    }
  }, [flush, accessTokenRef]);

  // Flush on tab-hide and unmount so a learner who closes the tab mid-video does
  // not lose the attempts they just made. visibilitychange is the reliable signal
  // on mobile, where pagehide/unload are unreliable.
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === 'hidden') flush();
    };
    document.addEventListener('visibilitychange', onHide);
    window.addEventListener('pagehide', flush);
    return () => {
      document.removeEventListener('visibilitychange', onHide);
      window.removeEventListener('pagehide', flush);
      flush();
    };
  }, [flush]);

  return { recordAttempt, flushAttempts: flush };
}
