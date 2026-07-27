import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useAuth } from '../AuthContext';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

// Label for an assignment row. Assignments created before titles were
// auto-filled have no title at all, so fall back to the video id rather than
// giving every such row the same word.
function assignmentLabel(a) {
  const explicit = a.title || a.videos?.title;
  if (explicit) return explicit;
  const youtubeId = a.youtube_id || a.videos?.youtube_id;
  return youtubeId ? `Video ${youtubeId}` : 'Assignment';
}

// Seed values for a duplicate: the same video, language pair and instructions,
// with no targets and no due date so the teacher chooses both. This is a plain
// prefilled create, so it needs no backend support.
function buildDuplicatePrefill(a, label) {
  const youtubeId = a.youtube_id || a.videos?.youtube_id || '';
  return {
    url: youtubeId ? `https://www.youtube.com/watch?v=${youtubeId}` : '',
    title: a.title || a.videos?.title || '',
    transcript_language: a.transcript_language || 'en',
    translation_language: a.translation_language || 'es',
    instructions: a.instructions || '',
    sourceTitle: label,
  };
}

export default function ClassView({ classId, onBack, onStartAssignment, onOpenAssignment, onDuplicateAssignment }) {
  const { accessToken, user } = useAuth();
  const [classData, setClassData] = useState(null);
  const [students, setStudents] = useState([]);
  const [isTeacher, setIsTeacher] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [codeCopied, setCodeCopied] = useState(false);

  // Assignments scoped to this class.
  const [assignments, setAssignments] = useState([]);

  // Confirm dialog state
  const [confirmAction, setConfirmAction] = useState(null); // { type, studentId, studentName }
  const [confirmSubmitting, setConfirmSubmitting] = useState(false);

  const fetchClassDetail = () => {
    if (!accessToken || !classId) return;
    setLoading(true);
    axios
      .get(`${API_BASE_URL}/api/classes/${classId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      .then((resp) => {
        setClassData(resp.data.class);
        setStudents(resp.data.students || []);
        setIsTeacher(resp.data.is_teacher);
      })
      .catch((err) => {
        setError(err.response?.data?.error || 'Failed to load class details.');
      })
      .finally(() => setLoading(false));
  };

  const fetchAssignments = () => {
    if (!accessToken || !classId) return;
    axios
      .get(`${API_BASE_URL}/api/assignments`, {
        params: { class_id: classId },
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      .then((resp) => {
        // Teachers get all their assignments (unfiltered by class server-side);
        // narrow to ones targeting this class is best-effort on the student side
        // where the server already scopes by class_id.
        setAssignments(resp.data.assignments || []);
      })
      .catch(() => setAssignments([]));
  };

  useEffect(() => {
    fetchClassDetail();
    fetchAssignments();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessToken, classId]);

  const handleCopyCode = () => {
    if (!classData?.class_code) return;
    if (!navigator.clipboard?.writeText) {
      alert(`Copy this class code: ${classData.class_code}`);
      return;
    }
    navigator.clipboard.writeText(classData.class_code).then(() => {
      setCodeCopied(true);
      setTimeout(() => setCodeCopied(false), 2000);
    }).catch(() => {
      alert(`Copy this class code: ${classData.class_code}`);
    });
  };

  const handleRemoveStudent = async (studentId) => {
    try {
      await axios.delete(`${API_BASE_URL}/api/classes/${classId}/students/${studentId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      localStorage.removeItem('vidioma_classes_cache');
      setConfirmAction(null);
      // Update in place rather than a full refetch: a transient refetch failure
      // would otherwise replace the whole (still-valid) class view with an
      // error screen.
      setStudents((prev) => prev.filter((s) => s.student_id !== studentId));
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to remove student.');
      setConfirmAction(null);
    }
  };

  // Runs a confirm-dialog action while guarding against double-submit.
  const runConfirmAction = async () => {
    if (confirmSubmitting) return;
    setConfirmSubmitting(true);
    try {
      if (confirmAction.type === 'delete') await handleDeleteClass();
      else if (confirmAction.type === 'leave') await handleLeaveClass();
      else await handleRemoveStudent(confirmAction.studentId);
    } finally {
      setConfirmSubmitting(false);
    }
  };

  const handleLeaveClass = async () => {
    try {
      await axios.delete(`${API_BASE_URL}/api/classes/${classId}/students/${user.id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      // Invalidate the cached class list so the class we just left doesn't
      // reappear (and stay clickable) when we return to the dashboard.
      localStorage.removeItem('vidioma_classes_cache');
      setConfirmAction(null);
      onBack();
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to leave class.');
      setConfirmAction(null);
    }
  };

  const handleDeleteClass = async () => {
    try {
      await axios.delete(`${API_BASE_URL}/api/classes/${classId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      localStorage.removeItem('vidioma_classes_cache');
      setConfirmAction(null);
      onBack();
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to delete class.');
      setConfirmAction(null);
    }
  };

  if (loading) {
    return (
      <div className="class-view">
        <p className="dashboard-loading">Loading class...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="class-view">
        <button className="back-button class-back-btn" onClick={onBack}>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
            <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
          </svg>
          Back to Classes
        </button>
        <p className="dashboard-error">{error}</p>
      </div>
    );
  }

  if (!classData) return null;

  const teacherProfile = classData.user_profiles;

  return (
    <div className="class-view">
      <button className="back-button class-back-btn" onClick={onBack}>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
          <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
        </svg>
        Back to Classes
      </button>

      {/* Class Header */}
      <div className="class-header">
        <div className="class-header-info">
          <h2 className="class-header-name">{classData.class_name}</h2>
          {classData.description && (
            <p className="class-header-desc">{classData.description}</p>
          )}
          <div className="class-header-tags">
            {classData.subject && <span className="class-card-tag">{classData.subject}</span>}
            {classData.grade && <span className="class-card-tag">{classData.grade}</span>}
          </div>
        </div>
        {isTeacher && (
          <div className="class-code-section">
            <span className="class-code-label">Class Code</span>
            <span className="class-code-display class-code-large">{classData.class_code}</span>
            <button className="class-code-copy-btn" onClick={handleCopyCode}>
              {codeCopied ? 'Copied!' : 'Copy Code'}
            </button>
          </div>
        )}
      </div>

      {/* Assignments */}
      <div className="class-section">
        <h3 className="class-section-title">Assignments ({assignments.length})</h3>
        {assignments.length === 0 ? (
          <p className="class-empty-students">
            {isTeacher ? 'No assignments yet. Use Create → Assignment to add one.' : 'No assignments yet.'}
          </p>
        ) : (
          <div className="assignment-list">
            {assignments.map((a) => {
              const title = assignmentLabel(a);
              const due = a.due_date ? new Date(a.due_date) : null;
              const overdue = due && due < new Date();
              // Student progress badge
              const prog = a.progress;
              const pct = prog && prog.total_lines > 0
                ? Math.round((prog.current_line_index / prog.total_lines) * 100) : 0;
              return (
                <div
                  key={a.assignment_id}
                  className="assignment-list-item"
                  onClick={() => onOpenAssignment && onOpenAssignment(a.assignment_id)}
                >
                  {/* maxWidth: when the row stacks (<=768px) the item becomes a
                      column with align-items:flex-start, so this sizes to its
                      content and a long auto-filled title overflows instead of
                      ellipsizing. Clamping to the parent restores the ellipsis. */}
                  <div className="assignment-list-main" style={{ maxWidth: '100%' }}>
                    <span className="assignment-list-title">{title}</span>
                    <span className="assignment-list-langs">
                      {(a.transcript_language || 'en').toUpperCase()} → {(a.translation_language || 'es').toUpperCase()}
                    </span>
                  </div>
                  {/* wrap: the teacher row carries three items, which would
                      otherwise overflow a 320px screen */}
                  <div className="assignment-list-meta" style={{ flexWrap: 'wrap' }}>
                    {due && (
                      <span className={`assignment-list-due ${overdue ? 'assignment-due-overdue' : ''}`}>
                        Due {due.toLocaleDateString()}
                      </span>
                    )}
                    {isTeacher ? (
                      <>
                        <span className="assignment-list-stat">
                          {a.completed_count}/{a.assigned_count} done
                        </span>
                        {onDuplicateAssignment && (
                          <button
                            className="navbar-btn"
                            onClick={(e) => {
                              e.stopPropagation();
                              onDuplicateAssignment(buildDuplicatePrefill(a, title));
                            }}
                          >
                            Duplicate
                          </button>
                        )}
                      </>
                    ) : prog?.completed ? (
                      <span className="assignment-status-complete">✓ Done</span>
                    ) : (
                      <button
                        className="class-action-btn assignment-list-btn"
                        onClick={(e) => { e.stopPropagation(); if (onStartAssignment) onStartAssignment(a); }}
                      >
                        {prog && prog.current_line_index > 0 ? `Continue (${pct}%)` : 'Start'}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Members: teacher first (its cyan-bordered card sets it apart), then students */}
      <div className="class-section">
        <h3 className="class-section-title">
          Members ({students.length + 1})
        </h3>
        <div className="class-members-list">
          <div className="class-member-card class-teacher-card">
            <div className="class-member-avatar">
              {(teacherProfile?.user_name || 'T').charAt(0).toUpperCase()}
            </div>
            <div className="class-member-info">
              <span className="class-member-name">{teacherProfile?.user_name || 'Teacher'}</span>
              <span className="class-member-badge class-badge-teacher">Teacher</span>
            </div>
          </div>
          {students.length === 0 ? (
            <p className="class-empty-students">No students have joined this class yet.</p>
          ) : (
            students.map((s) => {
              const studentName = s.user_profiles?.user_name || 'Student';
              const isSelf = s.student_id === user?.id;
              return (
                <div key={s.student_class_id} className="class-member-card">
                  <div className="class-member-avatar">
                    {studentName.charAt(0).toUpperCase()}
                  </div>
                  <div className="class-member-info">
                    <span className="class-member-name">
                      {studentName}
                      {isSelf && <span className="class-member-you"> (You)</span>}
                    </span>
                    <span className="class-member-badge class-badge-student">Student</span>
                  </div>
                  {(isTeacher || isSelf) && (
                    <button
                      className="class-remove-btn"
                      onClick={(e) => {
                        e.stopPropagation();
                        setConfirmAction({
                          type: isSelf ? 'leave' : 'remove',
                          studentId: s.student_id,
                          studentName,
                        });
                      }}
                    >
                      {isSelf ? 'Leave' : 'Remove'}
                    </button>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Teacher actions */}
      {isTeacher && (
        <div className="class-danger-zone">
          <button
            className="class-delete-btn"
            onClick={() => setConfirmAction({ type: 'delete' })}
          >
            Delete Class
          </button>
        </div>
      )}

      {/* Confirm Dialog */}
      {confirmAction && (
        <div className="auth-overlay" onClick={() => setConfirmAction(null)}>
          <div className="auth-modal class-confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h2 className="auth-title">
              {confirmAction.type === 'delete'
                ? 'Delete Class?'
                : confirmAction.type === 'leave'
                  ? 'Leave Class?'
                  : `Remove ${confirmAction.studentName}?`}
            </h2>
            <p style={{ color: '#aaa', fontSize: '0.9rem', textAlign: 'center', margin: '0 0 20px' }}>
              {confirmAction.type === 'delete'
                ? 'This will permanently delete this class and remove all students. This cannot be undone.'
                : confirmAction.type === 'leave'
                  ? 'You will be removed from this class. You can rejoin later with the class code.'
                  : `${confirmAction.studentName} will be removed from this class.`}
            </p>
            <div className="class-confirm-actions">
              <button
                className="navbar-btn"
                onClick={() => setConfirmAction(null)}
                disabled={confirmSubmitting}
              >
                Cancel
              </button>
              <button
                className="class-delete-btn"
                onClick={runConfirmAction}
                disabled={confirmSubmitting}
              >
                {confirmSubmitting
                  ? '...'
                  : confirmAction.type === 'delete' ? 'Delete' : confirmAction.type === 'leave' ? 'Leave' : 'Remove'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
