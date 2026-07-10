import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useAuth } from '../AuthContext';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

function formatDue(dueDate) {
  if (!dueDate) return null;
  const d = new Date(dueDate);
  const now = new Date();
  const overdue = d < now;
  return { label: d.toLocaleString(), overdue };
}

// Human-readable active practice time (e.g. "12m 30s", "1h 05m").
function formatActiveTime(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  if (total === 0) return null;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

// Assignment detail: teachers see per-student completion; students see the
// assignment plus a Start/Continue button that launches the no-skip player.
export default function AssignmentDetail({ assignmentId, onBack, onStartAssignment }) {
  const { accessToken } = useAuth();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!accessToken || !assignmentId) return;
    setLoading(true);
    axios
      .get(`${API_BASE_URL}/api/assignments/${assignmentId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      .then((resp) => setData(resp.data))
      .catch((err) => setError(err.response?.data?.error || 'Failed to load assignment.'))
      .finally(() => setLoading(false));
  }, [accessToken, assignmentId]);

  if (loading) {
    return (
      <div className="class-view">
        <p className="dashboard-loading">Loading assignment...</p>
      </div>
    );
  }
  if (error) {
    return (
      <div className="class-view">
        <button className="back-button class-back-btn" onClick={onBack}>Back</button>
        <p className="dashboard-error">{error}</p>
      </div>
    );
  }
  if (!data) return null;

  const a = data.assignment;
  const video = a.videos || {};
  const title = a.title || video.title || 'Assignment';
  const due = formatDue(a.due_date);
  const isTeacher = data.is_teacher;

  return (
    <div className="class-view">
      <button className="back-button class-back-btn" onClick={onBack}>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
          <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z" />
        </svg>
        Back
      </button>

      <div className="class-header">
        <div className="class-header-info">
          <h2 className="class-header-name">{title}</h2>
          {a.instructions && <p className="class-header-desc">{a.instructions}</p>}
          <div className="class-header-tags">
            <span className="class-card-tag">
              {(a.transcript_language || 'en').toUpperCase()} → {(a.translation_language || 'es').toUpperCase()}
            </span>
            {due && (
              <span className={`class-card-tag ${due.overdue ? 'assignment-due-overdue' : ''}`}>
                Due {due.label}
              </span>
            )}
          </div>
        </div>
        {video.thumbnail_url && (
          <img src={video.thumbnail_url} alt={title} className="assignment-detail-thumb" />
        )}
      </div>

      {isTeacher ? (
        <div className="class-section">
          <h3 className="class-section-title">Student Progress ({data.students.length})</h3>
          {data.students.length === 0 ? (
            <p className="class-empty-students">No students are assigned yet.</p>
          ) : (
            <div className="class-members-list">
              {data.students.map((s) => {
                const pct = s.total_lines > 0 ? Math.round((s.current_line_index / s.total_lines) * 100) : 0;
                const activeTime = formatActiveTime(s.active_seconds);
                return (
                  <div key={s.student_id} className="class-member-card">
                    <div className="class-member-avatar">
                      {(s.user_name || 'S').charAt(0).toUpperCase()}
                    </div>
                    <div className="class-member-info assignment-student-progress">
                      <span className="class-member-name">{s.user_name}</span>
                      {s.completed ? (
                        <span className="assignment-status-complete">
                          ✓ Completed{activeTime ? ` — ${activeTime}` : ''}
                        </span>
                      ) : s.started ? (
                        <span className="assignment-status-progress">
                          In progress — {pct}%{activeTime ? ` · ${activeTime}` : ''}
                        </span>
                      ) : (
                        <span className="assignment-status-notstarted">Not started</span>
                      )}
                      <div className="dashboard-progress-bar">
                        <div className="dashboard-progress-fill" style={{ width: `${s.completed ? 100 : pct}%` }} />
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ) : (
        <div className="class-section">
          <StudentAssignmentActions assignment={a} onStartAssignment={onStartAssignment} />
        </div>
      )}
    </div>
  );
}

function StudentAssignmentActions({ assignment, onStartAssignment }) {
  const progress = assignment.progress;
  const completed = progress?.completed;
  const started = progress && progress.current_line_index > 0;
  const pct = progress && progress.total_lines > 0
    ? Math.round((progress.current_line_index / progress.total_lines) * 100)
    : 0;
  const activeTime = formatActiveTime(progress?.active_seconds);

  return (
    <div className="assignment-student-actions">
      {completed ? (
        <p className="assignment-status-complete">
          ✓ You've completed this assignment.{activeTime ? ` Practice time: ${activeTime}.` : ''}
        </p>
      ) : started ? (
        <>
          <p className="assignment-status-progress">
            In progress — {pct}%{activeTime ? ` · ${activeTime} practiced` : ''}
          </p>
          <div className="dashboard-progress-bar">
            <div className="dashboard-progress-fill" style={{ width: `${pct}%` }} />
          </div>
        </>
      ) : (
        <p className="assignment-status-notstarted">Not started yet.</p>
      )}
      <button className="auth-submit" onClick={() => onStartAssignment(assignment)}>
        {completed ? 'Review Again' : started ? 'Continue' : 'Start Assignment'}
      </button>
    </div>
  );
}
