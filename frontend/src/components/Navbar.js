import React, { useState, useRef, useEffect } from 'react';
import { useAuth } from '../AuthContext';

export default function Navbar({ onDashboard, onHome, onOpenAuth, onClasses }) {
  const { isAuthenticated, logOut, userProfile, profileLoading } = useAuth();
  const [avatarMenuOpen, setAvatarMenuOpen] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const avatarRef = useRef(null);
  const mobileRef = useRef(null);

  const handleLogOut = async () => {
    setAvatarMenuOpen(false);
    setMobileMenuOpen(false);
    await logOut();
    onHome();
  };

  // Close menus when clicking outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (avatarRef.current && !avatarRef.current.contains(e.target)) {
        setAvatarMenuOpen(false);
      }
      if (mobileRef.current && !mobileRef.current.contains(e.target)) {
        setMobileMenuOpen(false);
      }
    };
    if (avatarMenuOpen || mobileMenuOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      document.addEventListener('touchstart', handleClickOutside);
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('touchstart', handleClickOutside);
    };
  }, [avatarMenuOpen, mobileMenuOpen]);

  const initial = userProfile?.user_name?.charAt(0)?.toUpperCase() || null;
  const profileReady = !profileLoading && !!userProfile;

  return (
    <nav className="navbar">
      <span className="navbar-brand" onClick={onHome}>
        Vidioma
      </span>

      {/* Desktop actions */}
      <div className="navbar-actions navbar-desktop">
        {isAuthenticated ? (
          <>
            <button className="navbar-btn" onClick={onClasses}>
              My Classes
            </button>
            <button className="navbar-btn" onClick={onDashboard}>
              My Dashboard
            </button>
            <div className="navbar-avatar-wrapper" ref={avatarRef}>
              {profileReady ? (
                <button
                  className="navbar-avatar"
                  onClick={() => setAvatarMenuOpen(!avatarMenuOpen)}
                  aria-label="Account menu"
                >
                  {initial}
                </button>
              ) : (
                <div className="navbar-avatar navbar-avatar-skeleton" />
              )}
              {avatarMenuOpen && profileReady && (
                <div className="navbar-avatar-menu">
                  <div className="navbar-avatar-menu-header">
                    <span className="navbar-avatar-menu-name">{userProfile.user_name}</span>
                    <span className="navbar-avatar-menu-role">{userProfile.user_role}</span>
                  </div>
                  <div className="navbar-avatar-menu-divider" />
                  <button className="navbar-avatar-menu-item" onClick={() => { setAvatarMenuOpen(false); onClasses(); }}>
                    My Classes
                  </button>
                  <button className="navbar-avatar-menu-item" onClick={() => { setAvatarMenuOpen(false); onDashboard(); }}>
                    My Dashboard
                  </button>
                  <div className="navbar-avatar-menu-divider" />
                  <button className="navbar-avatar-menu-item navbar-avatar-menu-logout" onClick={handleLogOut}>
                    Log Out
                  </button>
                </div>
              )}
            </div>
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
      <div className="navbar-mobile-controls" ref={mobileRef}>
        {isAuthenticated ? (
          <>
            {profileReady ? (
              <button
                className="navbar-avatar navbar-avatar-mobile"
                onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
                aria-label="Account menu"
              >
                {initial}
              </button>
            ) : (
              <div className="navbar-avatar navbar-avatar-mobile navbar-avatar-skeleton" />
            )}
          </>
        ) : (
          <button
            className="navbar-hamburger"
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
            aria-label="Toggle menu"
          >
            <span className={`hamburger-line ${mobileMenuOpen ? 'open' : ''}`} />
            <span className={`hamburger-line ${mobileMenuOpen ? 'open' : ''}`} />
            <span className={`hamburger-line ${mobileMenuOpen ? 'open' : ''}`} />
          </button>
        )}

        {/* Mobile dropdown */}
        {mobileMenuOpen && (
          <div className="navbar-mobile-menu">
            {isAuthenticated ? (
              <>
                {profileReady && (
                  <div className="navbar-avatar-menu-header">
                    <span className="navbar-avatar-menu-name">{userProfile.user_name}</span>
                    <span className="navbar-avatar-menu-role">{userProfile.user_role}</span>
                  </div>
                )}
                <div className="navbar-avatar-menu-divider" />
                <button className="navbar-mobile-item" onClick={() => { setMobileMenuOpen(false); onClasses(); }}>
                  My Classes
                </button>
                <button className="navbar-mobile-item" onClick={() => { setMobileMenuOpen(false); onDashboard(); }}>
                  My Dashboard
                </button>
                <div className="navbar-avatar-menu-divider" />
                <button className="navbar-mobile-item" onClick={handleLogOut}>
                  Log Out
                </button>
              </>
            ) : (
              <>
                <button className="navbar-mobile-item" onClick={() => { setMobileMenuOpen(false); onOpenAuth('login'); }}>
                  Log In
                </button>
                <button className="navbar-mobile-item" onClick={() => { setMobileMenuOpen(false); onOpenAuth('signup'); }}>
                  Sign Up
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </nav>
  );
}
