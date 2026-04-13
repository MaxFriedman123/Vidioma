import React, { useState, useRef, useEffect } from 'react';
import { useAuth } from '../AuthContext';

export default function Navbar({ onDashboard, onHome, onOpenAuth }) {
  const { isAuthenticated, user, logOut } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);

  const handleLogOut = async () => {
    setMenuOpen(false);
    await logOut();
    onHome();
  };

  // Close menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setMenuOpen(false);
      }
    };
    if (menuOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      document.addEventListener('touchstart', handleClickOutside);
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('touchstart', handleClickOutside);
    };
  }, [menuOpen]);

  return (
    <nav className="navbar" ref={menuRef}>
      <span className="navbar-brand" onClick={onHome}>
        Vidioma
      </span>

      {/* Desktop actions */}
      <div className="navbar-actions navbar-desktop">
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

      {/* Mobile hamburger */}
      <button
        className="navbar-hamburger"
        onClick={() => setMenuOpen(!menuOpen)}
        aria-label="Toggle menu"
      >
        <span className={`hamburger-line ${menuOpen ? 'open' : ''}`} />
        <span className={`hamburger-line ${menuOpen ? 'open' : ''}`} />
        <span className={`hamburger-line ${menuOpen ? 'open' : ''}`} />
      </button>

      {/* Mobile dropdown */}
      {menuOpen && (
        <div className="navbar-mobile-menu">
          {isAuthenticated ? (
            <>
              <span className="navbar-mobile-email">{user?.email}</span>
              <button className="navbar-mobile-item" onClick={() => { setMenuOpen(false); onDashboard(); }}>
                My Dashboard
              </button>
              <button className="navbar-mobile-item" onClick={handleLogOut}>
                Log Out
              </button>
            </>
          ) : (
            <>
              <button className="navbar-mobile-item" onClick={() => { setMenuOpen(false); onOpenAuth('login'); }}>
                Log In
              </button>
              <button className="navbar-mobile-item" onClick={() => { setMenuOpen(false); onOpenAuth('signup'); }}>
                Sign Up
              </button>
            </>
          )}
        </div>
      )}
    </nav>
  );
}
