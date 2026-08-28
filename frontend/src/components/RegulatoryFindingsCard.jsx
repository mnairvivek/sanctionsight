import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ShieldAlert, ChevronDown, ExternalLink } from 'lucide-react';
import { ExcerptBlock } from './ResultsTable';

const RISK_BADGE = {
  HIGH: 'bg-danger/10 text-danger',
  MEDIUM: 'bg-warning/10 text-warning',
  LOW: 'bg-success/10 text-success',
  MINIMAL: 'bg-muted/10 text-muted',
  UNKNOWN: 'bg-muted/5 text-muted/50',
};

function RegulatoryRow({ result, highlightRegex }) {
  const [expanded, setExpanded] = useState(false);
  const riskLevel = result.risk_level || 'UNKNOWN';
  return (
    <div className="border-b border-border/30 last:border-b-0">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full text-left px-4 py-3 hover:bg-bg/50 transition-colors flex items-center gap-3"
      >
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-primary truncate">
            {result.original_title || result.url}
          </div>
          <div className="text-[10px] text-muted/60 font-mono truncate">{result.url}</div>
        </div>
        <span
          className={`px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${
            RISK_BADGE[riskLevel] || RISK_BADGE.UNKNOWN
          }`}
        >
          {riskLevel}
        </span>
        <span className="text-[10px] font-mono text-muted">{Math.round(result.confidence || 0)}%</span>
        <ChevronDown
          size={14}
          className={`text-muted transition-transform flex-shrink-0 ${expanded ? 'rotate-180' : ''}`}
        />
      </button>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 pt-1 space-y-2">
              <a
                href={result.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
              >
                <ExternalLink size={12} /> Open URL
              </a>
              {result.original_snippet && (
                <div className="text-xs text-primary/70 bg-bg rounded-lg p-3">
                  <span className="text-[10px] font-bold text-muted uppercase tracking-wider block mb-1">
                    Search snippet
                  </span>
                  {result.original_snippet}
                </div>
              )}
              {result.relevant_excerpts?.length > 0 && (
                <div className="space-y-2">
                  {result.relevant_excerpts.map((ex, j) => (
                    <ExcerptBlock key={j} excerpt={ex} highlightRegex={highlightRegex} />
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function RegulatoryFindingsCard({ report, highlightRegex }) {
  if (!report || !report.analyzed_results?.length) return null;
  const results = report.analyzed_results;

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.1 }}
      className="rounded-2xl border-l-4 border-danger bg-danger/5 mb-6 overflow-hidden"
    >
      <div className="p-5 pb-3">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-danger/10 text-danger">
            <ShieldAlert size={20} />
          </div>
          <div>
            <div className="text-sm font-bold text-primary">
              Regulatory / OFAC Findings — {results.length} result{results.length !== 1 ? 's' : ''}
            </div>
            <div className="text-xs text-muted">
              These findings came from sanctions-term queries that don't reference a specific country.
            </div>
          </div>
        </div>
      </div>

      <div className="bg-surface/50 border-t border-border/30">
        {results.map((r, i) => (
          <RegulatoryRow key={`${r.url}-${i}`} result={r} highlightRegex={highlightRegex} />
        ))}
      </div>
    </motion.div>
  );
}
