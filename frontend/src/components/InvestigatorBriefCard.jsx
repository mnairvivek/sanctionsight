import React, { useState } from 'react';
import { motion } from 'framer-motion';
import { AlertTriangle, Search, CheckCircle, HelpCircle, Info } from 'lucide-react';
import CitationPopover from './CitationPopover';

const RECOMMENDATION_CONFIG = {
  ESCALATE_FOR_REVIEW: {
    label: 'Possible concern — review needed',
    icon: AlertTriangle,
    bg: 'bg-danger/5',
    border: 'border-danger',
    badge: 'bg-danger/10 text-danger',
    iconColor: 'text-danger',
  },
  ADDITIONAL_OSINT_NEEDED: {
    label: 'More research needed',
    icon: Search,
    bg: 'bg-warning/5',
    border: 'border-warning',
    badge: 'bg-warning/10 text-warning',
    iconColor: 'text-warning',
  },
  NO_FURTHER_ACTION_RECOMMENDED: {
    label: 'No sanctions concerns found',
    icon: CheckCircle,
    bg: 'bg-success/5',
    border: 'border-success',
    badge: 'bg-success/10 text-success',
    iconColor: 'text-success',
  },
  INSUFFICIENT_DATA: {
    label: 'Not enough information to assess',
    icon: HelpCircle,
    bg: 'bg-muted/5',
    border: 'border-muted/30',
    badge: 'bg-muted/10 text-muted',
    iconColor: 'text-muted',
  },
};

function ClaimList({ title, claims, bulletColorClass, onCitationClick, excerptIndex }) {
  if (!claims || claims.length === 0) return null;
  return (
    <div>
      <div className="text-xs font-bold text-muted uppercase tracking-wider mb-2">
        {title}
      </div>
      <ul className="space-y-1.5">
        {claims.map((claim, i) => {
          const citations = claim.citations || [];
          return (
            <li key={i} className="text-xs text-primary/80 flex items-start gap-1.5">
              <span className={`${bulletColorClass} mt-0.5`}>•</span>
              <span className="flex-1">
                {claim.text}
                {citations.length > 0 && (
                  <span className="ml-1.5 inline-flex items-center gap-1 flex-wrap text-[10px] font-mono">
                    {citations.map((c, j) => {
                      const resolved = !!excerptIndex?.[c.excerpt_id];
                      return (
                        <button
                          type="button"
                          key={j}
                          onClick={() => onCitationClick?.(c)}
                          title={
                            resolved
                              ? `View cited evidence ${c.excerpt_id}`
                              : `Evidence ${c.excerpt_id} not found in payload`
                          }
                          className={`px-1.5 py-0.5 rounded border transition-colors ${
                            resolved
                              ? 'bg-accent/10 border-accent/30 text-accent hover:bg-accent/20'
                              : 'bg-muted/10 border-muted/20 text-muted/70 hover:bg-muted/20'
                          }`}
                        >
                          {c.excerpt_id?.replace(/^exc_/, '')?.slice(0, 8) || '?'}
                        </button>
                      );
                    })}
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default function InvestigatorBriefCard({ brief, excerptIndex }) {
  const [activeCitation, setActiveCitation] = useState(null);

  if (!brief) return null;

  const recKey = (brief.recommendation || 'INSUFFICIENT_DATA').toUpperCase();
  const config = RECOMMENDATION_CONFIG[recKey] || RECOMMENDATION_CONFIG.INSUFFICIENT_DATA;
  const Icon = config.icon;

  const summary = (brief.summary_claims || []).map((c) => c.text).filter(Boolean).join(' ');
  const evidenceCount = brief.evidence_count ?? 0;
  const dropped = brief.unverified_claims_dropped ?? 0;
  const directMatch = brief.direct_list_match;

  return (
    <>
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className={`rounded-2xl border-l-4 ${config.border} ${config.bg} p-6 mb-6`}
      >
        <div className="flex items-start gap-4">
          <div className={`p-2.5 rounded-xl ${config.badge}`}>
            <Icon size={22} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 flex-wrap mb-2">
              <span className={`px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider ${config.badge}`}>
                Recommendation: {config.label}
              </span>
              <span className="text-xs text-muted font-medium">
                Confidence: {brief.confidence_band || '—'}
              </span>
              <span className="text-[10px] text-muted/70 font-mono">
                {evidenceCount} excerpts · {dropped} claim{dropped === 1 ? '' : 's'} dropped by verifier
              </span>
            </div>

            {summary ? (
              <p className="text-sm text-primary leading-relaxed mb-4">{summary}</p>
            ) : brief.note ? (
              <div className="text-sm text-primary/80 leading-relaxed mb-4 flex items-start gap-2">
                <Info size={16} className="mt-0.5 flex-shrink-0 text-muted" />
                <span>{brief.note}</span>
              </div>
            ) : (
              <p className="text-sm text-muted leading-relaxed mb-4 italic">
                No summary claims survived post-verification.
              </p>
            )}

            {(brief.summary_claims || []).some((c) => (c.citations || []).length > 0) && (
              <div className="mb-4">
                <ClaimList
                  title="Summary Citations"
                  claims={brief.summary_claims}
                  bulletColorClass="text-muted"
                  onCitationClick={setActiveCitation}
                  excerptIndex={excerptIndex}
                />
              </div>
            )}

            {directMatch && (
              <div className="mb-4 text-xs text-primary/80 bg-danger/5 border border-danger/20 rounded-lg p-3">
                <div className="font-bold text-danger mb-1">Direct sanctions list match</div>
                <div>Lists: {(directMatch.matched_lists || []).join(', ') || '—'}</div>
                <div>Names: {(directMatch.matched_names || []).slice(0, 3).join(', ') || '—'}</div>
              </div>
            )}

            {((brief.risk_factor_claims || []).length > 0 || (brief.suggested_next_steps || []).length > 0) && (
              <div className="grid sm:grid-cols-2 gap-4">
                <ClaimList
                  title="Risk Factors"
                  claims={brief.risk_factor_claims}
                  bulletColorClass="text-danger"
                  onCitationClick={setActiveCitation}
                  excerptIndex={excerptIndex}
                />
                <ClaimList
                  title="Suggested Next Steps"
                  claims={brief.suggested_next_steps}
                  bulletColorClass="text-accent"
                  onCitationClick={setActiveCitation}
                  excerptIndex={excerptIndex}
                />
              </div>
            )}

            <div className="mt-3 text-[10px] text-muted/50 font-mono">
              Automated first-pass screening · Recommendation only · the analyst decides
            </div>
          </div>
        </div>
      </motion.div>

      {activeCitation && (
        <CitationPopover
          citation={activeCitation}
          excerptIndex={excerptIndex}
          onClose={() => setActiveCitation(null)}
        />
      )}
    </>
  );
}
