import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useAuth } from '../AuthContext';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

export default function ClassView({ classId, onBack }) {
  const { accessToken, user } = useAuth();
  const [classData, setClassData] = useState(null);
  const [students, setStudents] = useState([]);
  const [isTeacher, setIsTeacher] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [codeCopied, setCodeCopied] = useState(false);

  // Confirm dialog state
  const [confirmAction, setConfirmAction] = useState(null); // { type, studentId, studentName }

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

  useEffect(() => {
    fetchClassDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessToken, classId]);

  const handleCopyCode = () => {
    if (!classData?.class_code) return;
    navigator.clipboard.writeText(classData.class_code).then(() => {
      setCodeCopied(true);
      setTimeout(() => setCodeCopied(false), 2000);
    });
  };

  const handleRemoveStudent = async (studentId) => {
    try {
      await axios.delete(`${API_BASE_URL}/api/classes/${classId}/students/${studentId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      setConfirmAction(null);
      fetchClassDetail();
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to remove student.');
      setConfirmAction(null);
    }
  };

  const handleLeaveClass = async () => {
    try {
      await axios.delete(`${API_BASE_URL}/api/classes/${classId}/students/${user.id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
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

      {/* Teacher Card */}
      <div className="class-section">
        <h3 className="class-section-title">Teacher</h3>
        <div className="class-member-card class-teacher-card">
          <div className="class-member-avatar">
            {(teacherProfile?.user_name || 'T').charAt(0).toUpperCase()}
          </div>
          <div className="class-member-info">
            <span className="class-member-name">{teacherProfile?.user_name || 'Teacher'}</span>
            <span className="class-member-badge class-badge-teacher">Teacher</span>
          </div>
        </div>
      </div>

      {/* Students List */}
      <div className="class-section">
        <h3 className="class-section-title">
          Students ({students.length})
        </h3>
        {students.length === 0 ? (
          <p className="class-empty-students">No students have joined this class yet.</p>
        ) : (
          <div className="class-members-list">
            {students.map((s) => {
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
            })}
          </div>
        )}
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
              <button className="navbar-btn" onClick={() => setConfirmAction(null)}>
                Cancel
              </button>
              <button
                className="class-delete-btn"
                onClick={() => {
                  if (confirmAction.type === 'delete') handleDeleteClass();
                  else if (confirmAction.type === 'leave') handleLeaveClass();
                  else handleRemoveStudent(confirmAction.studentId);
                }}
              >
                {confirmAction.type === 'delete' ? 'Delete' : confirmAction.type === 'leave' ? 'Leave' : 'Remove'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
