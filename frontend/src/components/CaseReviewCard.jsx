import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import {
  ThumbsUp, ThumbsDown, Loader2, PenLine, CheckCircle2, Info,
} from 'lucide-react';
import { useAuth } from '../auth';

const DISAGREE_MIN_LEN = 10;

export default function CaseReviewCard({ jobId, caseReview, onChanged }) {
  const { isAuthed, authedFetch } = useAuth();
  const [state, setState] = useState(caseReview || null);
  const [summary, setSummary] = useState(caseReview?.analyst_case_summary || '');
  const [summarySaving, setSummarySaving] = useState(false);
  const [summaryError, setSummaryError] = useState(null);
  const [summarySaved, setSummarySaved] = useState(false);
  const [disagreeOpen, setDisagreeOpen] = useState(false);
  const [reason, setReason] = useState(caseReview?.analyst_case_disagree_reason || '');
  const [voteSubmitting, setVoteSubmitting] = useState(false);
  const [voteError, setVoteError] = useState(null);

  useEffect(() => {
    setState(caseReview || null);
    setSummary(caseReview?.analyst_case_summary || '');
    setReason(caseReview?.analyst_case_disagree_reason || '');
  }, [caseReview]);

  if (!isAuthed || !jobId) {
    return null;
  }

  const agrees = state?.analyst_agrees_with_brief;
  const disagreeReason = state?.analyst_case_disagree_reason;
  const updatedBy = state?.analyst_case_updated_by;

  const saveSummary = async () => {
    const trimmed = summary.trim();
    if (!trimmed) {
      setSummaryError('Summary cannot be empty.');
      return;
    }
    setSummarySaving(true);
    setSummaryError(null);
    setSummarySaved(false);
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/case-summary`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ summary: trimmed }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || 'Failed to save summary');
      }
      const data = await res.json();
      setState((s) => ({ ...(s || {}), analyst_case_summary: data.analyst_case_summary }));
      setSummarySaved(true);
      onChanged?.();
      setTimeout(() => setSummarySaved(false), 2500);
    } catch (e) {
      setSummaryError(e.message);
    } finally {
      setSummarySaving(false);
    }
  };

  const submitVote = async (vote, reasonText) => {
    setVoteSubmitting(true);
    setVoteError(null);
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/case-verdict`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agrees: vote, reason: reasonText || null }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || 'Failed to record verdict');
      }
      const data = await res.json();
      setState((s) => ({
        ...(s || {}),
        analyst_agrees_with_brief: data.analyst_agrees_with_brief,
        analyst_case_disagree_reason: data.analyst_case_disagree_reason,
      }));
      setDisagreeOpen(false);
      onChanged?.();
    } catch (e) {
      setVoteError(e.message);
    } finally {
      setVoteSubmitting(false);
    }
  };

  const onAgree = () => submitVote(true, null);
  const onDisagreeOpen = () => { setDisagreeOpen(true); setVoteError(null); };
  const onSubmitDisagree = (e) => {
    e.preventDefault();
    if (reason.trim().length < DISAGREE_MIN_LEN) {
      setVoteError(`Reason must be at least ${DISAGREE_MIN_LEN} characters.`);
      return;
    }
    submitVote(false, reason.trim());
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.15 }}
      className="rounded-2xl border border-border/60 bg-surface shadow-card p-6 mb-6"
    >
      <div className="flex items-center gap-2 mb-3">
        <PenLine size={16} className="text-accent" />
        <h3 className="font-display font-bold text-base text-primary">Analyst review</h3>
      </div>

      {/* Case summary */}
      <div className="space-y-2 mb-5">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-bold text-muted uppercase tracking-wider">
            Case-wide summary (your own notes)
          </span>
          {summarySaved && (
            <span className="inline-flex items-center gap-1 text-[11px] text-success">
              <CheckCircle2 size={12} /> Saved
            </span>
          )}
        </div>
        <textarea
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          rows={4}
          placeholder="Write your own case-level read: what matters, what was weak evidence, what you'd want a reviewer to look at first."
          className="w-full rounded-xl border border-border bg-bg px-3 py-2 text-sm text-primary leading-relaxed placeholder:text-muted/50 focus:outline-none focus:ring-2 focus:ring-accent/20"
        />
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={saveSummary}
            disabled={summarySaving || !summary.trim()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium bg-accent/10 text-accent border-accent/30 hover:bg-accent/20 disabled:opacity-50"
          >
            {summarySaving ? <Loader2 size={12} className="animate-spin" /> : <PenLine size={12} />}
            Save summary
          </button>
          {summaryError && <span className="text-[11px] text-danger">{summaryError}</span>}
          {state?.analyst_case_updated_at && (
            <span className="text-[10px] text-muted font-mono">
              last edit {new Date(state.analyst_case_updated_at).toLocaleString()}
              {updatedBy ? ` · ${updatedBy}` : ''}
            </span>
          )}
        </div>
      </div>

      {/* Case-level agree/disagree */}
      <div className="space-y-2 pt-4 border-t border-border/40">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-bold text-muted uppercase tracking-wider">
            Agree with the aggregate brief above?
          </span>
          {agrees === true && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-bold uppercase bg-success/10 text-success border-success/30">
              <ThumbsUp size={10} /> Agreed
            </span>
          )}
          {agrees === false && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-bold uppercase bg-warning/10 text-warning border-warning/30">
              <ThumbsDown size={10} /> Disagreed
            </span>
          )}
          {agrees == null && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-bold uppercase bg-muted/10 text-muted border-muted/20">
              <Info size={10} /> Not yet reviewed
            </span>
          )}
        </div>

        {agrees === false && disagreeReason && (
          <div className="text-[11px] text-primary/70 border-l-2 border-warning/40 pl-2">
            <div className="text-[10px] font-bold text-warning uppercase tracking-wider mb-0.5">
              Your recorded disagreement
            </div>
            {disagreeReason}
          </div>
        )}

        <div className="flex items-center gap-2 pt-1">
          <button
            type="button"
            onClick={onAgree}
            disabled={voteSubmitting}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors disabled:opacity-50 ${
              agrees === true
                ? 'bg-success/15 text-success border-success/40'
                : 'bg-bg text-primary/80 border-border hover:bg-success/10 hover:border-success/30 hover:text-success'
            }`}
          >
            <ThumbsUp size={12} /> Agree
          </button>
          <button
            type="button"
            onClick={onDisagreeOpen}
            disabled={voteSubmitting}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors disabled:opacity-50 ${
              agrees === false
                ? 'bg-warning/15 text-warning border-warning/40'
                : 'bg-bg text-primary/80 border-border hover:bg-warning/10 hover:border-warning/30 hover:text-warning'
            }`}
          >
            <ThumbsDown size={12} /> Disagree
          </button>
          {voteError && <span className="text-[11px] text-danger">{voteError}</span>}
        </div>

        {disagreeOpen && (
          <form onSubmit={onSubmitDisagree} className="space-y-2 pt-2">
            <label className="block text-[10px] font-bold text-muted uppercase tracking-wider">
              Why do you disagree? (min {DISAGREE_MIN_LEN} chars)
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={3}
              placeholder="e.g. The brief overweights the Tehran mention; it refers to a trade-show stand, not a business presence."
              className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-xs text-primary placeholder:text-muted/50 focus:outline-none focus:ring-2 focus:ring-accent/20"
            />
            <div className="flex items-center gap-2">
              <button
                type="submit"
                disabled={voteSubmitting || reason.trim().length < DISAGREE_MIN_LEN}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium bg-warning/15 text-warning border-warning/40 disabled:opacity-50"
              >
                {voteSubmitting ? <Loader2 size={12} className="animate-spin" /> : <ThumbsDown size={12} />}
                Submit disagreement
              </button>
              <button
                type="button"
                onClick={() => { setDisagreeOpen(false); setVoteError(null); }}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium bg-bg text-muted border-border hover:text-primary"
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </motion.div>
  );
}
