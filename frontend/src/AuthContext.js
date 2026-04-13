import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { supabase } from './supabaseClient';
import axios from 'axios';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);

  // ── Bootstrap session ──────────────────────────────────────────────
  useEffect(() => {
    if (!supabase) {
      setLoading(false);
      return;
    }

    // Get current session
    supabase.auth.getSession().then(({ data: { session: s } }) => {
      setSession(s);
      setLoading(false);
    });

    // Listen for auth changes (login, logout, token refresh)
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (_event, s) => setSession(s)
    );

    return () => subscription.unsubscribe();
  }, []);

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

    // Fire upsert calls for each saved progress entry
    const promises = keysToMigrate.map((key) => {
      try {
        const data = JSON.parse(localStorage.getItem(key));
        if (!data || !data.youtube_id) return Promise.resolve();

        return axios.post(`${API_BASE_URL}/api/progress/upsert`, {
          youtube_id: data.youtube_id,
          transcript_language: data.transcript_language || 'en',
          translation_language: data.translation_language || 'es',
          current_line_index: data.current_line_index || 0,
          total_lines: data.total_lines || 0,
          title: data.title || null,
        }, {
          headers: { Authorization: `Bearer ${token}` },
        });
      } catch {
        return Promise.resolve();
      }
    });

    Promise.allSettled(promises).then(() => {
      // Clear migrated keys
      keysToMigrate.forEach((key) => localStorage.removeItem(key));
      sessionStorage.setItem('vidioma_progress_migrated', 'true');
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

  const logOut = useCallback(async () => {
    if (!supabase) return;
    sessionStorage.removeItem('vidioma_progress_migrated');
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
    isAuthenticated: !!session,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
