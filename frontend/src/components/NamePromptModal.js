import React, { useState } from 'react';
import { useAuth } from '../AuthContext';

export default function NamePromptModal() {
  const { createProfile, updateProfileName, userProfile } = useAuth();
  const [name, setName] = useState('');
  const [role, setRole] = useState('student');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  // If profile exists but has no name, we just need the name
  // If no profile at all, we need both name and role
  const needsRole = !userProfile;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    if (!name.trim() || name.trim().length < 2) {
      setError('Please enter your full name (at least 2 characters).');
      return;
    }

    setLoading(true);
    try {
      if (needsRole) {
        await createProfile(name.trim(), role);
      } else {
        await updateProfileName(name.trim());
      }
    } catch (err) {
      setError(err.response?.data?.error || err.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-overlay">
      <div className="auth-modal">
        <h2 className="auth-title">Complete Your Profile</h2>
        <p style={{ color: '#aaa', fontSize: '0.9rem', textAlign: 'center', margin: '0 0 20px' }}>
          Please add your name to get started
        </p>

        <form onSubmit={handleSubmit} className="auth-form">
          <input
            type="text"
            placeholder="Full Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="auth-input"
            required
            minLength={2}
            autoFocus
          />

          {needsRole && (
            <>
              <p style={{ color: '#aaa', fontSize: '0.85rem', textAlign: 'center', margin: '8px 0 4px' }}>
                I am a...
              </p>
              <div className="auth-role-selector">
                <button
                  type="button"
                  className={`auth-role-btn ${role === 'student' ? 'auth-role-active' : ''}`}
                  onClick={() => setRole('student')}
                >
                  Student
                </button>
                <button
                  type="button"
                  className={`auth-role-btn ${role === 'teacher' ? 'auth-role-active' : ''}`}
                  onClick={() => setRole('teacher')}
                >
                  Teacher
                </button>
              </div>
            </>
          )}

          {error && <p className="auth-error">{error}</p>}

          <button type="submit" className="auth-submit" disabled={loading}>
            {loading ? '...' : 'Continue'}
          </button>
        </form>
      </div>
    </div>
  );
}
