import React, { useState, useMemo, useEffect, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Search, ChevronDown, ChevronUp, ExternalLink, FileText, ArrowUpDown,
  Filter, X, Globe, AlertTriangle, CheckCircle2, ThumbsUp, ThumbsDown, Info,
} from 'lucide-react';
import { useAuth } from '../auth';
import FindingControls, { FindingStatusBadge } from './FindingControls';
import LinkVerdictBlock from './LinkVerdictBlock';

const RISK_ORDER = { HIGH: 0, MEDIUM: 1, LOW: 2, MINIMAL: 3, NONE: 4, UNKNOWN: 5 };

const RISK_BADGE = {
  HIGH: 'bg-danger/10 text-danger',
  MEDIUM: 'bg-warning/10 text-warning',
  LOW: 'bg-success/10 text-success',
  MINIMAL: 'bg-muted/10 text-muted',
  UNKNOWN: 'bg-muted/5 text-muted/50',
  NONE: 'bg-muted/5 text-muted/50',
};

function RiskBadge({ level }) {
  return (
    <span className={`inline-block px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${RISK_BADGE[level] || RISK_BADGE.UNKNOWN}`}>
      {level}
    </span>
  );
}

function buildHighlightRegex(highlightKeywords) {
  if (!highlightKeywords) return null;
  const terms = [
    ...(highlightKeywords.country_variations || []),
    ...(highlightKeywords.sanctions_keywords || []),
    ...(highlightKeywords.financial_keywords || []),
  ]
    .filter(Boolean)
    .map((t) => String(t).trim())
    .filter((t) => t.length > 0)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!terms.length) return null;
  try {
    return new RegExp(`\\b(${terms.join('|')})\\b`, 'gi');
  } catch {
    return null;
  }
}

function renderHighlighted(text, regex, triggerSentence) {
  if (!text) return null;
  let segments;
  if (triggerSentence && text.includes(triggerSentence)) {
    const idx = text.indexOf(triggerSentence);
    segments = [
      { text: text.slice(0, idx), trigger: false },
      { text: triggerSentence, trigger: true },
      { text: text.slice(idx + triggerSentence.length), trigger: false },
    ];
  } else {
    segments = [{ text, trigger: false }];
  }
  const out = [];
  segments.forEach((seg, si) => {
    if (!seg.text) return;
    if (!regex) {
      out.push(
        <span key={`s${si}`} className={seg.trigger ? 'bg-danger/15 font-semibold px-0.5 rounded' : ''}>
          {seg.text}
        </span>,
      );
      return;
    }
    regex.lastIndex = 0;
    const parts = seg.text.split(regex);
    parts.forEach((p, pi) => {
      if (p === '' || p == null) return;
      regex.lastIndex = 0;
      const isMatch = regex.test(p);
      regex.lastIndex = 0;
      const classes = [];
      if (isMatch) classes.push('text-danger font-bold bg-danger/10 px-0.5 rounded');
      if (seg.trigger && !isMatch) classes.push('bg-danger/15 font-semibold');
      if (seg.trigger && isMatch) classes.push('bg-danger/25');
      out.push(
        <span key={`s${si}p${pi}`} className={classes.join(' ')}>
          {p}
        </span>,
      );
    });
  });
  return out;
}

