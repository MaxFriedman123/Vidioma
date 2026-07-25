// Text normalization and fuzzy matching for the user's typed translation.
//
// Extracted from App.js so the matching rules can be unit-tested directly:
// they are pure string logic, and the bugs they had were only reachable through
// a full player render otherwise.
//
// Everything here has to behave the same for every writing system. The original
// implementation was written against space-delimited Latin text and silently
// rejected correct answers in scripts that don't work that way (see the unit
// counting note on segmentUnits and the punctuation note on normalizeText).

// Zero-width and bidi-control characters. These ride along invisibly when text
// is copied out of a rendered page (YouTube captions, a translation widget), so
// a pasted answer can be visually identical to the expected text and still
// compare unequal. They carry no meaning for matching purposes, so drop them.
// Written as escapes on purpose: these characters are invisible, so spelling them
// literally would make this line unreadable, and a stray U+2028 would even break
// the source line itself.
const INVISIBLE_RE =
  /[\u00ad\u034f\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]/gu;

// Marks a learner is not expected to type, because everyday writing omits them:
// Latin combining accents (already handled before), Hebrew niqqud, and Arabic
// harakat plus the purely decorative tatweel.
//
// Deliberately NOT included: Devanagari matras and Thai tone marks. Those change
// which word is written (मैं vs मे, or a different Thai tone = a different word),
// so stripping them would make wrong answers pass.
const OPTIONAL_MARKS_RE =
  /[\u0300-\u036f\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7\u0610-\u061a\u0640\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e8\u06ea-\u06ed]/gu;

// Strip line breaks, punctuation, and extra spaces so only the meaningful
// characters are compared.
export const normalizeText = (text) => {
  if (!text) return '';
  return (
    text
      // NFKC folds compatibility forms a user or a caption track may supply
      // instead of the plain characters: full-width Latin/digits/punctuation
      // (１！Ａ), half-width katakana, and Arabic presentation-form ligatures.
      .normalize('NFKC')
      .replace(INVISIBLE_RE, '')
      // Decompose so the optional marks above separate from their base letters,
      // then recompose. NFC restores Hangul syllables and Indic clusters, so
      // only the marks that were removed are actually gone.
      .normalize('NFD')
      .replace(OPTIONAL_MARKS_RE, '')
      .normalize('NFC')
      // Punctuation and symbols in EVERY script, via Unicode properties. The
      // previous hand-written character class listed only Latin/Spanish marks,
      // so CJK 。、！？, the Arabic comma and question mark, and the Devanagari
      // danda all survived normalization and counted as differences: a typed
      // answer was held to a stricter standard in those scripts than in Latin.
      .replace(/[\p{P}\p{S}]/gu, '')
      // Every kind of whitespace, not just the ASCII space. A pasted answer can
      // contain a non-breaking or ideographic space, which is invisible to the
      // user but is not a word separator to a plain split(' ').
      .replace(/\s+/gu, ' ')
      .trim()
      .toLowerCase()
  );
};

// Calculate the Levenshtein distance (number of edits required)
export const getLevenshteinDistance = (a, b) => {
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
export const getSimilarity = (str1, str2) => {
  const distance = getLevenshteinDistance(str1, str2);
  const maxLength = Math.max(str1.length, str2.length);
  if (maxLength === 0) return 1.0;
  return (maxLength - distance) / maxLength;
};

// Scripts that do not put spaces between words. For these, splitting on
// whitespace yields ~1 token for an entire paragraph, which is useless both for
// laying out per-line translations and for comparing an answer, so they are
// segmented per character instead.
//
// Covers Japanese kana, Han (incl. the supplementary planes), Thai, Lao, Khmer,
// Burmese, and Tibetan. Korean is deliberately absent: Hangul is written with
// spaces between words, so it segments correctly as words.
const NO_SPACE_SCRIPT_RE =
  /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\u0e00-\u0e7f\u0e80-\u0eff\u0f00-\u0fff\u1000-\u109f\u1780-\u17ff]|[\u{20000}-\u{3ffff}]/gu;

export const isNoSpaceScript = (text) => {
  if (!text) return false;
  const noSpaceChars = (text.match(NO_SPACE_SCRIPT_RE) || []).length;
  const nonSpace = (text.match(/\S/gu) || []).length;
  return nonSpace > 0 && noSpaceChars / nonSpace >= 0.3;
};

