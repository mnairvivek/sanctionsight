import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

// Token storage. We use sessionStorage so the token is forgotten when the
// tab closes — fine for a workstation-shared analyst setup and avoids the
// classic localStorage persistence footgun.
const TOKEN_KEY = 'sanctionsight.token';
const USER_KEY = 'sanctionsight.user';

function readToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || null; } catch { return null; }
}
function readUser() {
  try { return JSON.parse(sessionStorage.getItem(USER_KEY) || 'null'); } catch { return null; }
}

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(readToken);
  const [user, setUser] = useState(readUser);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Re-fetch /me on boot when we only have a token — catches stale sessions.
  useEffect(() => {
    if (!token || user) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/auth/me', { headers: { Authorization: `Bearer ${token}` } });
        if (!res.ok) throw new Error('session expired');
        const data = await res.json();
        if (!cancelled) {
          setUser(data);
          sessionStorage.setItem(USER_KEY, JSON.stringify(data));
        }
      } catch {
        if (!cancelled) logout();
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const login = useCallback(async (email, password) => {
    setLoading(true); setError(null);
    try {
      const body = new URLSearchParams();
      body.set('username', email);
      body.set('password', password);
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || 'Login failed');
      }
      const data = await res.json();
      const u = { email: data.email, role: data.role };
      setToken(data.access_token);
      setUser(u);
      sessionStorage.setItem(TOKEN_KEY, data.access_token);
      sessionStorage.setItem(USER_KEY, JSON.stringify(u));
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  const logout = useCallback(() => {
    setToken(null); setUser(null);
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
  }, []);

  const authedFetch = useCallback(async (url, opts = {}) => {
    const headers = new Headers(opts.headers || {});
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const res = await fetch(url, { ...opts, headers });
    if (res.status === 401) logout();
    return res;
  }, [token, logout]);

  const value = useMemo(() => ({
    token, user, loading, error,
    isAuthed: !!token && !!user,
    isReviewer: user?.role === 'reviewer' || user?.role === 'admin',
    isAdmin: user?.role === 'admin',
    login, logout, authedFetch,
  }), [token, user, loading, error, login, logout, authedFetch]);

  return React.createElement(AuthContext.Provider, { value }, children);
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
