import React, { useState } from 'react';
import { useAuth } from '../AuthContext';

export default function AuthModal({ mode: initialMode, onClose }) {
  const { signUp, logIn, resetPassword, updatePassword, createProfile } = useAuth();
  const [mode, setMode] = useState(initialMode); // 'login' | 'signup' | 'forgot' | 'reset'
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [userRole, setUserRole] = useState('student'); // 'student' | 'teacher'
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);

  const switchMode = (newMode) => {
    setMode(newMode);
    setError('');
    setInfo('');
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setInfo('');
    setLoading(true);

    try {
      if (mode === 'signup') {
        if (!fullName.trim() || fullName.trim().length < 2) {
          setError('Please enter your full name (at least 2 characters).');
          setLoading(false);
          return;
        }
        const data = await signUp(email, password);
        // If signup returned a session (auto-confirmed), create profile immediately
        if (data?.session?.access_token) {
          try {
            await createProfile(fullName.trim(), userRole);
          } catch (profileErr) {
            console.error('Profile creation after signup failed:', profileErr);
          }
        }
        // Store name+role in sessionStorage so we can create profile after email confirmation login
        sessionStorage.setItem('vidioma_pending_profile', JSON.stringify({
          user_name: fullName.trim(),
          user_role: userRole,
        }));
        setInfo('Check your email to confirm your account, then log in.');
      } else if (mode === 'login') {
        await logIn(email, password);
        // Check if there's a pending profile from signup
        const pending = sessionStorage.getItem('vidioma_pending_profile');
        if (pending) {
          try {
            const { user_name, user_role } = JSON.parse(pending);
            await createProfile(user_name, user_role);
            sessionStorage.removeItem('vidioma_pending_profile');
          } catch {
            // Profile may already exist or creation may fail — non-blocking
          }
        }
        onClose();
      } else if (mode === 'forgot') {
        await resetPassword(email);
        setInfo('Check your email for a password reset link.');
      } else if (mode === 'reset') {
        if (password !== confirmPassword) {
          setError('Passwords do not match.');
          setLoading(false);
          return;
        }
        await updatePassword(password);
        setInfo('Password updated successfully!');
        setTimeout(() => onClose(), 1500);
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  const title = {
    login: 'Log In',
    signup: 'Sign Up',
    forgot: 'Reset Password',
    reset: 'Set New Password',
  }[mode];

  const submitLabel = {
    login: 'Log In',
    signup: 'Create Account',
    forgot: 'Send Reset Link',
    reset: 'Update Password',
  }[mode];

  return (
    <div className="auth-overlay" onClick={onClose}>
      <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
        <button className="auth-close" onClick={onClose}>
          &times;
        </button>

        <h2 className="auth-title">{title}</h2>

        <form onSubmit={handleSubmit} className="auth-form">
          {mode === 'signup' && (
            <>
              <input
                type="text"
                placeholder="Full Name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                className="auth-input"
                required
                minLength={2}
              />
              <div className="auth-role-selector">
                <button
                  type="button"
                  className={`auth-role-btn ${userRole === 'student' ? 'auth-role-active' : ''}`}
                  onClick={() => setUserRole('student')}
                >
                  Student
                </button>
                <button
                  type="button"
                  className={`auth-role-btn ${userRole === 'teacher' ? 'auth-role-active' : ''}`}
                  onClick={() => setUserRole('teacher')}
                >
                  Teacher
                </button>
              </div>
            </>
          )}

          {(mode === 'login' || mode === 'signup' || mode === 'forgot') && (
            <input
              type="email"
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="auth-input"
              required
            />
          )}

          {(mode === 'login' || mode === 'signup') && (
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="auth-input"
              required
              minLength={6}
            />
          )}

          {mode === 'reset' && (
            <>
              <input
                type="password"
                placeholder="New password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="auth-input"
                required
                minLength={6}
              />
              <input
                type="password"
                placeholder="Confirm new password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="auth-input"
                required
                minLength={6}
              />
            </>
          )}

          {error && <p className="auth-error">{error}</p>}
          {info && <p className="auth-info">{info}</p>}

          <button type="submit" className="auth-submit" disabled={loading}>
            {loading ? '...' : submitLabel}
          </button>
        </form>

        {mode === 'login' && (
          <p className="auth-forgot">
            <span onClick={() => switchMode('forgot')}>
              Forgot your password?
            </span>
          </p>
        )}

        <p className="auth-switch">
          {mode === 'login' && (
            <>
              Don't have an account?{' '}
              <span onClick={() => switchMode('signup')}>Sign Up</span>
            </>
          )}
          {mode === 'signup' && (
            <>
              Already have an account?{' '}
              <span onClick={() => switchMode('login')}>Log In</span>
            </>
          )}
          {mode === 'forgot' && (
            <>
              Remember your password?{' '}
              <span onClick={() => switchMode('login')}>Log In</span>
            </>
          )}
        </p>
      </div>
    </div>
  );
}
