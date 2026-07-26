import { useCallback } from 'react';
import axios from 'axios';

// Loads a video's transcript into player state.
//
// Extracted from App.js, which had three near-identical copies of this ~40-line
// sequence (home GO, dashboard card, assignment launch). They differed only in
// which language/id variables they read, the resume rule, and the wording of the
// error message. Everything else (the client-caption attach, the stale-session
// checks, the abort-controller bookkeeping, the error mapping and the isLoading
// reset) was duplicated verbatim. Three copies meant a fix to any of it had to
// be made three times, and the copies had already drifted (one used double
// quotes, one had lost a comment).
//
// Behavior is unchanged from the inlined versions.

/**
 * @param {object} args
 * @param {(videoId: string, fromLang: string, body: object) => Promise<object>} args.attachClientCaptions
 *        Adds browser-fetched captions to the request body when it can.
 * @param {string} args.apiBaseUrl
 * @param {number} args.requestTimeoutMs
 * @param {string} args.timeoutMessage       shown when the request exceeds its timeout
 * @param {React.MutableRefObject<number>} args.activePlayerSessionRef
 * @param {React.MutableRefObject<AbortController|null>} args.transcriptRequestControllerRef
 * @param {(err: any) => boolean} args.isCanceledRequestError
 * @param {(err: any) => boolean} args.isTimeoutError
 * @param {Function} args.setTranscript
 * @param {Function} args.setParagraphs
 * @param {Function} args.setCurrentLineIndex
 * @param {Function} args.setLoadError
 * @param {Function} args.setIsLoading
 */
export function useTranscriptLoader({
  attachClientCaptions,
  apiBaseUrl,
  requestTimeoutMs,
  timeoutMessage,
  activePlayerSessionRef,
  transcriptRequestControllerRef,
  isCanceledRequestError,
  isTimeoutError,
  setTranscript,
  setParagraphs,
  setCurrentLineIndex,
  setLoadError,
  setIsLoading,
}) {
  /**
   * Fetch + install a transcript for one player session.
   *
   * @param {object}   opts
   * @param {number}   opts.sessionId    from beginPlayerSession; every await below
   *                                     re-checks it so a response for a video the
   *                                     user has already left is discarded instead
   *                                     of overwriting the new one's state.
   * @param {string}   opts.requestUrl
   * @param {string}   opts.videoId
   * @param {string}   opts.fromLang
   * @param {string}   opts.toLang
   * @param {string}   opts.errorMessage fallback when the server sends no error text
   * @param {(snippets: Array) => number} [opts.startLineFor]
   *        Which line to open on, given the loaded snippets. Defaults to 0 (start
   *        from the beginning); assignments use it to resume from saved progress.
   */
  return useCallback(async ({
    sessionId,
    requestUrl,
    videoId,
    fromLang,
    toLang,
    errorMessage,
    startLineFor,
  }) => {
    const controller = new AbortController();
    transcriptRequestControllerRef.current = controller;

    try {
      const requestBody = await attachClientCaptions(videoId, fromLang, {
        url: requestUrl,
        from_lang: fromLang,
        to_lang: toLang,
      });
      // The client caption fetch is async; bail if the user moved on meanwhile.
      if (activePlayerSessionRef.current !== sessionId) return;

      const response = await axios.post(`${apiBaseUrl}/api/transcript`, requestBody, {
        signal: controller.signal,
        timeout: requestTimeoutMs,
      });
      if (activePlayerSessionRef.current !== sessionId) return;

      const snippets = response.data.snippets;
      setTranscript(snippets);
      setParagraphs(response.data.paragraphs || []);
      setCurrentLineIndex(startLineFor ? startLineFor(snippets) : 0);
    } catch (error) {
      if (isCanceledRequestError(error) || activePlayerSessionRef.current !== sessionId) {
        return;
      }

      console.error('Transcript load error:', error);
      setLoadError(
        (isTimeoutError(error) && timeoutMessage) ||
        error?.response?.data?.error ||
        errorMessage
      );
    } finally {
      if (transcriptRequestControllerRef.current === controller) {
        transcriptRequestControllerRef.current = null;
      }

      // Only the active session clears the spinner: a stale session doing it
      // would re-enable the home GO button mid-load for the current video.
      if (activePlayerSessionRef.current === sessionId) {
        setIsLoading(false);
      }
    }
  }, [
    attachClientCaptions, apiBaseUrl, requestTimeoutMs, timeoutMessage,
    activePlayerSessionRef, transcriptRequestControllerRef,
    isCanceledRequestError, isTimeoutError,
    setTranscript, setParagraphs, setCurrentLineIndex, setLoadError, setIsLoading,
  ]);
}
