import React, { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, ShieldCheck, Loader2, AlertTriangle } from 'lucide-react';
import { useAuth } from '../auth';

export default function SignOffModal({ jobId, onClose, onSuccess }) {
  const { authedFetch, user } = useAuth();
  const [notes, setNotes] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !submitting) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, submitting]);

  const submit = async (e) => {
    e.preventDefault();
    if (!notes.trim()) {
      setError('Final disposition notes are required for regulator-ready sign-off.');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/sign-off`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ final_disposition_notes: notes.trim() }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      onSuccess?.();
    } catch (err) {
      setError(err.message || 'Sign-off failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-[60] bg-black/40 backdrop-blur-sm flex items-center justify-center px-4"
        onClick={() => !submitting && onClose()}
      >
        <motion.form
          onSubmit={submit}
          initial={{ scale: 0.96, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.98, opacity: 0 }}
          onClick={(e) => e.stopPropagation()}
          className="bg-surface rounded-2xl shadow-lifted border border-border/50 w-full max-w-xl overflow-hidden"
        >
          <div className="flex items-start justify-between gap-3 p-5 border-b border-border/40">
            <div>
              <div className="flex items-center gap-2 text-accent mb-1">
                <ShieldCheck size={18} />
                <h3 className="font-display font-bold text-lg text-primary tracking-tight">
                  Sign off on case
                </h3>
              </div>
              <p className="text-xs text-muted">
                Case {jobId} · signed off by {user?.email}
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="p-1.5 rounded-lg hover:bg-muted/10 text-muted"
            >
              <X size={16} />
            </button>
          </div>

          <div className="p-5 space-y-4">
            <div className="text-xs text-primary/80 bg-warning/5 border border-warning/20 rounded-lg p-3 flex items-start gap-2">
              <AlertTriangle size={14} className="mt-0.5 text-warning flex-shrink-0" />
              <span>
                Sign-off is a tamper-evident event recorded in the audit chain. Reopening later requires admin privilege
                and is logged as a separate action.
              </span>
            </div>

            <div>
              <label className="block text-xs font-bold text-muted uppercase tracking-wider mb-1.5">
                Final disposition notes
              </label>
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                rows={6}
                autoFocus
                placeholder="Summarize the disposition rationale — what was confirmed, what was cleared, and why this case can close."
                className="w-full px-3 py-2 rounded-lg bg-bg border border-border/50 focus:border-accent/50 focus:ring-0 text-sm text-primary placeholder:text-muted/50 resize-y"
              />
              <p className="mt-1 text-[10px] text-muted/70">
                These notes are included in the evidence packet and cannot be edited after sign-off.
              </p>
            </div>

            {error && (
              <div className="text-xs text-danger bg-danger/5 border border-danger/20 rounded-lg p-3">
                {error}
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 p-4 border-t border-border/40 bg-bg/40">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="px-4 py-2 rounded-lg text-sm text-muted hover:bg-muted/10 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || !notes.trim()}
              className="flex items-center gap-1.5 px-5 py-2 rounded-lg bg-accent text-white text-sm font-bold shadow-glow disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? <Loader2 size={14} className="animate-spin" /> : <ShieldCheck size={14} />}
              {submitting ? 'Signing off…' : 'Confirm sign-off'}
            </button>
          </div>
        </motion.form>
      </motion.div>
    </AnimatePresence>
  );
}
