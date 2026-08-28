import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { AlertTriangle, EyeOff, ChevronDown, ExternalLink } from 'lucide-react';

function extractionTypeLabel(type) {
  switch ((type || '').toUpperCase()) {
    case 'SNIPPET_FALLBACK':
      return 'Google snippet only';
    case 'DOCUMENT':
      return 'Binary document (not readable)';
    case 'PDF':
      return 'PDF extraction failed';
    case 'ERROR':
      return 'Page fetch failed';
    default:
      return type || 'Unknown';
  }
}

function extractionTypeColor(type) {
  switch ((type || '').toUpperCase()) {
    case 'SNIPPET_FALLBACK':
      return 'bg-warning/10 text-warning';
    case 'DOCUMENT':
      return 'bg-muted/10 text-muted';
    case 'PDF':
      return 'bg-danger/10 text-danger';
    case 'ERROR':
      return 'bg-danger/10 text-danger';
    default:
      return 'bg-muted/10 text-muted';
  }
}

function ReviewRow({ item }) {
  return (
    <div className="px-4 py-3 border-b border-border/40 last:border-b-0 hover:bg-bg/50 transition-colors">
      <div className="flex items-start justify-between gap-3 mb-1.5">
        <a
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-accent hover:underline truncate flex items-center gap-1.5 min-w-0"
          title={item.url}
        >
          <span className="truncate">{item.title || item.url}</span>
          <ExternalLink size={12} className="flex-shrink-0 opacity-60" />
        </a>
        <span
          className={`text-[10px] font-semibold px-2 py-0.5 rounded-full whitespace-nowrap ${extractionTypeColor(
            item.extraction_type,
          )}`}
        >
          {extractionTypeLabel(item.extraction_type)}
        </span>
      </div>
      {item.snippet && (
        <p className="text-xs text-muted leading-relaxed mb-1.5 line-clamp-2">{item.snippet}</p>
      )}
      <div className="flex items-center gap-2 flex-wrap text-[10px] text-muted/70">
        {item.countries && item.countries.length > 0 && (
          <span className="font-mono">{item.countries.join(' · ')}</span>
        )}
        {item.extraction_message && (
          <span className="text-muted/50">{item.extraction_message}</span>
        )}
      </div>
    </div>
  );
}

function NotAttemptedRow({ item }) {
  return (
    <div className="px-4 py-2.5 border-b border-border/40 last:border-b-0 hover:bg-bg/50 transition-colors">
      <div className="flex items-center justify-between gap-3">
        <a
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-muted hover:text-accent hover:underline truncate flex items-center gap-1.5 min-w-0"
          title={item.url}
        >
          <span className="truncate">{item.url}</span>
          <ExternalLink size={10} className="flex-shrink-0 opacity-60" />
        </a>
        <span className="text-[10px] font-mono text-muted/60 whitespace-nowrap">{item.domain}</span>
      </div>
    </div>
  );
}

function CollapsibleSection({ icon: Icon, title, count, accent, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  if (!count) return null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-surface rounded-2xl shadow-card border border-border/50 overflow-hidden mb-4"
    >
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-5 py-4 hover:bg-bg/50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className={`p-2 rounded-xl ${accent}`}>
            <Icon size={14} />
          </div>
          <div className="text-left">
            <div className="text-sm font-display font-bold text-primary">{title}</div>
            <div className="text-[10px] text-muted/70">
              {count} {count === 1 ? 'URL' : 'URLs'}
            </div>
          </div>
        </div>
        <motion.div animate={{ rotate: open ? 180 : 0 }} transition={{ duration: 0.2 }}>
          <ChevronDown size={16} className="text-muted" />
        </motion.div>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden border-t border-border/40"
          >
            <div>{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

export default function ExtractionReviewSections({ needsAnalystReview, notAttempted }) {
  const reviewItems = Array.isArray(needsAnalystReview) ? needsAnalystReview : [];
  const notAttemptedItems = Array.isArray(notAttempted) ? notAttempted : [];

  if (!reviewItems.length && !notAttemptedItems.length) return null;

  return (
    <div className="mb-8">
      <CollapsibleSection
        icon={AlertTriangle}
        title="Needs analyst review"
        count={reviewItems.length}
        accent="bg-warning/10 text-warning"
      >
        {reviewItems.map((item, idx) => (
          <ReviewRow key={`${item.url}-${idx}`} item={item} />
        ))}
      </CollapsibleSection>
      <CollapsibleSection
        icon={EyeOff}
        title="Not attempted"
        count={notAttemptedItems.length}
        accent="bg-muted/10 text-muted"
      >
        {notAttemptedItems.map((item, idx) => (
          <NotAttemptedRow key={`${item.url}-${idx}`} item={item} />
        ))}
      </CollapsibleSection>
    </div>
  );
}
