import {
  normalizeText,
  segmentUnits,
  isNoSpaceScript,
  splitParagraphToLines,
  getBestWindowSimilarity,
} from './answerMatching';

// The acceptance threshold used by App.js when the user submits a line.
const PASS = 0.6;

// The reported bug: in non-Roman scripts, typing (or pasting) the exact expected
// translation was still marked wrong. Each language below is a real correct
// answer for its source line, so every one of these must be accepted.
//
// [label, sourceLines, translatedLines, joinsWithSpaces]
const LANGUAGES = [
  ['Chinese', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['你好', '我等了你一整天', '我们现在走吧'], false],
  ['Japanese', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['こんにちは', '一日中あなたを待っていました', '今すぐ行きましょう'], false],
  ['Thai', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['สวัสดี', 'ฉันรอคุณมาทั้งวัน', 'ไปกันเลย'], false],
  ['Lao', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['ສະບາຍດີ', 'ຂ້ອຍລໍຖ້າເຈົ້າຕະຫຼອດວັນ', 'ໄປກັນເລີຍ'], false],
  ['Khmer', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['សូស្តី', 'ខ្ញុំរង់ចាំអ្នកពេញមួយថ្ងៃ', 'តោះទៅឥឡូវនេះ'], false],
  ['Burmese', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['မင်္ဂလာပါ', 'ကျွန်ုပ်တစ်ရက်လုံးသင်ကိုစောင့်နေခဲ့သည်', 'အခုသွားရအောင်'], false],
  ['Tibetan', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['བཀྲ་ཤིས་བདེ་ལེགས', 'ངས་ཉིན་གང་བོར་ཁྱོད་ལ་བསྒུགས་ཡོད', 'ད་ལྟ་འགྲོ'], false],
  ['Korean', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['안녕하세요', '나는 하루 종일 너를 기다렸어', '지금 가자'], true],
  ['Arabic', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['مرحبا', 'كنت أنتظرك طوال اليوم', 'لنذهب الآن'], true],
  ['Hebrew', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['שלום', 'חיכיתי לך כל היום', 'בוא נלך עכשיו'], true],
  ['Hindi', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['नमस्ते', 'मैं दिन भर तुम्हारा इंतजार कर रहा था', 'अब चलें'], true],
  ['Russian', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['Привет', 'Я ждал тебя весь день', 'Пойдем сейчас'], true],
  ['Greek', ['Hello there', 'I have been waiting for you all day', 'Lets go now'],
    ['Γεια σου', 'Σε περίμενα όλη μέρα', 'Πάμε τώρα'], true],
];

describe('exact expected translation is accepted in every script', () => {
  LANGUAGES.forEach(([label, sources, targets, spaced]) => {
    // The user practices line by line, but matching happens against the whole
    // paragraph translation, so the paragraph is what the line sits inside.
    const paragraph = targets.join(spaced ? ' ' : '');

    targets.forEach((expected, i) => {
      test(`${label}: line ${i + 1} of the paragraph`, () => {
        expect(
          getBestWindowSimilarity(expected, paragraph, sources[i])
        ).toBeGreaterThanOrEqual(PASS);
      });
    });

    test(`${label}: a single-line paragraph`, () => {
      expect(
        getBestWindowSimilarity(targets[1], targets[1], sources[1])
      ).toBeGreaterThanOrEqual(PASS);
    });
  });
});