function renderHighlightedMulti(text, regex, triggerSentences) {
  if (!text) return null;
  const triggers = Array.from(new Set((triggerSentences || []).filter(Boolean)));
  // Find non-overlapping ranges for each trigger sentence's first occurrence.
  const ranges = [];
  triggers.forEach((t) => {
    const idx = text.indexOf(t);
    if (idx >= 0) ranges.push({ start: idx, end: idx + t.length });
  });
  ranges.sort((a, b) => a.start - b.start);
  const merged = [];
  ranges.forEach((r) => {
    const last = merged[merged.length - 1];
    if (last && r.start <= last.end) {
      last.end = Math.max(last.end, r.end);
    } else {
      merged.push({ ...r });
    }
  });

  const segments = [];
  let cursor = 0;
  merged.forEach((r) => {
    if (r.start > cursor) segments.push({ text: text.slice(cursor, r.start), trigger: false });
    segments.push({ text: text.slice(r.start, r.end), trigger: true });
    cursor = r.end;
  });
  if (cursor < text.length) segments.push({ text: text.slice(cursor), trigger: false });

  const out = [];
  segments.forEach((seg, si) => {
    if (!seg.text) return;
    if (!regex) {
      out.push(
        <span
          key={`s${si}`}
          className={seg.trigger ? 'bg-danger/20 text-primary font-semibold px-0.5 rounded' : ''}
        >
          {seg.text}
        </span>,
      );
      return;
    }
    regex.lastIndex = 0;
    const parts = seg.text.split(regex);
    parts.forEach((p, pi) => {
      if (p === '' || p == null) return;
      regex.lastIndex = 0;
      const isMatch = regex.test(p);
      regex.lastIndex = 0;
      const classes = [];
      if (isMatch) classes.push('text-danger font-bold bg-danger/10 px-0.5 rounded');
      if (seg.trigger && !isMatch) classes.push('bg-danger/20 text-primary font-semibold');
      if (seg.trigger && isMatch) classes.push('bg-danger/25');
      out.push(
        <span key={`s${si}p${pi}`} className={classes.join(' ')}>
          {p}
        </span>,
      );
    });
  });
  return out;
}

