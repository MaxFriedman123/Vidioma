import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { supabase } from './supabaseClient';
import axios from 'axios';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [passwordRecoveryPending, setPasswordRecoveryPending] = useState(false);
  const [userProfile, setUserProfile] = useState(() => {
    try {
      const cached = localStorage.getItem('vidioma_user_profile');
      return cached ? JSON.parse(cached) : null;
    } catch { return null; }
  });
  const [profileLoading, setProfileLoading] = useState(false);

  // ── Bootstrap session ──────────────────────────────────────────────
  useEffect(() => {
    if (!supabase) {
      setLoading(false);
      return;
    }

    // Get current session
    supabase.auth.getSession()
      .then(({ data: { session: s } }) => {
        setSession(s);
      })
      .catch((err) => {
        // Never leave the app stuck on a blank loading screen if the initial
        // session lookup fails — fall back to the signed-out state.
        console.error('getSession failed:', err);
        setSession(null);
      })
      .finally(() => setLoading(false));

    // Listen for auth changes (login, logout, token refresh, password recovery)
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (event, s) => {
        setSession(s);
        if (event === 'PASSWORD_RECOVERY') {
          setPasswordRecoveryPending(true);
        }
      }
    );

    return () => subscription.unsubscribe();
  }, []);

  // ── Persist profile to localStorage ─────────────────────────────────
  const setAndCacheProfile = useCallback((profile) => {
    setUserProfile(profile);
    if (profile) {
      localStorage.setItem('vidioma_user_profile', JSON.stringify(profile));
    } else {
      localStorage.removeItem('vidioma_user_profile');
    }
  }, []);

  // Tracks the token of the most recent profile fetch so a slow in-flight
  // request can't apply its result after logout or an account switch.
  const activeProfileTokenRef = useRef(null);

  // ── Fetch user profile when session changes ────────────────────────
  const fetchProfile = useCallback(async (token) => {
    if (!token) { activeProfileTokenRef.current = null; setAndCacheProfile(null); return; }
    activeProfileTokenRef.current = token;
    // Only show loading spinner if we have no cached profile
    const hasCached = !!localStorage.getItem('vidioma_user_profile');
    if (!hasCached) setProfileLoading(true);
    try {
      const resp = await axios.get(`${API_BASE_URL}/api/profile`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      // Discard the result if the session changed (logout / new login) while
      // this request was in flight.
      if (activeProfileTokenRef.current !== token) return;
      setAndCacheProfile(resp.data.profile || null);
    } catch {
      if (activeProfileTokenRef.current !== token) return;
      // Keep cached profile on network error; only clear if we had none
      if (!hasCached) setAndCacheProfile(null);
    } finally {
      if (activeProfileTokenRef.current === token) setProfileLoading(false);
    }
  }, [setAndCacheProfile]);

  useEffect(() => {
    if (session?.access_token) {
      fetchProfile(session.access_token);
    } else {
      setAndCacheProfile(null);
    }
  }, [session, fetchProfile, setAndCacheProfile]);

  // Accept an optional explicit token: right after signUp/logIn the `session`
  // in this closure hasn't been updated by onAuthStateChange yet, so callers
  // (e.g. AuthModal) can pass the token they just received to avoid a spurious
  // "Not authenticated" error.
  const createProfile = useCallback(async (userName, userRole, tokenOverride) => {
    const token = tokenOverride || session?.access_token;
    if (!token) throw new Error('Not authenticated');
    const resp = await axios.post(`${API_BASE_URL}/api/profile`, {
      user_name: userName,
      user_role: userRole,
    }, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const profile = resp.data.profile;
    setAndCacheProfile(profile);
    return profile;
  }, [session, setAndCacheProfile]);

  const updateProfileName = useCallback(async (userName, tokenOverride) => {
    const token = tokenOverride || session?.access_token;
    if (!token) throw new Error('Not authenticated');
    const resp = await axios.patch(`${API_BASE_URL}/api/profile/name`, {
      user_name: userName,
    }, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const profile = resp.data.profile;
    setAndCacheProfile(profile);
    return profile;
  }, [session, setAndCacheProfile]);

  // ── Migrate localStorage progress on first login ───────────────────
  useEffect(() => {
    if (!session) return;

    const migrated = sessionStorage.getItem('vidioma_progress_migrated');
    if (migrated) return;

    // Collect all guest progress keys
    const keysToMigrate = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith('vidioma_progress_')) {
        keysToMigrate.push(key);
      }
    }

    if (keysToMigrate.length === 0) {
      sessionStorage.setItem('vidioma_progress_migrated', 'true');
      return;
    }

    const token = session.access_token;

    // Fire an upsert for each saved progress entry. Track per-key success so we
    // only delete localStorage entries that actually made it to the server —
    // otherwise a failed upsert (offline, 500) silently destroys the guest's
    // progress. A malformed/unparseable key is safe to drop (nothing to save).
    const migrations = keysToMigrate.map((key) => {
      let data;
      try {
        data = JSON.parse(localStorage.getItem(key));
      } catch {
        return { key, deletable: true }; // corrupt entry — nothing to preserve
      }
      if (!data || !data.youtube_id) {
        return { key, deletable: true };
      }

      return axios.post(`${API_BASE_URL}/api/progress/upsert`, {
        youtube_id: data.youtube_id,
        transcript_language: data.transcript_language || 'en',
        translation_language: data.translation_language || 'es',
        current_line_index: data.current_line_index || 0,
        total_lines: data.total_lines || 0,
        title: data.title || null,
      }, {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then(() => ({ key, deletable: true }))
        .catch(() => ({ key, deletable: false })); // keep for a later retry
    });

    Promise.allSettled(migrations).then((results) => {
      let allDeletable = true;
      results.forEach((r) => {
        const outcome = r.status === 'fulfilled' ? r.value : null;
        if (outcome && outcome.deletable) {
          localStorage.removeItem(outcome.key);
        } else {
          allDeletable = false;
        }
      });
      // Only mark migration complete once every entry was safely migrated, so
      // entries that failed this session get retried on the next login.
      if (allDeletable) {
        sessionStorage.setItem('vidioma_progress_migrated', 'true');
      }
    });
  }, [session]);

  // ── Auth helpers ───────────────────────────────────────────────────
  const signUp = useCallback(async (email, password) => {
    if (!supabase) throw new Error('Auth not configured');
    const { data, error } = await supabase.auth.signUp({ email, password });
    if (error) throw error;
    // Supabase returns 200 with empty identities and no session for existing emails
    if (data?.user?.identities?.length === 0) {
      throw new Error('An account with this email already exists. Please log in instead.');
    }
    return data;
  }, []);

  const logIn = useCallback(async (email, password) => {
    if (!supabase) throw new Error('Auth not configured');
    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;
    return data;
  }, []);

  const resetPassword = useCallback(async (email) => {
    if (!supabase) throw new Error('Auth not configured');
    const { error } = await supabase.auth.resetPasswordForEmail(email, {
      redirectTo: window.location.origin,
    });
    if (error) throw error;
  }, []);

  const updatePassword = useCallback(async (newPassword) => {
    if (!supabase) throw new Error('Auth not configured');
    const { error } = await supabase.auth.updateUser({ password: newPassword });
    if (error) throw error;
    setPasswordRecoveryPending(false);
  }, []);

  const logOut = useCallback(async () => {
    if (!supabase) return;
    activeProfileTokenRef.current = null;
    sessionStorage.removeItem('vidioma_progress_migrated');
    sessionStorage.removeItem('vidioma_pending_profile');
    localStorage.removeItem('vidioma_user_profile');
    localStorage.removeItem('vidioma_classes_cache');
    localStorage.removeItem('vidioma_dashboard_cache');
    // Clear all localStorage progress so it doesn't leak to the next account
    const keysToRemove = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith('vidioma_progress_')) {
        keysToRemove.push(key);
      }
    }
    keysToRemove.forEach((key) => localStorage.removeItem(key));
    await supabase.auth.signOut();
  }, []);

  const value = {
    session,
    user: session?.user ?? null,
    accessToken: session?.access_token ?? null,
    loading,
    signUp,
    logIn,
    logOut,
    resetPassword,
    updatePassword,
    passwordRecoveryPending,
    clearPasswordRecovery: () => setPasswordRecoveryPending(false),
    isAuthenticated: !!session,
    userProfile,
    profileLoading,
    createProfile,
    updateProfileName,
    refreshProfile: () => fetchProfile(session?.access_token),
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
