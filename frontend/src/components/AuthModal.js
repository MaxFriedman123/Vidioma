import React, { useState } from 'react';
import { useAuth } from '../AuthContext';

export default function AuthModal({ mode: initialMode, onClose }) {
  const { signUp, logIn } = useAuth();
  const [mode, setMode] = useState(initialMode); // 'login' | 'signup'
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setInfo('');
    setLoading(true);

    try {
      if (mode === 'signup') {
        await signUp(email, password);
        setInfo('Check your email to confirm your account, then log in.');
      } else {
        await logIn(email, password);
        onClose(); // Close modal on successful login
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-overlay" onClick={onClose}>
      <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
        <button className="auth-close" onClick={onClose}>
          &times;
        </button>

        <h2 className="auth-title">
          {mode === 'login' ? 'Log In' : 'Sign Up'}
        </h2>

        <form onSubmit={handleSubmit} className="auth-form">
          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="auth-input"
            required
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="auth-input"
            required
            minLength={6}
          />

          {error && <p className="auth-error">{error}</p>}
          {info && <p className="auth-info">{info}</p>}

          <button type="submit" className="auth-submit" disabled={loading}>
            {loading
              ? '...'
              : mode === 'login'
              ? 'Log In'
              : 'Create Account'}
          </button>
        </form>

        <p className="auth-switch">
          {mode === 'login' ? (
            <>
              Don't have an account?{' '}
              <span onClick={() => { setMode('signup'); setError(''); setInfo(''); }}>
                Sign Up
              </span>
            </>
          ) : (
            <>
              Already have an account?{' '}
              <span onClick={() => { setMode('login'); setError(''); setInfo(''); }}>
                Log In
              </span>
            </>
          )}
        </p>
      </div>
    </div>
  );
}