describe('script-specific punctuation is ignored, as Latin punctuation already was', () => {
  // Each pair is the same sentence with and without punctuation the learner
  // would not necessarily type. Previously only Latin marks were stripped, so
  // these counted as differences.
  const cases = [
    ['CJK ideographic period', '我等了你一整天。', '我等了你一整天', 'I waited all day'],
    // No space in the plain form: a CJK comma separates clauses without one, so
    // removing it must not leave a word gap behind.
    ['CJK fullwidth comma', '你好，我等了你', '你好我等了你', 'Hello I waited'],
    ['Japanese 、and 。', 'こんにちは、元気ですか。', 'こんにちは元気ですか', 'Hello how are you'],
    ['Arabic question mark and comma', 'كيف حالك؟ بخير، شكرا', 'كيف حالك بخير شكرا', 'How are you fine thanks'],
    ['Arabic thousands separator', 'مرحبا٬ كيف حالك', 'مرحبا كيف حالك', 'Hello how are you'],
    ['Devanagari danda', 'मैं ठीक हूँ। धन्यवाद', 'मैं ठीक हूँ धन्यवाद', 'I am fine thank you'],
    ['Greek question mark', 'Τι κάνεις; Καλά', 'Τι κάνεις Καλά', 'How are you good'],
    ['Latin (unchanged behavior)', 'Hola, mundo!', 'Hola mundo', 'Hello world'],
  ];

  cases.forEach(([label, withPunct, withoutPunct, source]) => {
    test(label, () => {
      expect(normalizeText(withPunct)).toBe(normalizeText(withoutPunct));
      expect(
        getBestWindowSimilarity(withoutPunct, withPunct, source)
      ).toBeGreaterThanOrEqual(PASS);
    });
  });
});

describe('invisible characters from copy-paste do not break a match', () => {
  // The user said they copied and pasted the translation. Text copied from a
  // rendered page carries these along, so the pasted answer looks identical but
  // did not compare equal.
  const cases = [
    ['non-breaking space', 'Hola mundo bonito', 'Hola mundo bonito', 'Hello beautiful world'],
    ['ideographic space', '你好 世界 朋友', '你好　世界　朋友', 'Hello world friend'],
    ['zero-width space', 'שלום לך חבר', 'שלום​ לך​ חבר', 'Hello to you friend'],
    ['right-to-left mark', 'مرحبا بك يا صديقي', '‏مرحبا بك يا صديقي', 'Hello my friend'],
    ['zero-width non-joiner', 'مرحبا بك', 'مرحبا‌ بك', 'Hello to you'],
    ['byte order mark', '我等了你一整天', '﻿我等了你一整天', 'I waited all day'],
    ['soft hyphen', 'Hola mundo', 'Hola­mundo', 'Hello world'],
    ['fullwidth Latin', 'Hello world', 'Ｈｅｌｌｏ　ｗｏｒｌｄ', 'Hello world'],
  ];

  cases.forEach(([label, clean, pasted, source]) => {
    test(label, () => {
      expect(
        getBestWindowSimilarity(pasted, clean, source)
      ).toBeGreaterThanOrEqual(PASS);
    });
  });
});

describe('optional vowel marks the learner would omit', () => {
  test('Hebrew niqqud in the expected text', () => {
    expect(
      getBestWindowSimilarity('שלום עולם', 'שָׁלוֹם עוֹלָם', 'Hello world')
    ).toBeGreaterThanOrEqual(PASS);
  });

  test('Arabic harakat in the expected text', () => {
    expect(
      getBestWindowSimilarity('مرحبا بالعالم', 'مَرْحَبًا بِالْعَالَم', 'Hello world')
    ).toBeGreaterThanOrEqual(PASS);
  });

  test('Devanagari matras are NOT stripped (they change the word)', () => {
    // मैं ("I") vs मे is a different word, so these must not normalize alike.
    expect(normalizeText('मैं')).not.toBe(normalizeText('मे'));
  });

  test('Thai vowel and tone marks are NOT stripped (they change the word)', () => {
    expect(normalizeText('ทั้ง')).not.toBe(normalizeText('ทัง'));
  });
});

