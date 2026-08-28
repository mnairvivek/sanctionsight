import React, { useCallback, useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Inbox, Loader2, ExternalLink, RefreshCw } from 'lucide-react';
import { useAuth } from '../auth';
import { FindingStatusBadge } from './FindingControls';

const RISK_COLOR = {
  HIGH: 'text-danger',
  MEDIUM: 'text-warning',
  LOW: 'text-success',
  MINIMAL: 'text-muted',
};

function riskLevelFromScore(score) {
  if (score >= 80) return 'HIGH';
  if (score >= 40) return 'MEDIUM';
  if (score >= 15) return 'LOW';
  return 'MINIMAL';
}

export default function AnalystQueue({ onClose, onOpenJob }) {
  const { authedFetch, user } = useAuth();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchQueue = useCallback(async () => {
    setLoading(true);
    try {
      const res = await authedFetch('/api/analysts/me/queue?limit=100');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setItems(data.items || []);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to load queue');
    } finally {
      setLoading(false);
    }
  }, [authedFetch]);

  useEffect(() => { fetchQueue(); }, [fetchQueue]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-[60] bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      >
        <motion.aside
          initial={{ x: '100%' }}
          animate={{ x: 0 }}
          exit={{ x: '100%' }}
          transition={{ type: 'tween', duration: 0.25 }}
          onClick={(e) => e.stopPropagation()}
          className="absolute top-0 right-0 h-full w-full max-w-xl bg-surface border-l border-border/50 shadow-lifted flex flex-col"
        >
          <div className="flex items-start justify-between gap-3 p-5 border-b border-border/40">
            <div>
              <div className="flex items-center gap-2 text-accent mb-1">
                <Inbox size={18} />
                <h3 className="font-display font-bold text-lg text-primary tracking-tight">
                  My queue
                </h3>
              </div>
              <p className="text-xs text-muted">
                Assigned to {user?.email || 'me'} · pending triage
              </p>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={fetchQueue}
                title="Refresh"
                className="p-1.5 rounded-lg hover:bg-muted/10 text-muted"
              >
                <RefreshCw size={14} />
              </button>
              <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-muted/10 text-muted">
                <X size={16} />
              </button>
            </div>
          </div>

          <div className="p-5 overflow-y-auto flex-1">
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted">
                <Loader2 size={14} className="animate-spin" /> Loading queue…
              </div>
            ) : error ? (
              <div className="text-sm text-danger bg-danger/5 border border-danger/20 rounded p-3">
                {error}
              </div>
            ) : items.length === 0 ? (
              <div className="text-sm text-muted italic">
                Queue is clear — no findings are assigned to you or pending disposition.
              </div>
            ) : (
              <>
                <div className="text-[11px] text-muted mb-3 font-mono">
                  {items.length} finding{items.length === 1 ? '' : 's'} awaiting action
                </div>
                <ul className="space-y-2">
                  {items.map((it) => {
                    const risk = riskLevelFromScore(it.risk_score || 0);
                    return (
                      <li
                        key={it.finding_id}
                        className="border border-border/40 rounded-lg p-3 hover:border-accent/30 hover:bg-accent/5 transition"
                      >
                        <div className="flex items-start justify-between gap-2 mb-1.5">
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 flex-wrap mb-1">
                              <FindingStatusBadge status={it.status} />
                              <span className={`text-[10px] font-mono font-bold ${RISK_COLOR[risk]}`}>
                                risk {Math.round(it.risk_score || 0)}
                              </span>
                              <span className="text-[10px] text-muted">
                                {it.country || '—'} · {it.risk_type || 'GENERAL'}
                              </span>
                            </div>
                            <div className="text-xs text-primary truncate">
                              {it.sentence?.slice(0, 140) || '(no sentence captured)'}
                            </div>
                            <div className="text-[10px] text-muted font-mono truncate mt-0.5">
                              {it.url}
                            </div>
                          </div>
                        </div>
                        <div className="flex items-center justify-between gap-2 text-[10px] text-muted mt-2">
                          <span className="font-mono">job {it.job_id}</span>
                          <button
                            onClick={() => onOpenJob?.(it.job_id)}
                            className="flex items-center gap-1 text-accent hover:underline font-medium"
                          >
                            Open case <ExternalLink size={10} />
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </div>
        </motion.aside>
      </motion.div>
    </AnimatePresence>
  );
}
