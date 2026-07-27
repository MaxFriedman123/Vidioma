import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { useAuth } from '../AuthContext';

// Same one-line env reads App.js uses (kept local, as with LANGUAGES below).
const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');
// Optional Cloudflare Worker caption-list relay, same contract as the backend's
// /api/caption-tracks. See docs/caption-egress.md.
const CAPTION_RELAY_URL = (process.env.REACT_APP_CAPTION_RELAY_URL || '').replace(/\/$/, '');

// Same language list the main app uses (kept local to avoid a circular import).
const LANGUAGES = [
  { code: 'en', name: 'English' },
  { code: 'es', name: 'Spanish' },
  { code: 'fr', name: 'French' },
  { code: 'de', name: 'German' },
  { code: 'iw', name: 'Hebrew' },
  { code: 'it', name: 'Italian' },
  { code: 'pt', name: 'Portuguese' },
  { code: 'ja', name: 'Japanese' },
  { code: 'ko', name: 'Korean' },
  { code: 'zh-CN', name: 'Chinese' },
  { code: 'ru', name: 'Russian' },
];

const langName = (code) => (LANGUAGES.find((l) => l.code === code) || {}).name || code;

// The title lookup is a convenience, never a blocker, so keep it short.
const TITLE_LOOKUP_TIMEOUT_MS = 6000;

// A bare 11-char video id is a valid thing to paste (the backend parses it), but
// YouTube's oEmbed endpoint needs a real watch URL, so build one.
const toWatchUrl = (raw) => (/^[\w-]{11}$/.test(raw) ? `https://www.youtube.com/watch?v=${raw}` : raw);

// Ask a caption-list endpoint whether this video has captions in `fromLang`.
// Returns 'ok' | 'translated' | 'missing' | 'unknown'; 'unknown' means the
// listing itself failed, which says nothing about the video.
async function checkCaptions(endpoint, target, fromLang) {
  try {
    const resp = await axios.post(endpoint, { url: target, from_lang: fromLang });
    return resp.data?.is_correct_lang ? 'ok' : 'translated';
  } catch (err) {
    // 404 is the real "no captions here" answer. Anything else (a 503 from a
    // YouTube blip, a network drop) must not cry wolf.
    return err.response?.status === 404 ? 'missing' : 'unknown';
  }
}

// Text + status class per pre-flight outcome. Reuses the existing assignment
// status colours (green / amber / grey) plus .assignment-due-overdue for red, so
// this needs no new CSS.
const CAPTION_NOTES = {
  checking: { cls: 'assignment-status-notstarted', text: () => 'Checking captions...' },
  ok: { cls: 'assignment-status-complete', text: (lang) => `Captions available in ${lang}` },
  translated: { cls: 'assignment-status-progress', text: () => 'Only auto-translated captions available' },
  missing: {
    cls: 'assignment-status-notstarted assignment-due-overdue',
    text: (lang) => `No captions found in ${lang}. You can still assign it.`,
  },
  // The check itself failed, which says nothing about the video.
  unknown: { cls: 'assignment-status-notstarted', text: () => 'Could not check captions right now.' },
};

// Inline caption pre-flight indicator. A WARNING, never a gate: YouTube listing
// blips are common, and a teacher preparing a lesson late at night must still be
// able to save.
function CaptionNote({ check }) {
  const note = check && CAPTION_NOTES[check.state];
  if (!note) return null;
  return (
    <p className={note.cls} style={{ margin: '8px 0 0', overflowWrap: 'anywhere' }}>
      {note.text(check.lang)}
    </p>
  );
}