describe('wrong answers are still rejected', () => {
  const paragraph = '你好我等了你一整天我们现在走吧';

  test('unrelated text in a no-space script', () => {
    expect(
      getBestWindowSimilarity('完全不同的句子内容', paragraph, 'I have been waiting for you all day')
    ).toBeLessThan(PASS);
  });

  test('a single character out of a long paragraph', () => {
    // The minimum-length gate must still stop someone typing one character to
    // brute-force a match against a long paragraph.
    expect(
      getBestWindowSimilarity('我', paragraph, 'I have been waiting for you all day')
    ).toBeLessThan(PASS);
  });

  test('unrelated text in a space-delimited script', () => {
    expect(
      getBestWindowSimilarity(
        'totally different words here',
        'Hello there I waited all day lets go',
        'I have been waiting for you all day'
      )
    ).toBeLessThan(PASS);
  });

  test('empty input', () => {
    expect(getBestWindowSimilarity('', paragraph, 'Hello')).toBe(0);
  });

  test('empty paragraph translation', () => {
    expect(getBestWindowSimilarity('我等了你', '', 'Hello')).toBe(0);
  });
});

describe('segmentUnits and isNoSpaceScript', () => {
  test('space-delimited scripts segment into words', () => {
    expect(segmentUnits('hola mundo bonito')).toEqual(['hola', 'mundo', 'bonito']);
    expect(segmentUnits('Я ждал тебя')).toEqual(['Я', 'ждал', 'тебя']);
  });

  test('no-space scripts segment into characters', () => {
    expect(segmentUnits('我等了你')).toEqual(['我', '等', '了', '你']);
    expect(segmentUnits('ฉันรอ')).toEqual(['ฉ', 'ั', 'น', 'ร', 'อ']);
  });

  test('detects the scripts written without word spaces', () => {
    expect(isNoSpaceScript('我等了你一整天')).toBe(true);
    expect(isNoSpaceScript('こんにちは')).toBe(true);
    expect(isNoSpaceScript('ฉันรอคุณ')).toBe(true);
    expect(isNoSpaceScript('ຂ້ອຍລໍຖ້າ')).toBe(true);
    expect(isNoSpaceScript('ខ្ញុំរង់ចាំ')).toBe(true);
    expect(isNoSpaceScript('ကျွန်ုပ်')).toBe(true);
  });

  test('Korean is space-delimited, so it is not treated as a no-space script', () => {
    expect(isNoSpaceScript('나는 하루 종일 너를 기다렸어')).toBe(false);
  });

  test('Latin, Cyrillic, Arabic, Hebrew and Devanagari are space-delimited', () => {
    expect(isNoSpaceScript('hola mundo')).toBe(false);
    expect(isNoSpaceScript('Я ждал тебя')).toBe(false);
    expect(isNoSpaceScript('كنت أنتظرك')).toBe(false);
    expect(isNoSpaceScript('חיכיתי לך')).toBe(false);
    expect(isNoSpaceScript('मैं दिन भर')).toBe(false);
  });

  test('empty input', () => {
    expect(segmentUnits('')).toEqual([]);
    expect(isNoSpaceScript('')).toBe(false);
  });
});

describe('splitParagraphToLines still distributes across every line', () => {
  test('no-space script fills all lines rather than dumping into one', () => {
    const chunks = splitParagraphToLines('你好我等了你一整天我们现在走吧', [
      'Hello there',
      'I have been waiting for you all day',
      'Lets go now',
    ]);
    expect(chunks).toHaveLength(3);
    chunks.forEach((c) => expect(c.length).toBeGreaterThan(0));
    expect(chunks.join('')).toBe('你好我等了你一整天我们现在走吧');
  });

  test('space-delimited script keeps words joined by spaces', () => {
    const chunks = splitParagraphToLines('hola mundo bonito y grande', ['one two', 'three four']);
    expect(chunks).toHaveLength(2);
    chunks.forEach((c) => expect(c).not.toMatch(/^\s|\s$/));
    expect(chunks.join(' ')).toBe('hola mundo bonito y grande');
  });

  test('a newly added no-space script also distributes (Lao)', () => {
    const chunks = splitParagraphToLines('ສະບາຍດີຂ້ອຍລໍຖ້າເຈົ້າ', ['Hello', 'I waited for you']);
    expect(chunks).toHaveLength(2);
    chunks.forEach((c) => expect(c.length).toBeGreaterThan(0));
  });
});
