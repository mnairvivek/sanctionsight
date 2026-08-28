import React, { useEffect, useState, useCallback } from 'react';
import { motion } from 'framer-motion';
import {
  ShieldCheck, RotateCcw, Package, Loader2, AlertCircle, Clock,
  UserCheck, XCircle, Flag, ScrollText,
} from 'lucide-react';
import { useAuth } from '../auth';
import SignOffModal from './SignOffModal';
import AuditTrailDrawer from './AuditTrailDrawer';

const STATUS_STYLE = {
  draft: { label: 'Draft', className: 'bg-muted/15 text-muted border-muted/30' },
  in_review: { label: 'In Review', className: 'bg-warning/15 text-warning border-warning/30' },
  signed_off: { label: 'Signed Off', className: 'bg-success/15 text-success border-success/30' },
  reopened: { label: 'Reopened', className: 'bg-danger/15 text-danger border-danger/30' },
};

const COUNT_STYLE = {
  pending:         { label: 'Pending',      icon: Clock,     color: 'text-muted' },
  in_review:       { label: 'In review',    icon: AlertCircle, color: 'text-warning' },
  cleared_fp:      { label: 'Cleared FP',   icon: XCircle,   color: 'text-success' },
  confirmed_match: { label: 'Confirmed',    icon: UserCheck, color: 'text-danger' },
  escalated:       { label: 'Escalated',    icon: Flag,      color: 'text-danger' },
};

export default function CaseHeader({ jobId, onChanged }) {
  const { authedFetch, isReviewer, isAdmin } = useAuth();
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [signOffOpen, setSignOffOpen] = useState(false);
  const [auditOpen, setAuditOpen] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);

  const fetchOverview = useCallback(async () => {
    if (!jobId) return;
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/hitl-overview`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setOverview(data);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to load workflow state');
    } finally {
      setLoading(false);
    }
  }, [authedFetch, jobId]);

  useEffect(() => { fetchOverview(); }, [fetchOverview]);

  const handleReopen = async () => {
    const reason = window.prompt('Reason for reopening this case?');
    if (!reason) return;
    setActionBusy(true);
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/reopen`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      await fetchOverview();
      onChanged?.();
    } catch (err) {
      alert(`Reopen failed: ${err.message}`);
    } finally {
      setActionBusy(false);
    }
  };

  const handleDownloadPacket = async () => {
    setActionBusy(true);
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/evidence-packet.zip`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `evidence-${jobId}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(`Evidence packet download failed: ${err.message}`);
    } finally {
      setActionBusy(false);
    }
  };

  const handleSignOffSuccess = async () => {
    setSignOffOpen(false);
    await fetchOverview();
    onChanged?.();
  };

  if (!jobId) return null;
  if (loading) {
    return (
      <div className="bg-surface rounded-2xl border border-border/50 p-4 mb-6 flex items-center gap-2 text-sm text-muted">
        <Loader2 size={14} className="animate-spin" /> Loading case workflow…
      </div>
    );
  }
  if (error || !overview) {
    return (
      <div className="bg-surface rounded-2xl border border-danger/30 p-4 mb-6 text-sm text-danger">
        Workflow state unavailable: {error || 'unknown error'}
      </div>
    );
  }

  const statusStyle = STATUS_STYLE[overview.workflow_status] || STATUS_STYLE.draft;
  const counts = overview.counts || {};
  const pending = (counts.pending || 0) + (counts.in_review || 0);
  const canSignOff = (isReviewer || isAdmin) &&
    (overview.workflow_status === 'draft' || overview.workflow_status === 'in_review' || overview.workflow_status === 'reopened') &&
    overview.total_findings > 0 &&
    pending === 0;
  const canReopen = isAdmin && overview.workflow_status === 'signed_off';

  return (
    <>
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-surface rounded-2xl border border-border/50 p-5 mb-6"
      >
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 mb-2 flex-wrap">
              <span className={`px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider border ${statusStyle.className}`}>
                {statusStyle.label}
              </span>
              <span className="text-[11px] font-mono text-muted">Case {jobId}</span>
              {overview.signed_off_by && (
                <span className="text-[11px] text-muted">
                  signed off by {overview.signed_off_by}
                  {overview.signed_off_at ? ` · ${new Date(overview.signed_off_at).toLocaleString()}` : ''}
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-x-5 gap-y-2 mt-1">
              {Object.entries(COUNT_STYLE).map(([key, style]) => {
                const Icon = style.icon;
                const value = counts[key] || 0;
                return (
                  <div key={key} className="flex items-center gap-1.5 text-xs">
                    <Icon size={13} className={style.color} />
                    <span className="font-mono text-primary">{value}</span>
                    <span className="text-muted">{style.label}</span>
                  </div>
                );
              })}
              <div className="flex items-center gap-1.5 text-xs text-muted/70">
                <span className="font-mono">{overview.total_findings}</span>
                <span>total findings</span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => setAuditOpen(true)}
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-border/50 hover:border-accent/40 hover:bg-accent/5 text-xs font-medium text-primary transition"
              title="View audit trail"
            >
              <ScrollText size={14} /> Audit trail
            </button>
            {(isReviewer || isAdmin) && (
              <button
                onClick={handleDownloadPacket}
                disabled={actionBusy}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-border/50 hover:border-accent/40 hover:bg-accent/5 text-xs font-medium text-primary transition disabled:opacity-50"
                title="Download evidence packet ZIP"
              >
                <Package size={14} /> Evidence packet
              </button>
            )}
            {canReopen && (
              <button
                onClick={handleReopen}
                disabled={actionBusy}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-danger/30 text-danger hover:bg-danger/5 text-xs font-medium transition disabled:opacity-50"
              >
                <RotateCcw size={14} /> Reopen
              </button>
            )}
            {(isReviewer || isAdmin) && overview.workflow_status !== 'signed_off' && (
              <button
                onClick={() => setSignOffOpen(true)}
                disabled={!canSignOff || actionBusy}
                title={
                  !canSignOff
                    ? pending > 0
                      ? `Disposition ${pending} open finding${pending === 1 ? '' : 's'} before sign-off`
                      : overview.total_findings === 0
                      ? 'No findings to sign off'
                      : 'Sign-off unavailable'
                    : 'Sign off on this case'
                }
                className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-accent text-white text-xs font-bold shadow-glow disabled:opacity-50 disabled:cursor-not-allowed transition"
              >
                <ShieldCheck size={14} /> Sign off
              </button>
            )}
          </div>
        </div>

        {pending > 0 && (isReviewer || isAdmin) && overview.workflow_status !== 'signed_off' && (
          <div className="mt-3 text-[11px] text-warning flex items-center gap-1.5">
            <AlertCircle size={12} />
            {pending} finding{pending === 1 ? '' : 's'} still pending disposition — sign-off is blocked until each one is cleared, confirmed, or escalated.
          </div>
        )}
      </motion.div>

      {signOffOpen && (
        <SignOffModal
          jobId={jobId}
          onClose={() => setSignOffOpen(false)}
          onSuccess={handleSignOffSuccess}
        />
      )}

      {auditOpen && (
        <AuditTrailDrawer jobId={jobId} onClose={() => setAuditOpen(false)} />
      )}
    </>
  );
}
