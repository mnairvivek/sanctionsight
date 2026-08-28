import React, { useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, ExternalLink, FileText, Hash } from 'lucide-react';

/**
 * buildExcerptIndex(data)
 *
 * Walk the result payload and return a map of excerpt_id → {
 *   text, trigger_sentence, url, country, source_id, risk_type, risk_score
 * }. Built from reports[].analyzed_results[].relevant_excerpts and
 * name_co_results[].relevant_excerpts and regulatory_findings. This lets
 * the popover look up any citation coming from the investigator brief.
 */
export function buildExcerptIndex(data) {
  const idx = {};
  const walk = (bucket) => {
    for (const ar of bucket || []) {
      const url = ar.url || '';
      const country = ar.country || bucket.country || '';
      for (const ex of ar.relevant_excerpts || []) {
        if (ex.excerpt_id) {
          idx[ex.excerpt_id] = { ...ex, url, country };
        }
      }
    }
  };
  for (const rep of data?.reports || []) {
    walk((rep.analyzed_results || []).map((ar) => ({ ...ar, country: rep.country })));
  }
  walk(data?.name_co_results || []);
  if (data?.regulatory_findings) {
    walk((data.regulatory_findings.analyzed_results || []).map((ar) => ({
      ...ar,
      country: data.regulatory_findings.country,
    })));
  }
  return idx;
}

export default function CitationPopover({ citation, excerptIndex, onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!citation) return null;
  const excerpt = excerptIndex?.[citation.excerpt_id];

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-[60] bg-black/40 backdrop-blur-sm flex items-center justify-center px-4"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.96, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.98, opacity: 0 }}
          onClick={(e) => e.stopPropagation()}
          className="bg-surface rounded-2xl shadow-lifted border border-border/50 w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col"
        >
          <div className="flex items-start justify-between gap-3 p-5 border-b border-border/40">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-xs font-mono text-muted">
                <Hash size={12} />
                {citation.excerpt_id}
              </div>
              <h3 className="font-display font-bold text-lg text-primary tracking-tight mt-1">
                Cited evidence
              </h3>
            </div>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-muted/10 text-muted">
              <X size={16} />
            </button>
          </div>

          <div className="p-5 overflow-y-auto space-y-4">
            {!excerpt ? (
              <div className="text-sm text-muted italic flex items-start gap-2">
                <FileText size={14} className="mt-0.5 flex-shrink-0" />
                <span>
                  Cited excerpt {citation.excerpt_id} could not be located in the current payload.
                  This usually means the evidence was dropped by verification or the payload is stale;
                  check the evidence packet ZIP for the full audit-chain record.
                </span>
              </div>
            ) : (
              <>
                <div className="text-[11px] font-mono text-muted/70 flex items-center gap-2 flex-wrap">
                  {excerpt.country && <span className="px-2 py-0.5 bg-muted/10 rounded">{excerpt.country}</span>}
                  {excerpt.risk_type && <span className="px-2 py-0.5 bg-muted/10 rounded">{excerpt.risk_type}</span>}
                  {typeof excerpt.risk_score === 'number' && (
                    <span className="px-2 py-0.5 bg-muted/10 rounded">risk {Math.round(excerpt.risk_score)}</span>
                  )}
                </div>

                {excerpt.trigger_sentence && (
                  <div>
                    <div className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1">
                      Trigger sentence
                    </div>
                    <p className="text-sm text-primary leading-relaxed bg-warning/10 border border-warning/30 rounded-lg p-3">
                      {excerpt.trigger_sentence}
                    </p>
                  </div>
                )}

                <div>
                  <div className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1">
                    Context window
                  </div>
                  <p className="text-sm text-primary/90 leading-relaxed whitespace-pre-wrap">
                    {excerpt.text || '—'}
                  </p>
                </div>

                {excerpt.url && (
                  <a
                    href={excerpt.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-xs text-accent hover:underline break-all"
                  >
                    <ExternalLink size={12} />
                    {excerpt.url}
                  </a>
                )}
              </>
            )}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
