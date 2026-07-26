import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';

// Lazy, just-in-time paragraph translation for the player.
//
// Extracted from App.js, which had grown to ~1600 lines with 47 hooks in one
// component. This owns everything about translation state: the translated text,
// the per-line chunks, the per-paragraph status, the in-flight guards, and the
// retry path. App.js consumes the result and renders it.
//
// Behavior is unchanged from the inlined version; the code moved verbatim.

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

/**
 * @param {object}   args
 * @param {Array}    args.transcript          snippets, each with a `paragraph` index
 * @param {Array}    args.paragraphs          source-language paragraph strings
 * @param {number}   args.currentLineIndex    line the user is on (gates which
 *                                            paragraph is fetched first)
 * @param {string}   args.fromLang
 * @param {string}   args.toLang
 * @param {string}   args.apiBaseUrl
 * @param {number}   args.requestTimeoutMs
 * @param {React.MutableRefObject<number>} args.activePlayerSessionRef
 *        Bumped when the user switches video; a response from an older session is
 *        discarded rather than written into the new video's state.
 * @param {React.MutableRefObject<Set>} args.translationRequestControllersRef
 *        Shared with App.js so leaving the player can abort in-flight requests.
 * @param {(err: any) => boolean} args.isCanceledRequestError
 */
export function useTranslations({
  transcript,
  paragraphs,
  currentLineIndex,
  fromLang,
  toLang,
  apiBaseUrl,
  requestTimeoutMs,
  activePlayerSessionRef,
  translationRequestControllersRef,
  isCanceledRequestError,
}) {
  const [translatedParagraphs, setTranslatedParagraphs] = useState({});
  const [translatedLinesByParagraph, setTranslatedLinesByParagraph] = useState({});
  const [translationStatus, setTranslationStatus] = useState({});
  const [translationRetryNonce, setTranslationRetryNonce] = useState(0);
  // Which paragraphs are in flight, so the lookahead can't double-request one.
  const fetchingRef = useRef(new Set());

  const retryFailedTranslations = useCallback(() => {
    setTranslationStatus((prev) => {
      const updated = { ...prev };
      Object.keys(updated).forEach((idx) => {
        if (updated[idx] === 'failed') {
          delete updated[idx];
          fetchingRef.current.delete(Number(idx));
        }
      });
      return updated;
    });
    setTranslationRetryNonce((n) => n + 1);
  }, []);

  // ---------------------------------------------------------
  // LAZY LOADING PARAGRAPH TRANSLATIONS (Fetch just-in-time)
  // ---------------------------------------------------------
  // Fetch a set of paragraph indices in a single /api/translate request and
  // fold the results into state. Extracted so the CURRENT paragraph and the
  // lookahead paragraphs can be requested SEPARATELY: the current paragraph is
  // what gates the first visible line, so issuing it on its own means it renders
  // as soon as it's ready instead of waiting on the slowest paragraph in a
  // combined batch. Output is unchanged — each paragraph is translated
  // independently on the server, so splitting a batch only changes timing.
  const fetchParagraphGroup = useCallback((indices, sessionId) => {
    if (indices.length === 0) return;

    const paragraphTextsToFetch = indices.map((p) => paragraphs[p]);
    const linesToFetch = indices.map((p) =>
      transcript.filter((s) => (s.paragraph ?? 0) === p).map((s) => s.source || '')
    );

    const controller = new AbortController();
    setTranslationStatus((prev) => {
      const updated = { ...prev };
      indices.forEach((idx) => { updated[idx] = 'pending'; });
      return updated;
    });
    translationRequestControllersRef.current.add(controller);

    axios.post(`${apiBaseUrl}/api/translate`, {
      paragraphs: paragraphTextsToFetch,
      lines: linesToFetch,
      from_lang: fromLang,
      to_lang: toLang,
    }, {
      signal: controller.signal,
      timeout: requestTimeoutMs,
    }).then((response) => {
      if (activePlayerSessionRef.current !== sessionId) return;

      const newTranslations = response.data.translated_paragraphs || [];
      const newLineChunks = response.data.translated_lines || [];

      setTranslatedParagraphs((prev) => {
        const updated = { ...prev };
        indices.forEach((idx, i) => { updated[idx] = newTranslations[i] || ''; });
        return updated;
      });

      setTranslatedLinesByParagraph((prev) => {
        const updated = { ...prev };
        indices.forEach((idx, i) => {
          if (Array.isArray(newLineChunks[i])) updated[idx] = newLineChunks[i];
        });
        return updated;
      });

      setTranslationStatus((prev) => {
        const updated = { ...prev };
        indices.forEach((idx) => { updated[idx] = 'ready'; fetchingRef.current.delete(idx); });
        return updated;
      });
    }).catch((err) => {
      if (isCanceledRequestError(err) || activePlayerSessionRef.current !== sessionId) return;

      console.error("Failed to fetch paragraph translation:", err);
      setTranslationStatus((prev) => {
        const updated = { ...prev };
        indices.forEach((idx) => { updated[idx] = 'failed'; fetchingRef.current.delete(idx); });
        return updated;
      });
    }).finally(() => {
      translationRequestControllersRef.current.delete(controller);
    });
  }, [paragraphs, transcript, fromLang, toLang, apiBaseUrl, requestTimeoutMs,
      activePlayerSessionRef, translationRequestControllersRef, isCanceledRequestError]);

  useEffect(() => {
    if (transcript.length === 0 || paragraphs.length === 0) return;

    const currentLine = transcript[currentLineIndex];
    if (!currentLine) return;

    // The current paragraph gates the first visible line; the following few are
    // a prefetch lookahead. Fetch a paragraph only if we don't already have it
    // and it isn't in flight. A previously 'failed' paragraph has no entry in
    // translatedParagraphs, so it becomes eligible again on re-run (retry nonce
    // bump or line change).
    const currentParagraphIdx = currentLine.paragraph ?? 0;
    const PARAGRAPH_LOOKAHEAD = 2;

    const eligible = (p) =>
      p < paragraphs.length && !hasOwn(translatedParagraphs, p) && !fetchingRef.current.has(p);

    const currentGroup = [];
    if (eligible(currentParagraphIdx)) currentGroup.push(currentParagraphIdx);

    const lookaheadGroup = [];
    for (let p = currentParagraphIdx + 1; p <= currentParagraphIdx + PARAGRAPH_LOOKAHEAD; p++) {
      if (eligible(p)) lookaheadGroup.push(p);
    }

    if (currentGroup.length === 0 && lookaheadGroup.length === 0) return;

    // Reserve in-flight guards before issuing requests.
    [...currentGroup, ...lookaheadGroup].forEach((p) => fetchingRef.current.add(p));

    const sessionId = activePlayerSessionRef.current;
    // Current paragraph on its own request (unblocks the first line ASAP);
    // lookahead as a separate background request. With per-paragraph server
    // caching, splitting adds no duplicate work.
    fetchParagraphGroup(currentGroup, sessionId);
    fetchParagraphGroup(lookaheadGroup, sessionId);
  }, [currentLineIndex, transcript, paragraphs, toLang, fromLang, translatedParagraphs,
      translationRetryNonce, fetchParagraphGroup, activePlayerSessionRef]);

  // Reset when the user switches video, so a new transcript never renders the
  // previous video's translations while its own are still loading.
  const resetTranslations = useCallback(() => {
    setTranslatedParagraphs({});
    setTranslatedLinesByParagraph({});
    setTranslationStatus({});
    fetchingRef.current.clear();
  }, []);

  return {
    translatedParagraphs,
    translatedLinesByParagraph,
    translationStatus,
    retryFailedTranslations,
    resetTranslations,
  };
}