export function ExcerptBlock({ excerpt, highlightRegex }) {
  const text = (excerpt.text || '').slice(0, 600);
  const overflow = (excerpt.text || '').length > 600;
  const triggerSentence = excerpt.trigger_sentence || '';
  const isRegulatory = excerpt.risk_type === 'SANCTIONS_REGULATORY_MENTION';
  return (
    <div className="bg-bg rounded-xl p-3 border-l-2 border-accent/30 text-xs">
      <div className="flex items-center justify-between mb-1.5">
        <span
          className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
            isRegulatory
              ? 'bg-danger/10 text-danger border border-danger/30'
              : 'bg-accent/10 text-accent'
          }`}
        >
          {isRegulatory && '🛡 '}
          {excerpt.risk_type || 'GENERAL'}
        </span>
        <span className="text-muted text-[10px]">{excerpt.confidence || 0}% conf.</span>
      </div>
      <p className="text-primary/80 leading-relaxed whitespace-pre-wrap break-words">
        {renderHighlighted(text, highlightRegex, triggerSentence)}
        {overflow ? '…' : ''}
      </p>
      {excerpt.note && (
        <p className="mt-1.5 text-danger/80 text-[10px] font-medium">{excerpt.note}</p>
      )}
    </div>
  );
}

export default function ResultsTable({ results, highlightKeywords, jobId, onHitlChanged }) {
  const { authedFetch, isAuthed } = useAuth();
  const highlightRegex = useMemo(() => buildHighlightRegex(highlightKeywords), [highlightKeywords]);
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState('risk');
  const [sortAsc, setSortAsc] = useState(true);
  const [filters, setFilters] = useState({ risk: 'ALL', country: 'ALL', source: 'ALL' });
  const [expandedRow, setExpandedRow] = useState(null);
  const [showFilters, setShowFilters] = useState(false);
  const [findings, setFindings] = useState([]);

  const fetchFindings = useCallback(async () => {
    if (!jobId || !isAuthed) return;
    try {
      const res = await authedFetch(`/api/jobs/${jobId}/findings`);
      if (!res.ok) return;
      const data = await res.json();
      setFindings(data.findings || []);
    } catch { /* ignore — HITL is enhancement, table still renders */ }
  }, [authedFetch, jobId, isAuthed]);

  useEffect(() => { fetchFindings(); }, [fetchFindings]);

  const handleFindingChanged = async () => {
    await fetchFindings();
    onHitlChanged?.();
  };

  // Index findings by URL so the expanded row can look up its findings cheaply.
  const findingsByUrl = useMemo(() => {
    const m = {};
    for (const f of findings) {
      if (!m[f.url]) m[f.url] = [];
      m[f.url].push(f);
    }
    return m;
  }, [findings]);

  // Extract unique values for filter dropdowns
  const countries = useMemo(() => [...new Set(results.map((r) => r.country).filter(Boolean))].sort(), [results]);
  const sources = useMemo(() => [...new Set(results.map((r) => r.source).filter(Boolean))].sort(), [results]);

  // Filter + search + sort
  const processed = useMemo(() => {
    let items = [...results];

    // Filters
    if (filters.risk !== 'ALL') items = items.filter((r) => r.risk_level === filters.risk);
    if (filters.country !== 'ALL') items = items.filter((r) => r.country === filters.country);
    if (filters.source !== 'ALL') items = items.filter((r) => r.source === filters.source);

    // Search
    if (search.trim()) {
      const q = search.toLowerCase();
      items = items.filter((r) =>
        (r.original_title || '').toLowerCase().includes(q) ||
        (r.original_snippet || '').toLowerCase().includes(q) ||
        (r.url || '').toLowerCase().includes(q) ||
        (r.country || '').toLowerCase().includes(q) ||
        (r.matched_names || []).some((n) => n.toLowerCase().includes(q)) ||
        (r.relevant_excerpts || []).some((ex) => (ex.text || '').toLowerCase().includes(q))
      );
    }

    // Sort
    items.sort((a, b) => {
      let cmp = 0;
      switch (sortKey) {
        case 'risk':
          cmp = (RISK_ORDER[a.risk_level] || 5) - (RISK_ORDER[b.risk_level] || 5);
          break;
        case 'confidence':
          cmp = (b.confidence || 0) - (a.confidence || 0);
          break;
        case 'country':
          cmp = (a.country || '').localeCompare(b.country || '');
          break;
        case 'title':
          cmp = (a.original_title || '').localeCompare(b.original_title || '');
          break;
        default:
          break;
      }
      return sortAsc ? cmp : -cmp;
    });

    return items;
  }, [results, search, sortKey, sortAsc, filters]);

  const toggleSort = (key) => {
    if (sortKey === key) {
      setSortAsc(!sortAsc);
    } else {
      setSortKey(key);
      setSortAsc(true);
    }
  };

  const SortButton = ({ label, field }) => (
    <button
      onClick={() => toggleSort(field)}
      className={`flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider transition-colors ${
        sortKey === field ? 'text-accent' : 'text-muted hover:text-primary'
      }`}
    >
      {label}
      {sortKey === field ? (
        sortAsc ? <ChevronUp size={12} /> : <ChevronDown size={12} />
      ) : (
        <ArrowUpDown size={10} className="opacity-30" />
      )}
    </button>
  );

  const activeFilterCount = Object.values(filters).filter((v) => v !== 'ALL').length;
  const hasActiveFilters = activeFilterCount > 0 || search.trim();

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.4 }}
      className="bg-surface rounded-2xl shadow-card border border-border/50 overflow-hidden"
    >
      {/* Toolbar */}
      <div className="p-4 border-b border-border/50">
        <div className="flex items-center gap-3 flex-wrap">
          {/* Search */}
          <div className="relative flex-1 min-w-[200px]">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted/50" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search results…"
              className="w-full pl-9 pr-3 py-2 bg-bg border border-border rounded-lg text-xs text-primary
                         placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/20"
            />
          </div>

          {/* Filter toggle */}
          <button
            onClick={() => setShowFilters(!showFilters)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium border transition-all ${
              showFilters || activeFilterCount > 0
                ? 'bg-accent/10 text-accent border-accent/20'
                : 'bg-bg text-muted border-border hover:text-primary'
            }`}
          >
            <Filter size={12} />
            Filters
            {activeFilterCount > 0 && (
              <span className="w-4 h-4 rounded-full bg-accent text-white text-[9px] flex items-center justify-center font-bold">
                {activeFilterCount}
              </span>
            )}
          </button>

          {/* Result count */}
          <span className="text-[10px] text-muted font-mono">
            {processed.length} of {results.length}
          </span>

          {/* Clear all */}
          {hasActiveFilters && (
            <button
              onClick={() => { setFilters({ risk: 'ALL', country: 'ALL', source: 'ALL' }); setSearch(''); }}
              className="flex items-center gap-1 text-[10px] text-danger font-medium hover:underline"
            >
              <X size={10} /> Clear
            </button>
          )}
        </div>

        {/* Filter row */}
        <AnimatePresence>
          {showFilters && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              className="overflow-hidden"
            >
              <div className="flex items-center gap-4 pt-3 flex-wrap">
                <FilterSelect
                  label="Risk"
                  value={filters.risk}
                  options={['ALL', 'HIGH', 'MEDIUM', 'LOW', 'MINIMAL']}
                  onChange={(v) => setFilters({ ...filters, risk: v })}
                />
                <FilterSelect
                  label="Country"
                  value={filters.country}
                  options={['ALL', ...countries]}
                  onChange={(v) => setFilters({ ...filters, country: v })}
                />
                <FilterSelect
                  label="Source"
                  value={filters.source}
                  options={['ALL', ...sources]}
                  onChange={(v) => setFilters({ ...filters, source: v })}
                />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Table header */}
      <div className="hidden sm:grid grid-cols-[2fr_1fr_1fr_1fr_80px] gap-3 px-5 py-2.5 bg-bg/60 border-b border-border/30">
        <SortButton label="Title / URL" field="title" />
        <SortButton label="Risk" field="risk" />
        <SortButton label="Country" field="country" />
        <SortButton label="Confidence" field="confidence" />
        <span className="text-[10px] font-bold text-muted uppercase tracking-wider">Source</span>
      </div>

      {/* Rows */}
      <div className="divide-y divide-border/30">
        {processed.length === 0 ? (
          <div className="py-16 text-center text-muted text-sm">
            <Search size={32} className="mx-auto mb-3 opacity-20" />
            <p>No results match your filters.</p>
          </div>
        ) : (
          processed.map((r, i) => {
            const isExpanded = expandedRow === i;
            const urlFindings = findingsByUrl[r.url] || [];
            const pendingCount = urlFindings.filter(
              (f) => (f.state?.status || 'pending') === 'pending' || f.state?.status === 'in_review',
            ).length;
            const worstStatus = urlFindings.reduce((acc, f) => {
              const order = { confirmed_match: 0, escalated: 1, in_review: 2, pending: 3, cleared_fp: 4 };
              const s = f.state?.status || 'pending';
              if (!acc) return s;
              return (order[s] ?? 9) < (order[acc] ?? 9) ? s : acc;
            }, null);
            return (
              <div key={r.url + i}>
                <button
                  onClick={() => setExpandedRow(isExpanded ? null : i)}
                  className="w-full text-left grid grid-cols-1 sm:grid-cols-[2fr_1fr_1fr_1fr_80px] gap-2 sm:gap-3
                             px-5 py-3.5 hover:bg-bg/50 transition-colors items-center"
                >
                  {/* Title */}
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-primary truncate">
                      {r.original_title || r.url}
                    </div>
                    <div className="text-[10px] text-muted font-mono truncate">{r.url}</div>
                    {urlFindings.length > 0 && (
                      <div className="mt-1 flex items-center gap-1.5 flex-wrap">
                        {worstStatus && <FindingStatusBadge status={worstStatus} />}
                        {urlFindings.length > 1 && (
                          <span className="text-[10px] text-muted font-mono">
                            {urlFindings.length} findings
                            {pendingCount > 0 ? ` · ${pendingCount} open` : ''}
                          </span>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Risk */}
                  <div><RiskBadge level={r.risk_level} /></div>

                  {/* Country */}
                  <div className="text-xs text-primary font-medium">{r.country}</div>

                  {/* Confidence */}
                  <div className="text-xs font-mono text-muted">{Math.round(r.confidence || 0)}%</div>

                  {/* Source */}
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] text-muted font-medium">{r.source}</span>
                    <ChevronDown
                      size={14}
                      className={`text-muted transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                    />
                  </div>
                </button>

                {/* Expanded row */}
                <AnimatePresence>
                  {isExpanded && (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      className="overflow-hidden"
                    >
                      <div className="px-5 pb-5 pt-1 space-y-3">
                        {/* Meta */}
                        <div className="flex flex-wrap gap-3 text-xs">
                          <a
                            href={r.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-center gap-1 text-accent hover:underline"
                          >
                            <ExternalLink size={12} /> Open URL
                          </a>
                          {r.extraction_type === 'PDF' && (
                            <span className="flex items-center gap-1 text-success/80">
                              <FileText size={12} /> PDF — text extracted
                            </span>
                          )}
                          {r.extraction_type === 'PDF_CACHE' && (
                            <span className="flex items-center gap-1 text-warning">
                              <FileText size={12} /> PDF — via Google cache
                            </span>
                          )}
                          {r.language && r.language !== 'en' && (
                            <span
                              className="flex items-center gap-1 text-warning"
                              title="Non-English source — machine translation not yet included; trigger-sentence matching may be less reliable."
                            >
                              <Globe size={12} /> {r.language.toUpperCase()} source · translation pending
                            </span>
                          )}
                          {r.extraction_type === 'SNIPPET_FALLBACK' && (
                            <span className="flex items-center gap-1 text-muted">
                              <FileText size={12} /> Snippet only — full page not extracted
                            </span>
                          )}
                          {r.extraction_type === 'DOCUMENT' && (
                            <span className="flex items-center gap-1 text-muted">
                              <FileText size={12} /> Document — manual review needed
                            </span>
                          )}
                          {r.matched_name_type && r.matched_name_type !== 'NONE' && (
                            <span className="text-muted">
                              Name match: <strong className="text-primary">{r.matched_name_type}</strong>
                              {r.matched_names?.length > 0 && ` — ${r.matched_names.join(', ')}`}
                            </span>
                          )}
                        </div>

                        {/* Per-link LLM verdict + analyst agree/disagree */}
                        <LinkVerdictBlock
                          jobId={jobId}
                          urlHash={r.url_hash}
                          verdict={r.link_verdict}
                          onChanged={onHitlChanged}
                        />

                        {/* Snippet — only shown when the full page couldn't
                            be extracted; otherwise the consolidated page
                            content below covers this and more. */}
                        {r.original_snippet && !r.extracted_content && (
                          <div className="text-xs text-primary/70 bg-bg rounded-lg p-3">
                            <span className="text-[10px] font-bold text-muted uppercase tracking-wider block mb-1">
                              Search snippet
                            </span>
                            {renderHighlighted(r.original_snippet, highlightRegex, null)}
                          </div>
                        )}

                        {/* Consolidated page content with trigger sentences
                            highlighted inline. Replaces the prior multi-
                            excerpt list so the analyst can read the whole
                            page in one go and still have the risky phrases
                            jump out. */}
                        {r.extracted_content ? (
                          <PageContentBlock
                            content={r.extracted_content}
                            triggerSentences={(r.relevant_excerpts || []).map((ex) => ex.trigger_sentence).filter(Boolean)}
                            highlightRegex={highlightRegex}
                            excerptCount={(r.relevant_excerpts || []).length}
                          />
                        ) : (r.relevant_excerpts?.length > 0 && (
                          <div className="space-y-2">
                            <span className="text-[10px] font-bold text-muted uppercase tracking-wider">
                              Relevant excerpts ({r.relevant_excerpts.length})
                            </span>
                            {r.relevant_excerpts.map((ex, j) => (
                              <ExcerptBlock key={j} excerpt={ex} highlightRegex={highlightRegex} />
                            ))}
                          </div>
                        ))}

                        {/* HITL dispositions — per-finding analyst controls */}
                        {jobId && urlFindings.length > 0 && (
                          <div className="space-y-2 pt-2">
                            <span className="text-[10px] font-bold text-muted uppercase tracking-wider">
                              Analyst disposition ({urlFindings.length})
                            </span>
                            {urlFindings.map((f) => (
                              <FindingControls
                                key={f.finding_id}
                                finding={f}
                                onChanged={handleFindingChanged}
                              />
                            ))}
                          </div>
                        )}

                        {/* Audit trail — confirmed non-sanctions references the
                            engine saw and excluded from risk scoring. */}
                        {r.excluded_references?.length > 0 && (
                          <ExcludedReferencesBlock excluded={r.excluded_references} />
                        )}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            );
          })
        )}
      </div>
    </motion.div>
  );
}

function ExcludedReferencesBlock({ excluded }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="pt-2">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-[10px] font-bold text-muted/70 uppercase tracking-wider hover:text-muted"
      >
        <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
        Non-sanctions matches ({excluded.length})
        <span className="normal-case font-normal text-muted/50">
          · audit trail · excluded from risk
        </span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="mt-2 space-y-1.5">
              {excluded.map((ex, i) => (
                <div
                  key={i}
                  className="bg-muted/5 rounded-lg p-2.5 border border-dashed border-muted/20 text-[11px] text-muted/80"
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase bg-muted/10 text-muted/70">
                      {ex.matched_phrase}
                    </span>
                    <span className="text-muted/50 text-[10px]">{ex.risk_type}</span>
                  </div>
                  <p className="leading-relaxed break-words">{ex.text}</p>
                  {ex.note && (
                    <p className="mt-1 text-muted/60 text-[10px] italic">{ex.note}</p>
                  )}
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function PageContentBlock({ content, triggerSentences, highlightRegex, excerptCount }) {
  const [expanded, setExpanded] = useState(false);
  const COLLAPSED_LIMIT = 4000;
  const isLong = content.length > COLLAPSED_LIMIT;
  const shown = expanded || !isLong ? content : content.slice(0, COLLAPSED_LIMIT);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-bold text-muted uppercase tracking-wider">
          Page content
          {excerptCount > 0 ? (
            <span className="ml-2 font-mono text-muted/70 normal-case">
              · {excerptCount} trigger sentence{excerptCount === 1 ? '' : 's'} highlighted
            </span>
          ) : null}
        </span>
        {isLong && (
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            className="text-[10px] text-accent hover:underline font-medium"
          >
            {expanded ? 'Show less' : `Show all (${content.length.toLocaleString()} chars)`}
          </button>
        )}
      </div>
      <div className="bg-bg rounded-xl p-3 border border-border/40 text-xs text-primary/80 leading-relaxed whitespace-pre-wrap break-words max-h-[600px] overflow-y-auto">
        {renderHighlightedMulti(shown, highlightRegex, triggerSentences)}
        {!expanded && isLong ? '…' : ''}
      </div>
    </div>
  );
}

function FilterSelect({ label, value, options, onChange }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] text-muted font-bold uppercase tracking-wider">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="text-xs bg-bg border border-border rounded-lg px-2.5 py-1.5 text-primary
                   focus:outline-none focus:ring-2 focus:ring-accent/20 cursor-pointer"
      >
        {options.map((o) => (
          <option key={o} value={o}>{o === 'ALL' ? `All ${label}s` : o}</option>
        ))}
      </select>
    </div>
  );
}
