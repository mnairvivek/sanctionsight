import React, { useState } from 'react';
import { motion } from 'framer-motion';
import { Shield, LogIn, Loader2 } from 'lucide-react';
import { useAuth } from '../auth';

export default function LoginScreen() {
  const { login, loading, error } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [localErr, setLocalErr] = useState(null);

  const onSubmit = async (e) => {
    e.preventDefault();
    setLocalErr(null);
    if (!email.trim() || !password) {
      setLocalErr('Email and password required');
      return;
    }
    await login(email.trim().toLowerCase(), password);
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-bg px-4">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-sm bg-surface rounded-2xl shadow-card border border-border/50 p-8"
      >
        <div className="flex items-center gap-3 mb-6">
          <Shield size={22} className="text-accent" />
          <div>
            <h1 className="font-display font-bold text-lg text-primary leading-none">
              SanctionSight
            </h1>
            <span className="text-muted text-[11px] font-mono tracking-wider uppercase">
              Investigator Workspace
            </span>
          </div>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-muted mb-1.5">Email</label>
            <input
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3 py-2.5 rounded-xl bg-bg border border-border/50 focus:border-accent/50
                         focus:outline-none focus:ring-2 focus:ring-accent/20 text-sm text-primary"
              placeholder="analyst@bank.example"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-muted mb-1.5">Password</label>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2.5 rounded-xl bg-bg border border-border/50 focus:border-accent/50
                         focus:outline-none focus:ring-2 focus:ring-accent/20 text-sm text-primary"
            />
          </div>

          {(localErr || error) && (
            <div className="text-xs text-danger bg-danger/10 border border-danger/20 rounded-lg px-3 py-2">
              {localErr || error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-accent text-white
                       font-display font-bold text-sm shadow-glow hover:shadow-lifted transition-all
                       disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="text-[11px] text-muted/70 mt-6 leading-relaxed">
          Sessions last 8 hours by default. Contact an admin for access —
          new user accounts are created from the admin panel only.
        </p>
      </motion.div>
    </div>
  );
}
