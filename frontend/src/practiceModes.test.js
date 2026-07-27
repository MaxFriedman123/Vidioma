import {
  PRACTICE_MODES,
  PRACTICE_MODE_OPTIONS,
  answersInSourceLanguage,
  gradeAnswer,
  getModeOption,
  isListeningMode,
  passThresholdFor,
  DICTATION_PASS_THRESHOLD,
  TRANSLATION_PASS_THRESHOLD,
} from './practiceModes';

describe('mode metadata', () => {
  test('every mode has an option entry', () => {
    const ids = PRACTICE_MODE_OPTIONS.map((o) => o.id);
    expect(new Set(ids)).toEqual(new Set(Object.values(PRACTICE_MODES)));
  });

  test('exactly the listening modes hide the source line', () => {
    const hiding = PRACTICE_MODE_OPTIONS.filter((o) => o.hidesSource).map((o) => o.id);
    expect(new Set(hiding)).toEqual(
      new Set([PRACTICE_MODES.LISTEN, PRACTICE_MODES.DICTATE])
    );
  });

  test('isListeningMode agrees with the option table', () => {
    PRACTICE_MODE_OPTIONS.forEach((o) => {
      expect(isListeningMode(o.id)).toBe(o.hidesSource);
    });
  });

  test('only dictation is answered in the source language', () => {
    expect(answersInSourceLanguage(PRACTICE_MODES.DICTATE)).toBe(true);
    expect(answersInSourceLanguage(PRACTICE_MODES.LISTEN)).toBe(false);
    expect(answersInSourceLanguage(PRACTICE_MODES.TRANSLATE)).toBe(false);
  });

  test('an unknown mode falls back to the first option rather than crashing', () => {
    expect(getModeOption('nonsense')).toBe(PRACTICE_MODE_OPTIONS[0]);
    expect(getModeOption(undefined)).toBe(PRACTICE_MODE_OPTIONS[0]);
  });

  test('dictation is held to a higher bar than translation', () => {
    expect(passThresholdFor(PRACTICE_MODES.DICTATE)).toBe(DICTATION_PASS_THRESHOLD);
    expect(passThresholdFor(PRACTICE_MODES.LISTEN)).toBe(TRANSLATION_PASS_THRESHOLD);
    expect(DICTATION_PASS_THRESHOLD).toBeGreaterThan(TRANSLATION_PASS_THRESHOLD);
  });
});

describe('gradeAnswer: translate and listen', () => {
  const paragraph = 'te he estado esperando todo el dia vamonos ahora';
  const source = 'I have been waiting for you all day';

  test('the exact expected translation passes', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.TRANSLATE,
      userInput: 'te he estado esperando todo el dia',
      sourceLine: source,
      paragraphTranslation: paragraph,
    });
    expect(r.passed).toBe(true);
    expect(r.score).toBeGreaterThanOrEqual(TRANSLATION_PASS_THRESHOLD);
  });

  test('listen grades identically to translate', () => {
    const args = {
      userInput: 'te he estado esperando todo el dia',
      sourceLine: source,
      paragraphTranslation: paragraph,
    };
    const t = gradeAnswer({ ...args, mode: PRACTICE_MODES.TRANSLATE });
    const l = gradeAnswer({ ...args, mode: PRACTICE_MODES.LISTEN });
    // Hiding the source changes the difficulty of the task, not the judging.
    expect(l.score).toBe(t.score);
    expect(l.passed).toBe(t.passed);
  });

  test('an unrelated answer fails', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.LISTEN,
      userInput: 'completamente diferente sin relacion',
      sourceLine: source,
      paragraphTranslation: paragraph,
    });
    expect(r.passed).toBe(false);
  });

  test('the expected text returned is the paragraph translation', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.TRANSLATE,
      userInput: 'algo',
      sourceLine: source,
      paragraphTranslation: paragraph,
    });
    // Snapshotted by the caller, because the translation cache expires and
    // machine translation is not stable across provider versions.
    expect(r.expected).toBe(paragraph);
  });
});

describe('gradeAnswer: dictation', () => {
  const source = 'we are no strangers to love';

  test('typing the caption exactly passes', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: source,
      sourceLine: source,
      paragraphTranslation: 'irrelevant for dictation',
    });
    expect(r.score).toBe(1);
    expect(r.passed).toBe(true);
  });

  test('grades against the caption, NOT the translation', () => {
    // This is the point of dictation: the machine translation is ignored, so the
    // false negatives it causes cannot happen.
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: source,
      sourceLine: source,
      paragraphTranslation: 'no somos ajenos al amor',
    });
    expect(r.passed).toBe(true);
    expect(r.expected).toBe(source);
  });

  test('punctuation and casing differences still pass', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: 'We are no strangers to love!',
      sourceLine: source,
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(true);
  });

  test('a wrong word fails, because dictation is strict', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: 'we are no strangers to war',
      sourceLine: source,
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(false);
  });

  test('a one-character typo in a long word still passes', () => {
    // Grading is unit-aware: a slip inside a word is a typo, not a wrong word.
    for (const typo of ['we are no strangers to lovee', 'we are no strangers to lov']) {
      const r = gradeAnswer({
        mode: PRACTICE_MODES.DICTATE,
        userInput: typo,
        sourceLine: source,
        paragraphTranslation: '',
      });
      expect(r.passed).toBe(true);
    }
  });

  test('a wrong SHORT word fails, even though it is one edit away', () => {
    // The trap character-level scoring falls into: "we" for "i" is one edit but a
    // completely different word, and on a 7-word line it scores 0.857, above any
    // threshold loose enough to forgive a real typo.
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: 'you know the rules and so do we',
      sourceLine: 'you know the rules and so do i',
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(false);
  });

  test('a wrong word that shares most of its letters fails', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: 'it war a good day',
      sourceLine: 'it was a good day',
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(false);
  });

  test('padding an answer with extra words cannot raise the score', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: `${source} and some extra words`,
      sourceLine: source,
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(false);
  });

  test('a dropped word fails', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: 'we are no strangers',
      sourceLine: source,
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(false);
  });

  test('empty input fails rather than dividing by zero', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: '',
      sourceLine: source,
      paragraphTranslation: '',
    });
    expect(r.score).toBe(0);
    expect(r.passed).toBe(false);
  });

  test('a missing caption cannot accidentally pass', () => {
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: 'anything',
      sourceLine: '',
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(false);
  });

  test('works in a non-Latin script', () => {
    const zh = '我等了你一整天';
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: zh,
      sourceLine: zh,
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(true);
  });

  test('one wrong character fails in a no-space script', () => {
    // Chinese segments per character, so every character is a unit and a single
    // substitution is a genuine error rather than a typo.
    const r = gradeAnswer({
      mode: PRACTICE_MODES.DICTATE,
      userInput: '我等了你一整年',
      sourceLine: '我等了你一整天',
      paragraphTranslation: '',
    });
    expect(r.passed).toBe(false);
  });
});
