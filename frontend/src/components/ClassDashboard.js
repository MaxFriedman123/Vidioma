import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useAuth } from '../AuthContext';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

export default function ClassDashboard({ onSelectClass }) {
  const { accessToken, userProfile } = useAuth();
  const [classes, setClasses] = useState(() => {
    try {
      const cached = localStorage.getItem('vidioma_classes_cache');
      return cached ? JSON.parse(cached) : [];
    } catch { return []; }
  });
  const [loading, setLoading] = useState(() => {
    return !localStorage.getItem('vidioma_classes_cache');
  });
  const [error, setError] = useState('');

  // Create class modal state (teacher)
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [className, setClassName] = useState('');
  const [description, setDescription] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');

  // Join class state (student)
  const [showJoinModal, setShowJoinModal] = useState(false);
  const [classCode, setClassCode] = useState('');
  const [joining, setJoining] = useState(false);
  const [joinError, setJoinError] = useState('');
  const [joinSuccess, setJoinSuccess] = useState('');

  // Newly created class code display
  const [newClassCode, setNewClassCode] = useState('');
  const [codeCopied, setCodeCopied] = useState(false);

  const role = userProfile?.user_role;
  const isTeacher = role === 'teacher';

  const fetchClasses = () => {
    if (!accessToken) return;
    const hasCached = !!localStorage.getItem('vidioma_classes_cache');
    if (!hasCached) setLoading(true);
    axios
      .get(`${API_BASE_URL}/api/classes`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      .then((resp) => {
        const data = resp.data.classes || [];
        setClasses(data);
        localStorage.setItem('vidioma_classes_cache', JSON.stringify(data));
      })
      .catch(() => { if (!hasCached) setError('Failed to load classes.'); })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchClasses();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessToken]);

  const handleCreateClass = async (e) => {
    e.preventDefault();
    setCreateError('');
    if (!className.trim()) {
      setCreateError('Class name is required.');
      return;
    }
    setCreating(true);
    try {
      const resp = await axios.post(`${API_BASE_URL}/api/classes`, {
        class_name: className.trim(),
        description: description.trim() || undefined,
      }, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      setNewClassCode(resp.data.class.class_code);
      setClassName('');
      setDescription('');
      setShowCreateModal(false);
      fetchClasses();
    } catch (err) {
      setCreateError(err.response?.data?.error || 'Failed to create class.');
    } finally {
      setCreating(false);
    }
  };

  const handleJoinClass = async (e) => {
    e.preventDefault();
    setJoinError('');
    setJoinSuccess('');
    if (!classCode.trim() || classCode.trim().length !== 6) {
      setJoinError('Please enter a valid 6-character class code.');
      return;
    }
    setJoining(true);
    try {
      const resp = await axios.post(`${API_BASE_URL}/api/classes/join`, {
        class_code: classCode.trim().toUpperCase(),
      }, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      setJoinSuccess(resp.data.message);
      setClassCode('');
      fetchClasses();
      setTimeout(() => {
        setShowJoinModal(false);
        setJoinSuccess('');
      }, 1500);
    } catch (err) {
      setJoinError(err.response?.data?.error || 'Failed to join class.');
    } finally {
      setJoining(false);
    }
  };

  const handleCopyCode = (code) => {
    navigator.clipboard.writeText(code).then(() => {
      setCodeCopied(true);
      setTimeout(() => setCodeCopied(false), 2000);
    });
  };

  if (loading) {
    return (
      <div className="class-dashboard">
        <h2 className="dashboard-title">My Classes</h2>
        <p className="dashboard-loading">Loading classes...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="class-dashboard">
        <h2 className="dashboard-title">My Classes</h2>
        <p className="dashboard-error">{error}</p>
      </div>
    );
  }

  return (
    <div className="class-dashboard">
      <div className="class-dashboard-header">
        <h2 className="dashboard-title">My Classes</h2>
        {isTeacher ? (
          <button className="class-action-btn" onClick={() => setShowCreateModal(true)}>
            + Create Class
          </button>
        ) : (
          <button className="class-action-btn" onClick={() => setShowJoinModal(true)}>
            + Join Class
          </button>
        )}
      </div>

      {/* New class code display */}
      {newClassCode && (
        <div className="class-code-banner">
          <span>Class created! Share this code with your students:</span>
          <span className="class-code-display">{newClassCode}</span>
          <button className="class-code-copy-btn" onClick={() => handleCopyCode(newClassCode)}>
            {codeCopied ? 'Copied!' : 'Copy'}
          </button>
          <button className="guest-banner-dismiss" onClick={() => setNewClassCode('')}>&times;</button>
        </div>
      )}

      {classes.length === 0 ? (
        <div className="class-empty-state">
          {isTeacher ? (
            <>
              <p>You haven't created any classes yet.</p>
              <p style={{ color: '#888', fontSize: '0.9rem' }}>
                Click "Create Class" to get started and share the code with your students.
              </p>
            </>
          ) : (
            <>
              <p>You haven't joined any classes yet.</p>
              <p style={{ color: '#888', fontSize: '0.9rem' }}>
                Ask your teacher for a class code and click "Join Class" to get started.
              </p>
            </>
          )}
        </div>
      ) : (
        <div className="class-grid">
          {classes.map((cls) => {
            const studentCount = isTeacher
              ? (cls.student_classes?.[0]?.count || 0)
              : null;
            const teacherName = !isTeacher
              ? cls.user_profiles?.user_name
              : null;

            return (
              <div
                key={cls.class_id}
                className="class-card"
                onClick={() => onSelectClass(cls.class_id)}
              >
                <div className="class-card-header">
                  <h3 className="class-card-name">{cls.class_name}</h3>
                  {isTeacher && (
                    <span className="class-card-code">{cls.class_code}</span>
                  )}
                </div>
                {cls.description && (
                  <p className="class-card-desc">{cls.description}</p>
                )}
                <div className="class-card-meta">
                  {isTeacher && (
                    <span className="class-card-students">
                      {studentCount} student{studentCount !== 1 ? 's' : ''}
                    </span>
                  )}
                  {teacherName && (
                    <span className="class-card-teacher">Teacher: {teacherName}</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Create Class Modal */}
      {showCreateModal && (
        <div className="auth-overlay" onClick={() => setShowCreateModal(false)}>
          <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
            <button className="auth-close" onClick={() => setShowCreateModal(false)}>&times;</button>
            <h2 className="auth-title">Create a Class</h2>
            <form onSubmit={handleCreateClass} className="auth-form">
              <input
                type="text"
                placeholder="Class Name (e.g., Spanish 101)"
                value={className}
                onChange={(e) => setClassName(e.target.value)}
                className="auth-input"
                required
                autoFocus
              />
              <input
                type="text"
                placeholder="Description (optional)"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="auth-input"
              />
              {createError && <p className="auth-error">{createError}</p>}
              <button type="submit" className="auth-submit" disabled={creating}>
                {creating ? '...' : 'Create Class'}
              </button>
            </form>
          </div>
        </div>
      )}

      {/* Join Class Modal */}
      {showJoinModal && (
        <div className="auth-overlay" onClick={() => setShowJoinModal(false)}>
          <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
            <button className="auth-close" onClick={() => setShowJoinModal(false)}>&times;</button>
            <h2 className="auth-title">Join a Class</h2>
            <form onSubmit={handleJoinClass} className="auth-form">
              <input
                type="text"
                placeholder="Enter 6-character class code"
                value={classCode}
                onChange={(e) => setClassCode(e.target.value.toUpperCase().slice(0, 6))}
                className="auth-input class-code-input"
                required
                maxLength={6}
                autoFocus
              />
              {joinError && <p className="auth-error">{joinError}</p>}
              {joinSuccess && <p className="auth-info">{joinSuccess}</p>}
              <button type="submit" className="auth-submit" disabled={joining}>
                {joining ? '...' : 'Join Class'}
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
