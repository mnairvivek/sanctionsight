import React, { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, ThumbsUp, ThumbsDown, Info, Loader2 } from 'lucide-react';
import { useAuth } from '../auth';

const DISAGREE_MIN_LEN = 10;

export default function LinkVerdictBlock({ jobId, urlHash, verdict, onChanged }) {
  const { isAuthed, authedFetch } = useAuth();
  const [local, setLocal] = useState(verdict || null);
  const [disagreeOpen, setDisagreeOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => { setLocal(verdict || null); }, [verdict]);

  if (!local && !verdict) {
    // Job predates per-link verdicts, or the LLM was unavailable and the
    // row was never stamped. Show a neutral placeholder so the analyst
    // knows what the empty space means.
    return (
      <div className="rounded-xl border border-border/40 bg-bg/60 px-3 py-2 text-[11px] text-muted flex items-start gap-2">
        <Info size={13} className="mt-0.5 flex-shrink-0" />
        <span>No per-link LLM read available for this URL.</span>
      </div>
    );
  }

  const v = local || verdict;
  const concern = v?.llm_concern;
  const reasoning = v?.llm_reasoning;
  const llmError = v?.llm_error;
  const analystAgrees = v?.analyst_agrees;
  const analystReason = v?.analyst_disagree_reason;
  const analystBy = v?.analyst_updated_by;

  const badge = (() => {
    if (llmError) {
      return {
        label: 'LLM unavailable',
        icon: Info,
        classes: 'bg-muted/10 text-muted border-muted/20',
      };
    }
    if (concern === true) {
      return {
        label: 'Possible concern',
        icon: AlertTriangle,
        classes: 'bg-danger/10 text-danger border-danger/30',
      };
    }
    if (concern === false) {
      return {
        label: 'No concern',
        icon: CheckCircle2,
        classes: 'bg-success/10 text-success border-success/30',
      };
    }
    return {
      label: 'Awaiting LLM',
      icon: Loader2,
      classes: 'bg-muted/10 text-muted border-muted/20',
    };
  })();

  const BadgeIcon = badge.icon;

  const submitVerdict = async (agrees, reasonText) => {
    if (!isAuthed || !jobId || !urlHash) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/links/${urlHash}/verdict`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agrees, reason: reasonText || null }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || 'Failed to record verdict');
      }
      const data = await res.json();
      setLocal({
        ...(v || {}),
        analyst_agrees: data.analyst_agrees,
        analyst_disagree_reason: data.analyst_disagree_reason,
      });
      setDisagreeOpen(false);
      setReason('');
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const onAgree = () => submitVerdict(true, null);
  const onDisagreeOpen = () => { setReason(analystReason || ''); setDisagreeOpen(true); setError(null); };
  const onSubmitDisagree = (e) => {
    e.preventDefault();
    if (reason.trim().length < DISAGREE_MIN_LEN) {
      setError(`Reason must be at least ${DISAGREE_MIN_LEN} characters.`);
      return;
    }
    submitVerdict(false, reason.trim());
  };

  return (
    <div className="rounded-xl border border-border/40 bg-bg/60 p-3 space-y-2">
      <div className="flex items-start gap-2 flex-wrap">
        <span
          className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-full border text-[10px] font-bold uppercase tracking-wider ${badge.classes}`}
        >
          <BadgeIcon size={11} />
          LLM: {badge.label}
        </span>
        {analystAgrees === true && (
          <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full border text-[10px] font-bold uppercase tracking-wider bg-success/10 text-success border-success/30">
            <ThumbsUp size={11} /> Analyst agreed
          </span>
        )}
        {analystAgrees === false && (
          <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full border text-[10px] font-bold uppercase tracking-wider bg-warning/10 text-warning border-warning/30">
            <ThumbsDown size={11} /> Analyst disagreed
          </span>
        )}
      </div>

      {reasoning && (
        <p className="text-xs text-primary/80 leading-relaxed">{reasoning}</p>
      )}
      {llmError && !reasoning && (
        <p className="text-[11px] text-muted italic">Model did not return a verdict for this link ({llmError}).</p>
      )}

      {analystAgrees === false && analystReason && (
        <div className="text-[11px] text-primary/70 border-l-2 border-warning/40 pl-2">
          <div className="text-[10px] font-bold text-warning uppercase tracking-wider mb-0.5">
            Analyst disagreement reason
          </div>
          {analystReason}
          {analystBy ? <div className="mt-1 text-[10px] text-muted">— {analystBy}</div> : null}
        </div>
      )}

      {isAuthed && jobId && urlHash && (
        <div className="flex items-center gap-2 pt-1">
          <button
            type="button"
            onClick={onAgree}
            disabled={submitting}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[11px] font-medium transition-colors disabled:opacity-50 ${
              analystAgrees === true
                ? 'bg-success/15 text-success border-success/40'
                : 'bg-bg text-primary/80 border-border hover:bg-success/10 hover:border-success/30 hover:text-success'
            }`}
          >
            <ThumbsUp size={12} /> Agree
          </button>
          <button
            type="button"
            onClick={onDisagreeOpen}
            disabled={submitting}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[11px] font-medium transition-colors disabled:opacity-50 ${
              analystAgrees === false
                ? 'bg-warning/15 text-warning border-warning/40'
                : 'bg-bg text-primary/80 border-border hover:bg-warning/10 hover:border-warning/30 hover:text-warning'
            }`}
          >
            <ThumbsDown size={12} /> Disagree
          </button>
          {error && <span className="text-[11px] text-danger">{error}</span>}
        </div>
      )}

      {disagreeOpen && (
        <form onSubmit={onSubmitDisagree} className="space-y-2 pt-2 border-t border-border/40">
          <label className="block text-[10px] font-bold text-muted uppercase tracking-wider">
            Reason for disagreeing (min {DISAGREE_MIN_LEN} chars)
          </label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            placeholder="e.g. Page refers to a namesake company in a different industry."
            className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-xs text-primary placeholder:text-muted/50 focus:outline-none focus:ring-2 focus:ring-accent/20"
          />
          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={submitting || reason.trim().length < DISAGREE_MIN_LEN}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[11px] font-medium bg-warning/15 text-warning border-warning/40 disabled:opacity-50"
            >
              {submitting ? <Loader2 size={12} className="animate-spin" /> : <ThumbsDown size={12} />}
              Submit disagreement
            </button>
            <button
              type="button"
              onClick={() => { setDisagreeOpen(false); setReason(''); setError(null); }}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[11px] font-medium bg-bg text-muted border-border hover:text-primary"
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
