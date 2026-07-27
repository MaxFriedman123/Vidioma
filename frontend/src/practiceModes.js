// Practice modes: what task the learner is actually doing on each line.
//
// The app began as translate-only, and the source line was printed on screen
// while the audio played. That made the audio decorative: the learner read a
// sentence and translated it, never mapping sound to meaning. Listening modes
// hide the source line so it has to be decoded from the audio instead.
//
// DICTATE is the mode with the best grading signal in the app. TRANSLATE and
// LISTEN both grade against a machine translation, so a correct but differently
// worded answer can fail. Dictation grades against the video's own subtitle text,
// which is ground truth, so that whole class of false negative disappears.

import {
  getBestWindowSimilarity,
  getLevenshteinDistance,
  getSimilarity,
  normalizeText,
  segmentUnits,
} from './answerMatching';

export const PRACTICE_MODES = {
  TRANSLATE: 'translate',
  LISTEN: 'listen',
  DICTATE: 'dictate',
};

// Ordered for the UI. `hidesSource` is what makes a mode a listening exercise.
export const PRACTICE_MODE_OPTIONS = [
  {
    id: PRACTICE_MODES.TRANSLATE,
    label: 'Read & Translate',
    short: 'Translate',
    hint: 'The line is shown. Type what it means in your target language.',
    hidesSource: false,
  },
  {
    id: PRACTICE_MODES.LISTEN,
    label: 'Listen & Translate',
    short: 'Listen',
    hint: 'The line is hidden. Listen, then type what it means.',
    hidesSource: true,
  },
  {
    id: PRACTICE_MODES.DICTATE,
    label: 'Listen & Write',
    short: 'Dictation',
    hint: 'The line is hidden. Type exactly what you hear, in the same language.',
    hidesSource: true,
  },
];

export const isListeningMode = (mode) =>
  mode === PRACTICE_MODES.LISTEN || mode === PRACTICE_MODES.DICTATE;

// Dictation is answered in the SOURCE language, the other two in the target
// language. The input's placeholder and lang attribute follow this.
export const answersInSourceLanguage = (mode) => mode === PRACTICE_MODES.DICTATE;

export const getModeOption = (mode) =>
  PRACTICE_MODE_OPTIONS.find((m) => m.id === mode) || PRACTICE_MODE_OPTIONS[0];

/**
 * Word-aware similarity, for dictation only.
 *
 * Character-level Levenshtein cannot separate a typo from a wrong word: measured
 * against "we are no strangers to love", the typo "lovee" scores 0.964 and the
 * genuinely wrong "war" scores 0.852, so no single character threshold accepts
 * the first and rejects the second. Comparing unit by unit fixes that, because a
 * substituted word is one wrong unit out of six regardless of how many letters it
 * happens to share.
 *
 * Each unit is still compared fuzzily, so a one-character slip inside a word
 * counts as very nearly correct rather than as a miss. Uses segmentUnits so the
 * no-space scripts (Chinese, Japanese, Thai) segment per character as they must.
 */
function dictationSimilarity(userText, expectedText) {
  const expectedUnits = segmentUnits(expectedText);
  const userUnits = segmentUnits(userText);
  if (!expectedUnits.length || !userUnits.length) return 0;

  // Credit each expected unit by how well the unit in the same position matches.
  // Dividing by the longer of the two lengths penalises both missing and extra
  // units, so padding an answer with junk cannot raise the score.
  const span = Math.max(expectedUnits.length, userUnits.length);
  let credit = 0;
  let misses = 0;

  for (let i = 0; i < span; i++) {
    const want = expectedUnits[i];
    const got = userUnits[i];
    if (want === undefined || got === undefined) {
      misses += 1;
      continue;
    }
    if (want === got) {
      credit += 1;
      continue;
    }
    // A single edit inside a longer word is a typo ("lov" for "love"), but on a
    // short word it is a different word ("we" for "i", "war" for "was"). So the
    // one-edit allowance only applies once a word is long enough that a slip is
    // likelier than a substitution; below that only an exact match counts, which
    // is what keeps function words strict.
    if (want.length >= 4 && getLevenshteinDistance(got, want) <= 1) {
      credit += 0.9;
      continue;
    }
    const unit = getSimilarity(got, want);
    if (unit >= 0.85) {
      credit += unit;
    } else {
      misses += 1;
    }
  }

  const ratio = credit / span;
  // A wholly wrong unit is a real error and must be able to fail the line on its
  // own. The positional ratio alone cannot do that on a short line: one wrong
  // unit out of seven is 0.857, above any threshold loose enough to forgive a
  // typo. So any genuine miss caps the score below the pass mark, while the
  // ratio still carries how close the attempt was into the attempt log.
  return misses > 0 ? Math.min(ratio, DICTATION_PASS_THRESHOLD - 0.001) : ratio;
}


// Dictation compares against one known-correct string, so it can be held to a
// higher bar than the windowed fuzzy match against machine-translated text.
// Still not 1.0: auto-generated captions lack punctuation and casing, and
// normalizeText already discards both, so what is left is genuine word errors.
export const DICTATION_PASS_THRESHOLD = 0.85;
export const TRANSLATION_PASS_THRESHOLD = 0.6;

export const passThresholdFor = (mode) =>
  mode === PRACTICE_MODES.DICTATE ? DICTATION_PASS_THRESHOLD : TRANSLATION_PASS_THRESHOLD;

/**
 * Grade one submitted answer.
 *
 * Returns { score, passed, expected, threshold } so the caller can both act on
 * the result and log what the learner was actually graded against. `expected` is
 * the text the score was computed from, which is why it is returned rather than
 * re-derived later: the translation cache expires and machine translation is not
 * stable across provider versions.
 *
 * @param {object} args
 * @param {string} args.mode                 one of PRACTICE_MODES
 * @param {string} args.userInput            what the learner typed
 * @param {string} args.sourceLine           the caption line, in the source language
 * @param {string} args.paragraphTranslation the whole paragraph's translation
 */
export function gradeAnswer({ mode, userInput, sourceLine, paragraphTranslation }) {
  const threshold = passThresholdFor(mode);

  if (mode === PRACTICE_MODES.DICTATE) {
    // Ground truth: the subtitle itself. Compared whole rather than windowed,
    // because there is no ambiguity about which text the answer corresponds to.
    const expected = sourceLine || '';
    const normUser = normalizeText(userInput);
    const normExpected = normalizeText(expected);
    const score = normUser && normExpected ? dictationSimilarity(normUser, normExpected) : 0;
    return { score, passed: score >= threshold, expected, threshold };
  }

  // TRANSLATE and LISTEN are graded identically: hiding the source changes the
  // difficulty of the task, not how the answer is judged.
  const score = getBestWindowSimilarity(userInput, paragraphTranslation, sourceLine);
  return {
    score,
    passed: score >= threshold,
    expected: paragraphTranslation || '',
    threshold,
  };
}
