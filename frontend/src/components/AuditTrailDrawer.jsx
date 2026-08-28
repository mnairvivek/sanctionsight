import React, { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, ScrollText, Loader2, Hash, User, Clock } from 'lucide-react';
import { useAuth } from '../auth';

function EventRow({ event }) {
  const payload = event.payload || {};
  const when = event.occurred_at ? new Date(event.occurred_at).toLocaleString() : '—';
  const payloadStr = JSON.stringify(payload, null, 2);

  return (
    <li className="border-l-2 border-accent/30 pl-3 py-2">
      <div className="flex items-center gap-2 text-[11px] flex-wrap">
        <span className="font-mono text-accent font-bold">#{event.seq}</span>
        <span className="px-1.5 py-0.5 rounded bg-muted/10 text-[10px] font-mono text-primary">
          {event.event_type}
        </span>
        <span className="flex items-center gap-1 text-muted">
          <Clock size={10} /> {when}
        </span>
        <span className="flex items-center gap-1 text-muted">
          <User size={10} /> {event.actor || 'system'}
        </span>
      </div>
      {Object.keys(payload).length > 0 && (
        <pre className="mt-1.5 text-[10px] font-mono text-primary/70 bg-bg rounded p-2 overflow-x-auto max-h-40">
          {payloadStr}
        </pre>
      )}
      <div className="flex items-center gap-1 mt-1 text-[9px] font-mono text-muted/50">
        <Hash size={8} /> {(event.hash || '').slice(0, 16)}…
      </div>
    </li>
  );
}

export default function AuditTrailDrawer({ jobId, onClose }) {
  const { authedFetch } = useAuth();
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authedFetch(`/api/jobs/${jobId}/audit-events`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) setEvents(data.events || []);
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to load audit trail');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [authedFetch, jobId]);

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
                <ScrollText size={18} />
                <h3 className="font-display font-bold text-lg text-primary tracking-tight">
                  Audit trail
                </h3>
              </div>
              <p className="text-xs text-muted font-mono">case {jobId}</p>
            </div>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-muted/10 text-muted">
              <X size={16} />
            </button>
          </div>

          <div className="p-5 overflow-y-auto flex-1">
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted">
                <Loader2 size={14} className="animate-spin" /> Loading events…
              </div>
            ) : error ? (
              <div className="text-sm text-danger bg-danger/5 border border-danger/20 rounded p-3">
                {error}
              </div>
            ) : events.length === 0 ? (
              <div className="text-sm text-muted italic">No audit events recorded yet.</div>
            ) : (
              <>
                <div className="text-[11px] text-muted mb-3 font-mono">
                  {events.length} event{events.length === 1 ? '' : 's'} · hash-chained
                </div>
                <ul className="space-y-2">
                  {events.map((e) => <EventRow key={e.seq} event={e} />)}
                </ul>
              </>
            )}
          </div>
        </motion.aside>
      </motion.div>
    </AnimatePresence>
  );
}