// The comparable "units" of a string: words for space-delimited scripts,
// characters for the scripts listed above.
//
// This is what makes the length comparisons below meaningful across writing
// systems. Counting a 7-character Chinese sentence as "1 word" made it look
// shorter than any Latin source line, which is what caused correct answers to be
// rejected outright.
export const segmentUnits = (text) => {
  if (!text) return [];
  if (isNoSpaceScript(text)) return Array.from(text).filter((ch) => !/\s/u.test(ch));
  return text.trim().split(/\s+/u).filter(Boolean);
};

// Split a paragraph-level translation into per-source-line chunks, proportional
// to each source line's word count within the paragraph. Translation accuracy
// still comes from the whole-paragraph translation. This split is just so the
// user can see prev/current/next translated chunks alongside the source scroll.
export const splitParagraphToLines = (paragraphText, sourceLineTexts) => {
  const n = sourceLineTexts.length;
  if (n === 0) return [];
  if (!paragraphText) return sourceLineTexts.map(() => '');
  if (n === 1) return [paragraphText.trim()];

  const noSpace = isNoSpaceScript(paragraphText);
  const joiner = noSpace ? '' : ' ';
  const targetWords = segmentUnits(paragraphText);
  const totalTargetWords = targetWords.length;
  if (totalTargetWords === 0) return sourceLineTexts.map(() => '');

  const sourceWordCounts = sourceLineTexts.map(
    (s) => segmentUnits(s || '').length || 1
  );
  const totalSourceWords = sourceWordCounts.reduce((a, b) => a + b, 0);

  const chunks = [];
  let consumed = 0;
  for (let i = 0; i < n; i++) {
    let size;
    if (i === n - 1) {
      size = totalTargetWords - consumed;
    } else {
      const share = sourceWordCounts[i] / totalSourceWords;
      const remaining = totalTargetWords - consumed;
      const linesLeft = n - i;
      // Round proportional share, but guarantee at least 1 unit for each
      // remaining line (so later chunks aren't starved).
      size = Math.max(1, Math.round(share * totalTargetWords));
      size = Math.min(size, remaining - (linesLeft - 1));
    }
    chunks.push(targetWords.slice(consumed, consumed + size).join(joiner));
    consumed += size;
  }
  return chunks;
};

// Fuzzy-match a short user translation against a longer paragraph translation.
// We slide a unit-window over the paragraph looking for the best match, because
// we don't know where within the paragraph the current line's translation sits
// (word order shifts across languages).
export const getBestWindowSimilarity = (userInput, paragraphTranslation, sourceLine) => {
  const normInput = normalizeText(userInput);
  const normParagraph = normalizeText(paragraphTranslation);
  const normSource = normalizeText(sourceLine || '');
  if (!normInput || !normParagraph) return 0;

  const inputUnits = segmentUnits(normInput);
  const paragraphUnits = segmentUnits(normParagraph);
  const sourceUnits = segmentUnits(normSource);
  if (inputUnits.length === 0 || paragraphUnits.length === 0) return 0;

  // Compare like with like: a windowed slice of the paragraph is rebuilt with
  // the same joiner its units were split on, and the input is measured the same
  // way, so a character-segmented script is never compared against a
  // space-joined string.
  const joiner = isNoSpaceScript(normParagraph) ? '' : ' ';
  const inputJoined = inputUnits.join(joiner);

  // Require the user to type enough units so matching a single word in a long
  // paragraph doesn't count. Both sides are measured with segmentUnits, so this
  // stays a like-for-like comparison even when the source and target use
  // different writing systems. Capped by the paragraph length, since a single
  // line can never need more units than the whole paragraph contains.
  const minUnitCount = Math.min(
    Math.max(1, Math.ceil(sourceUnits.length * 0.5)),
    paragraphUnits.length
  );
  if (inputUnits.length < minUnitCount) return 0;

  // If the full input is as long as the paragraph, just compare directly.
  if (inputUnits.length >= paragraphUnits.length) {
    return getSimilarity(inputJoined, paragraphUnits.join(joiner));
  }

  let best = 0;
  const sizes = new Set([inputUnits.length - 1, inputUnits.length, inputUnits.length + 1]);
  for (const size of sizes) {
    if (size <= 0 || size > paragraphUnits.length) continue;
    for (let i = 0; i + size <= paragraphUnits.length; i++) {
      const window = paragraphUnits.slice(i, i + size).join(joiner);
      const score = getSimilarity(inputJoined, window);
      if (score > best) best = score;
    }
  }
  return best;
};
