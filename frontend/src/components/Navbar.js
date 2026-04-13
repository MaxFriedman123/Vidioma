import React from 'react';
import { useAuth } from '../AuthContext';

export default function Navbar({ onDashboard, onHome, onOpenAuth }) {
  const { isAuthenticated, user, logOut } = useAuth();

  const handleLogOut = async () => {
    await logOut();
    onHome();
  };

  return (
    <nav className="navbar">
      <span className="navbar-brand" onClick={onHome}>
        Vidioma
      </span>

      <div className="navbar-actions">
        {isAuthenticated ? (
          <>
            <button className="navbar-btn" onClick={onDashboard}>
              My Dashboard
            </button>
            <span className="navbar-email">{user?.email}</span>
            <button className="navbar-btn navbar-btn-outline" onClick={handleLogOut}>
              Log Out
            </button>
          </>
        ) : (
          <>
            <button className="navbar-btn" onClick={() => onOpenAuth('login')}>
              Log In
            </button>
            <button className="navbar-btn navbar-btn-primary" onClick={() => onOpenAuth('signup')}>
              Sign Up
            </button>
          </>
        )}
      </div>
    </nav>
  );
}