// Teacher page: create a video assignment for whole classes and/or individual
// students, with an optional due date and practice language pair.
//
// `prefill` (optional) seeds the video/language/instruction fields, used by the
// Duplicate action in ClassView. Targets are never prefilled: the teacher picks
// which class gets the copy.
export default function CreateAssignment({ onBack, onCreated, prefill }) {
  const { accessToken } = useAuth();

  const [url, setUrl] = useState(prefill?.url || '');
  const [title, setTitle] = useState(prefill?.title || '');
  const [fromLang, setFromLang] = useState(prefill?.transcript_language || 'en');
  const [toLang, setToLang] = useState(prefill?.translation_language || 'es');
  const [dueDate, setDueDate] = useState('');
  const [instructions, setInstructions] = useState(prefill?.instructions || '');

  // Caption pre-flight result: { state, lang }. state is one of 'checking',
  // 'ok', 'translated', 'missing', 'unknown'. Advisory only.
  const [captionCheck, setCaptionCheck] = useState(null);
  const captionReqRef = useRef(0);   // ignores out-of-order responses
  const lastCheckedRef = useRef(''); // "url|lang" already checked

  const [classes, setClasses] = useState([]);
  const [loadingClasses, setLoadingClasses] = useState(true);
  // Per class: 'none' | 'all' | 'some'. When 'some', selectedStudents holds ids.
  const [classMode, setClassMode] = useState({}); // classId -> mode
  const [rosters, setRosters] = useState({});       // classId -> [{student_id, user_name}]
  const [selectedStudents, setSelectedStudents] = useState({}); // classId -> Set(studentId)

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  // The URL the pre-flight check runs against: set when the URL field blurs (so
  // we don't fire a request per keystroke), and seeded from a prefill so a
  // duplicated assignment is checked without the teacher touching the field.
  // `nonce` bumps on every blur so re-blurring an unchanged URL can retry an
  // inconclusive check (setting identical state alone would not re-run).
  const [preflight, setPreflight] = useState({ url: prefill?.url || '', nonce: 0 });

  // Load the teacher's classes.
  useEffect(() => {
    if (!accessToken) return;
    axios
      .get(`${API_BASE_URL}/api/classes`, { headers: { Authorization: `Bearer ${accessToken}` } })
      .then((resp) => setClasses(resp.data.classes || []))
      .catch(() => setError('Failed to load your classes.'))
      .finally(() => setLoadingClasses(false));
  }, [accessToken]);

  // Caption pre-flight: ask the backend whether this video actually has
  // captions in the chosen transcript language, so a teacher finds out here
  // instead of from 25 students hitting a dead assignment. Purely advisory: any
  // failure shows an "unknown" note and submission is never blocked.
  useEffect(() => {
    const target = preflight.url.trim();
    if (!target || !fromLang) {
      setCaptionCheck(null);
      return;
    }
    // Skip a repeat of a check that already gave a real answer; an inconclusive
    // one (lastCheckedRef cleared below) is worth another try.
    const key = `${target}|${fromLang}`;
    if (lastCheckedRef.current === key) return;
    lastCheckedRef.current = key;

    const reqId = captionReqRef.current + 1;
    captionReqRef.current = reqId;
    setCaptionCheck({ state: 'checking', lang: langName(fromLang) });

    // Relay first, then our backend, mirroring the layering the player uses:
    // YouTube IP-blocks the backend host's listing calls, so asking only the
    // backend would report "could not check" for nearly every prod teacher.
    (async () => {
      let state = 'unknown';
      if (CAPTION_RELAY_URL) {
        state = await checkCaptions(CAPTION_RELAY_URL, target, fromLang);
      }
      // Only a failed listing is worth a second opinion; a real answer stands.
      if (state === 'unknown') {
        state = await checkCaptions(`${API_BASE_URL}/api/caption-tracks`, target, fromLang);
      }
      // A failed listing is not an answer about the video, so let a re-blur ask
      // again rather than leaving the teacher stuck with "could not check".
      if (state === 'unknown') lastCheckedRef.current = '';
      if (captionReqRef.current !== reqId) return;
      setCaptionCheck({ state, lang: langName(fromLang) });
    })();
  }, [preflight, fromLang]);

  // Fill the title from YouTube when the teacher left it blank, so a class list
  // isn't a wall of rows all reading "Assignment". oEmbed needs no API key and
  // is CORS-open. Silent on failure: the teacher can always type their own.
  const fetchTitleIfEmpty = async (rawUrl) => {
    const target = toWatchUrl(rawUrl.trim());
    if (!target) return;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TITLE_LOOKUP_TIMEOUT_MS);
    try {
      const endpoint = `https://www.youtube.com/oembed?url=${encodeURIComponent(target)}&format=json`;
      const resp = await fetch(endpoint, { signal: controller.signal });
      if (!resp.ok) return;
      const data = await resp.json();
      const fetched = (data && data.title ? String(data.title) : '').trim();
      // Re-check emptiness: the teacher may have typed a title while we waited,
      // and their words win over YouTube's.
      if (fetched) setTitle((prev) => (prev.trim() ? prev : fetched));
    } catch (_) {
      // No title is fine; the field just stays empty.
    } finally {
      clearTimeout(timer);
    }
  };

  const handleUrlBlur = () => {
    setPreflight((prev) => ({ url, nonce: prev.nonce + 1 }));
    if (!title.trim()) fetchTitleIfEmpty(url);
  };

  // Duplicating an assignment that predates auto-filled titles would otherwise
  // produce another untitled row, so look its title up once on mount.
  useEffect(() => {
    if (prefill?.url && !prefill.title) fetchTitleIfEmpty(prefill.url);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Lazily fetch a class roster the first time it's expanded.
  const loadRoster = (classId) => {
    if (rosters[classId]) return;
    axios
      .get(`${API_BASE_URL}/api/classes/${classId}`, { headers: { Authorization: `Bearer ${accessToken}` } })
      .then((resp) => {
        const students = (resp.data.students || []).map((s) => ({
          student_id: s.student_id,
          user_name: s.user_profiles?.user_name || 'Student',
        }));
        setRosters((prev) => ({ ...prev, [classId]: students }));
      })
      .catch(() => setRosters((prev) => ({ ...prev, [classId]: [] })));
  };

  const setMode = (classId, mode) => {
    setClassMode((prev) => ({ ...prev, [classId]: mode }));
    if (mode === 'some') {
      loadRoster(classId);
    }
  };

  const toggleStudent = (classId, studentId) => {
    setSelectedStudents((prev) => {
      const next = new Set(prev[classId] || []);
      if (next.has(studentId)) next.delete(studentId);
      else next.add(studentId);
      return { ...prev, [classId]: next };
    });
  };

  const buildTargets = () => {
    const class_ids = [];
    const student_targets = [];
    for (const cls of classes) {
      const mode = classMode[cls.class_id] || 'none';
      if (mode === 'all') {
        class_ids.push(cls.class_id);
      } else if (mode === 'some') {
        const chosen = selectedStudents[cls.class_id] || new Set();
        chosen.forEach((sid) => student_targets.push({ class_id: cls.class_id, student_id: sid }));
      }
    }
    return { class_ids, student_targets };
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    if (!url.trim()) { setError('Enter a YouTube URL.'); return; }
    if (fromLang === toLang) {
      setError('Choose two different languages — the video language and the translation must differ.');
      return;
    }
    // Flag classes set to "Pick students" but with nothing selected, so the
    // teacher isn't silently left with an empty assignment.
    const emptyPick = classes.find(
      (c) => (classMode[c.class_id] || 'none') === 'some' && (selectedStudents[c.class_id]?.size || 0) === 0
    );
    if (emptyPick) {
      setError(`Select at least one student in "${emptyPick.class_name}", or change it to Whole class / None.`);
      return;
    }
    const { class_ids, student_targets } = buildTargets();
    if (class_ids.length === 0 && student_targets.length === 0) {
      setError('Select at least one class or student to assign to.');
      return;
    }
    setSubmitting(true);
    try {
      const resp = await axios.post(`${API_BASE_URL}/api/assignments`, {
        url: url.trim(),
        title: title.trim() || undefined,
        transcript_language: fromLang,
        translation_language: toLang,
        instructions: instructions.trim() || undefined,
        due_date: dueDate ? new Date(dueDate).toISOString() : undefined,
        class_ids,
        student_targets,
      }, { headers: { Authorization: `Bearer ${accessToken}` } });
      if (onCreated) onCreated(resp.data.assignment);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create assignment.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="class-view assignment-create">
      <button className="back-button class-back-btn" onClick={onBack}>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
          <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z" />
        </svg>
        Back to Classes
      </button>

      <h2 className="dashboard-title">{prefill ? 'Duplicate Assignment' : 'New Assignment'}</h2>
      {prefill && (
        <p className="assignment-player-note" style={{ margin: '0 0 14px', overflowWrap: 'anywhere' }}>
          Copied from "{prefill.sourceTitle || 'the original'}". Pick who gets this copy.
        </p>
      )}

      <form onSubmit={handleSubmit} className="assignment-form">
        {/* Section 1: the video to practice */}
        <div className="assignment-section">
          <h3 className="assignment-section-title">Video</h3>

          <label className="assignment-label">YouTube URL</label>
          <input
            type="text"
            className="auth-input"
            placeholder="Paste a YouTube URL..."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onBlur={handleUrlBlur}
            required
          />

          <CaptionNote check={captionCheck} />

          <label className="assignment-label">Title (optional)</label>
          <input
            type="text"
            className="auth-input"
            placeholder="e.g. Chapter 3 listening practice"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />

          <div className="assignment-lang-row">
            <div>
              <label className="assignment-label">Video language</label>
              <select className="auth-input" value={fromLang} onChange={(e) => setFromLang(e.target.value)}>
                {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
              </select>
            </div>
            <div>
              <label className="assignment-label">Translate into</label>
              <select className="auth-input" value={toLang} onChange={(e) => setToLang(e.target.value)}>
                {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
              </select>
            </div>
          </div>
        </div>

        {/* Section 2: optional details */}
        <div className="assignment-section">
          <h3 className="assignment-section-title">Details <span className="assignment-section-optional">optional</span></h3>

          <label className="assignment-label">Due date</label>
          <input
            type="datetime-local"
            className="auth-input"
            value={dueDate}
            onChange={(e) => setDueDate(e.target.value)}
          />

          <label className="assignment-label">Instructions</label>
          <input
            type="text"
            className="auth-input"
            placeholder="A note for your students"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </div>

        {/* Section 3: who gets it */}
        <div className="assignment-section">
          <h3 className="assignment-section-title">Assign to</h3>

          {loadingClasses ? (
            <p className="dashboard-loading">Loading classes...</p>
          ) : classes.length === 0 ? (
            <p className="dashboard-empty">You have no classes yet. Create a class first.</p>
          ) : (
            <div className="assignment-targets">
              {classes.map((cls) => {
                const mode = classMode[cls.class_id] || 'none';
                return (
                  <div key={cls.class_id} className="assignment-target-class">
                    <div className="assignment-target-header">
                      <span className="assignment-target-name">{cls.class_name}</span>
                      <div className="assignment-target-modes">
                        <button type="button" className={`assignment-mode-btn ${mode === 'none' ? 'active' : ''}`} onClick={() => setMode(cls.class_id, 'none')}>None</button>
                        <button type="button" className={`assignment-mode-btn ${mode === 'all' ? 'active' : ''}`} onClick={() => setMode(cls.class_id, 'all')}>Whole class</button>
                        <button type="button" className={`assignment-mode-btn ${mode === 'some' ? 'active' : ''}`} onClick={() => setMode(cls.class_id, 'some')}>Pick students</button>
                      </div>
                    </div>
                    {mode === 'some' && (
                      <div className="assignment-roster">
                        {!rosters[cls.class_id] ? (
                          <p className="dashboard-loading">Loading students...</p>
                        ) : rosters[cls.class_id].length === 0 ? (
                          <p className="class-empty-students">No students in this class yet.</p>
                        ) : (
                          rosters[cls.class_id].map((s) => {
                            const checked = (selectedStudents[cls.class_id] || new Set()).has(s.student_id);
                            return (
                              <label key={s.student_id} className="assignment-student-option">
                                <input type="checkbox" checked={checked} onChange={() => toggleStudent(cls.class_id, s.student_id)} />
                                <span>{s.user_name}</span>
                              </label>
                            );
                          })
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {error && <p className="auth-error">{error}</p>}

        <button type="submit" className="auth-submit" disabled={submitting}>
          {submitting ? 'Creating...' : 'Create Assignment'}
        </button>
      </form>
    </div>
  );
}
