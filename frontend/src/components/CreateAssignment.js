import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useAuth } from '../AuthContext';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

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

// Teacher page: create a video assignment for whole classes and/or individual
// students, with an optional due date and practice language pair.
export default function CreateAssignment({ onBack, onCreated }) {
  const { accessToken } = useAuth();

  const [url, setUrl] = useState('');
  const [title, setTitle] = useState('');
  const [fromLang, setFromLang] = useState('en');
  const [toLang, setToLang] = useState('es');
  const [dueDate, setDueDate] = useState('');
  const [instructions, setInstructions] = useState('');

  const [classes, setClasses] = useState([]);
  const [loadingClasses, setLoadingClasses] = useState(true);
  // Per class: 'none' | 'all' | 'some'. When 'some', selectedStudents holds ids.
  const [classMode, setClassMode] = useState({}); // classId -> mode
  const [rosters, setRosters] = useState({});       // classId -> [{student_id, user_name}]
  const [selectedStudents, setSelectedStudents] = useState({}); // classId -> Set(studentId)

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  // Load the teacher's classes.
  useEffect(() => {
    if (!accessToken) return;
    axios
      .get(`${API_BASE_URL}/api/classes`, { headers: { Authorization: `Bearer ${accessToken}` } })
      .then((resp) => setClasses(resp.data.classes || []))
      .catch(() => setError('Failed to load your classes.'))
      .finally(() => setLoadingClasses(false));
  }, [accessToken]);

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
    <div className="class-dashboard">
      <button className="back-button class-back-btn" onClick={onBack}>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
          <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z" />
        </svg>
        Back to Classes
      </button>

      <h2 className="dashboard-title">New Assignment</h2>

      <form onSubmit={handleSubmit} className="assignment-form">
        <label className="assignment-label">YouTube URL</label>
        <input
          type="text"
          className="auth-input"
          placeholder="Paste a YouTube URL..."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
        />

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

        <label className="assignment-label">Due date (optional)</label>
        <input
          type="datetime-local"
          className="auth-input"
          value={dueDate}
          onChange={(e) => setDueDate(e.target.value)}
        />

        <label className="assignment-label">Instructions (optional)</label>
        <input
          type="text"
          className="auth-input"
          placeholder="A note for your students"
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
        />

        <label className="assignment-label">Assign to</label>
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

        {error && <p className="auth-error">{error}</p>}

        <button type="submit" className="auth-submit" disabled={submitting}>
          {submitting ? 'Creating...' : 'Create Assignment'}
        </button>
      </form>
    </div>
  );
}
