import React, { useState } from 'react';
import {
  Clock, AlertCircle, XCircle, UserCheck, Flag, Loader2, ChevronDown,
} from 'lucide-react';
import { useAuth } from '../auth';

const STATUS_BADGE = {
  pending:         { label: 'Pending',      icon: Clock,       className: 'bg-muted/15 text-muted border-muted/30' },
  in_review:       { label: 'In review',    icon: AlertCircle, className: 'bg-warning/15 text-warning border-warning/30' },
  cleared_fp:      { label: 'Cleared FP',   icon: XCircle,     className: 'bg-success/15 text-success border-success/30' },
  confirmed_match: { label: 'Confirmed',    icon: UserCheck,   className: 'bg-danger/15 text-danger border-danger/30' },
  escalated:       { label: 'Escalated',    icon: Flag,        className: 'bg-danger/15 text-danger border-danger/30' },
};

export function FindingStatusBadge({ status }) {
  const cfg = STATUS_BADGE[status] || STATUS_BADGE.pending;
  const Icon = cfg.icon;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider border ${cfg.className}`}>
      <Icon size={10} /> {cfg.label}
    </span>
  );
}

export default function FindingControls({ finding, onChanged }) {
  const { authedFetch, isReviewer, user } = useAuth();
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notes, setNotes] = useState(finding.state?.notes_md || '');
  const [assign, setAssign] = useState(finding.state?.assigned_analyst_id || '');

  const currentStatus = finding.state?.status || 'pending';

  const postStateChange = async (toStatus) => {
    if (!notes.trim()) {
      setError('A rationale note is required before recording this disposition.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await authedFetch(`/api/findings/${finding.finding_id}/state`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          to_status: toStatus,
          reason: notes.trim(),
          assign_to_email: assign.trim() || null,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      await onChanged?.();
    } catch (err) {
      setError(err.message || 'Update failed');
    } finally {
      setBusy(false);
    }
  };

  const clearAsFp = async () => {
    if (!notes.trim()) {
      setError('Record a rationale before clearing as false positive.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await authedFetch(`/api/findings/${finding.finding_id}/fp-override`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: notes.trim(), notes_md: notes.trim() }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      await onChanged?.();
    } catch (err) {
      setError(err.message || 'FP override failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-bg/60 border border-border/40 rounded-lg p-3 text-xs">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <FindingStatusBadge status={currentStatus} />
          <span className="text-muted">Finding #{finding.finding_id}</span>
          {finding.state?.updated_by && (
            <span className="text-[10px] text-muted/70">by {finding.state.updated_by}</span>
          )}
          {finding.state?.assigned_analyst_id && (
            <span className="text-[10px] text-accent">→ {finding.state.assigned_analyst_id}</span>
          )}
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-1 text-muted hover:text-primary text-[10px] font-bold uppercase tracking-wider"
        >
          {expanded ? 'Hide' : 'Disposition'}
          <ChevronDown size={12} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
        </button>
      </div>

      {expanded && (
        <div className="mt-3 space-y-2.5 pt-3 border-t border-border/40">
          <div>
            <label className="block text-[10px] font-bold text-muted uppercase tracking-wider mb-1">
              Notes
            </label>
            <textarea
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Record your rationale…"
              className="w-full px-2 py-1.5 rounded bg-surface border border-border/50 focus:border-accent/40 focus:ring-0 text-xs text-primary placeholder:text-muted/40 resize-y"
            />
          </div>
          <div>
            <label className="block text-[10px] font-bold text-muted uppercase tracking-wider mb-1">
              Assign to
            </label>
            <input
              type="email"
              value={assign}
              onChange={(e) => setAssign(e.target.value)}
              placeholder="analyst@firm.com"
              className="w-full px-2 py-1.5 rounded bg-surface border border-border/50 focus:border-accent/40 focus:ring-0 text-xs text-primary placeholder:text-muted/40"
            />
          </div>

          <div className="flex items-center gap-1.5 flex-wrap pt-1">
            <button
              disabled={busy || currentStatus === 'in_review'}
              onClick={() => postStateChange('in_review')}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded border border-warning/30 text-warning hover:bg-warning/5 text-[11px] font-medium disabled:opacity-40"
            >
              <AlertCircle size={11} /> In review
            </button>
            <button
              disabled={busy || currentStatus === 'cleared_fp'}
              onClick={clearAsFp}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded border border-success/30 text-success hover:bg-success/5 text-[11px] font-medium disabled:opacity-40"
            >
              <XCircle size={11} /> Clear FP
            </button>
            {isReviewer && (
              <>
                <button
                  disabled={busy || currentStatus === 'confirmed_match'}
                  onClick={() => postStateChange('confirmed_match')}
                  className="flex items-center gap-1 px-2.5 py-1.5 rounded border border-danger/30 text-danger hover:bg-danger/5 text-[11px] font-medium disabled:opacity-40"
                >
                  <UserCheck size={11} /> Confirm match
                </button>
                <button
                  disabled={busy || currentStatus === 'escalated'}
                  onClick={() => postStateChange('escalated')}
                  className="flex items-center gap-1 px-2.5 py-1.5 rounded border border-danger/30 text-danger hover:bg-danger/5 text-[11px] font-medium disabled:opacity-40"
                >
                  <Flag size={11} /> Escalate
                </button>
              </>
            )}
            {!isReviewer && (
              <span className="text-[10px] text-muted italic">
                Only reviewers can confirm or escalate. You are {user?.role}.
              </span>
            )}
            {busy && <Loader2 size={12} className="animate-spin text-muted" />}
          </div>

          {error && (
            <div className="text-[11px] text-danger bg-danger/5 border border-danger/20 rounded p-2">
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
