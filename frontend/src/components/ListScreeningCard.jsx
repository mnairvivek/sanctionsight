import React from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ShieldAlert, ShieldCheck, ChevronDown, ExternalLink } from 'lucide-react';

const SOURCE_LABELS = {
  OFAC_SDN: { label: 'OFAC SDN', color: 'bg-danger/10 text-danger' },
  OFAC_CONS: { label: 'OFAC Non-SDN', color: 'bg-danger/10 text-danger' },
  EU: { label: 'EU', color: 'bg-warning/10 text-warning' },
  UN_SC: { label: 'UN', color: 'bg-warning/10 text-warning' },
  OFSI_UK: { label: 'OFSI (UK)', color: 'bg-accent/10 text-accent' },
  BIS: { label: 'BIS', color: 'bg-primary/10 text-primary' },
  BIS_ENTITY: { label: 'BIS Entity List', color: 'bg-primary/10 text-primary' },
  BIS_DENIED: { label: 'BIS Denied Persons', color: 'bg-danger/10 text-danger' },
  BIS_UVL: { label: 'BIS Unverified', color: 'bg-warning/10 text-warning' },
  BIS_MEU: { label: 'BIS Mil. End User', color: 'bg-warning/10 text-warning' },
  // US Consolidated Screening List sources
  CSL: { label: 'US CSL', color: 'bg-danger/10 text-danger' },
  STATE_DEBARRED: { label: 'State Debarred', color: 'bg-danger/10 text-danger' },
  STATE_NONPRO: { label: 'State Nonprolif.', color: 'bg-warning/10 text-warning' },
  // OpenSanctions sources
  OPENSANCTIONS: { label: 'OpenSanctions', color: 'bg-primary/10 text-primary' },
  OPENSANCTIONS_AU: { label: 'Australia (DFAT)', color: 'bg-primary/10 text-primary' },
  OPENSANCTIONS_CA: { label: 'Canada (SEMA)', color: 'bg-primary/10 text-primary' },
  OPENSANCTIONS_CH: { label: 'Switzerland (SECO)', color: 'bg-primary/10 text-primary' },
  OPENSANCTIONS_JP: { label: 'Japan (MOF)', color: 'bg-primary/10 text-primary' },
  INTERPOL: { label: 'INTERPOL', color: 'bg-danger/10 text-danger' },
};

const SOURCE_URLS = {
  OFAC_SDN: 'https://sanctionssearch.ofac.treas.gov/',
  OFAC_CONS: 'https://sanctionssearch.ofac.treas.gov/',
  EU: 'https://www.sanctionsmap.eu/',
  UN_SC: 'https://www.un.org/securitycouncil/sanctions/information',
  OFSI_UK: 'https://sanctionssearchapp.ofsi.hmtreasury.gov.uk/',
  BIS: 'https://www.bis.gov/licensing/guidance-on-end-user-and-end-use-controls-and-us-person-controls',
  BIS_ENTITY: 'https://www.trade.gov/consolidated-screening-list',
  BIS_DENIED: 'https://www.trade.gov/consolidated-screening-list',
  BIS_UVL: 'https://www.trade.gov/consolidated-screening-list',
  BIS_MEU: 'https://www.trade.gov/consolidated-screening-list',
  CSL: 'https://www.trade.gov/consolidated-screening-list',
  STATE_DEBARRED: 'https://www.trade.gov/consolidated-screening-list',
  STATE_NONPRO: 'https://www.trade.gov/consolidated-screening-list',
  OPENSANCTIONS: 'https://www.opensanctions.org/',
  OPENSANCTIONS_AU: 'https://www.dfat.gov.au/international-relations/security/sanctions',
  OPENSANCTIONS_CA: 'https://www.international.gc.ca/world-monde/international_relations-relations_internationales/sanctions/index.aspx',
  OPENSANCTIONS_CH: 'https://www.seco.admin.ch/seco/en/home/Aussenwirtschaftspolitik_Wirtschaftliche_Zusammenarbeit/Wirtschaftsbeziehungen/exportkontrollen-und-sanktionen/sanktionen-embargos.html',
  OPENSANCTIONS_JP: 'https://www.mof.go.jp/policy/international_policy/gaitame_kawase/gaitame/economic_sanctions/index.html',
  INTERPOL: 'https://www.interpol.int/en/How-we-work/Notices/Red-Notices',
};

function ScoreBadge({ score }) {
  const color =
    score >= 95
      ? 'bg-danger/10 text-danger'
      : score >= 85
      ? 'bg-warning/10 text-warning'
      : 'bg-muted/10 text-muted';

  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${color}`}>
      {Math.round(score)}% match
    </span>
  );
}

function MatchRow({ match }) {
  const [expanded, setExpanded] = React.useState(false);
  const source = SOURCE_LABELS[match.list_source] || { label: match.list_source, color: 'bg-muted/10 text-muted' };
  const sourceUrl = SOURCE_URLS[match.list_source] || null;

  return (
    <div className="border-b border-border/30 last:border-b-0">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full text-left px-4 py-3 hover:bg-bg/50 transition-colors flex items-center gap-3"
      >
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-primary truncate">
            {match.listed_name}
          </div>
          {match.matched_name !== match.listed_name && (
            <div className="text-[10px] text-muted truncate">
              Matched via: {match.matched_name}
            </div>
          )}
        </div>

        <span className={`px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider whitespace-nowrap ${source.color}`}>
          {source.label}
        </span>

        <ScoreBadge score={match.score} />

        <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
          match.entity_type === 'INDIVIDUAL' ? 'bg-accent/10 text-accent' : 'bg-primary/10 text-primary'
        }`}>
          {match.entity_type === 'INDIVIDUAL' ? 'Person' : 'Org'}
        </span>

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
            <div className="px-4 pb-3 pt-1 space-y-2">
              <div className="grid grid-cols-2 gap-3 text-xs">
                {match.country && (
                  <div>
                    <span className="text-muted font-medium">Country: </span>
                    <span className="text-primary">{match.country}</span>
                  </div>
                )}
                {match.programs && (
                  <div>
                    <span className="text-muted font-medium">Programs: </span>
                    <span className="text-primary">{match.programs}</span>
                  </div>
                )}
                {match.source_id && (
                  <div>
                    <span className="text-muted font-medium">List ID: </span>
                    <span className="text-primary font-mono">{match.source_id}</span>
                  </div>
                )}
                {match.query_name && (
                  <div>
                    <span className="text-muted font-medium">Searched: </span>
                    <span className="text-primary">{match.query_name}</span>
                  </div>
                )}
              </div>

              {match.aliases && match.aliases.length > 0 && (
                <div className="text-xs">
                  <span className="text-muted font-medium">Also known as: </span>
                  <span className="text-primary/70">{match.aliases.join(', ')}</span>
                </div>
              )}

              {(match.sanctions_url || sourceUrl) && (
                <a
                  href={match.sanctions_url || sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs text-accent hover:underline mt-1"
                >
                  <ExternalLink size={12} />
                  {match.sanctions_url ? 'View sanctions listing' : 'View on official sanctions page'}
                </a>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function ListScreeningCard({ matches, isSanctioned }) {
  if (!matches || matches.length === 0) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.08 }}
        className="rounded-2xl border-l-4 border-success bg-success/5 p-5 mb-6"
      >
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-success/10 text-success">
            <ShieldCheck size={20} />
          </div>
          <div>
            <div className="text-sm font-bold text-primary">No Sanctions List Matches</div>
            <div className="text-xs text-muted">
              No matches found against OFAC SDN/CSL, EU, UN, OFSI, BIS, OpenSanctions, or INTERPOL lists.
              Proceeding with open-web investigation.
            </div>
          </div>
        </div>
      </motion.div>
    );
  }

  const highConfidence = matches.filter((m) => m.score >= 90).length;
  const matchedLists = [...new Set(matches.filter((m) => m.score >= 90).map((m) => {
    const src = SOURCE_LABELS[m.list_source];
    return src ? src.label : m.list_source;
  }))];

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.08 }}
      className={`rounded-2xl border-l-4 ${
        isSanctioned ? 'border-danger bg-danger/5' : highConfidence > 0 ? 'border-danger bg-danger/5' : 'border-warning bg-warning/5'
      } mb-6 overflow-hidden`}
    >
      {/* Sanctioned banner */}
      {isSanctioned && (
        <div className="bg-danger/15 px-5 py-3 border-b border-danger/20">
          <div className="flex items-center gap-2">
            <ShieldAlert size={18} className="text-danger" />
            <span className="text-sm font-bold text-danger uppercase tracking-wider">
              Entity is Sanctioned
            </span>
          </div>
          <p className="text-xs text-danger/80 mt-1">
            This entity was found on {matchedLists.length > 0 ? matchedLists.join(', ') : 'sanctions lists'}.
            Open-web investigation was skipped. Do not proceed with any transactions without compliance review.
          </p>
        </div>
      )}

      {/* Header */}
      <div className="p-5 pb-3">
        <div className="flex items-center gap-3 mb-2">
          <div className={`p-2 rounded-xl ${
            isSanctioned || highConfidence > 0 ? 'bg-danger/10 text-danger' : 'bg-warning/10 text-warning'
          }`}>
            <ShieldAlert size={20} />
          </div>
          <div>
            <div className="text-sm font-bold text-primary">
              {matches.length} Sanctions List Match{matches.length !== 1 ? 'es' : ''} Found
            </div>
            <div className="text-xs text-muted">
              {isSanctioned
                ? 'Direct sanctions list match confirmed — review details below'
                : highConfidence > 0
                ? `${highConfidence} high-confidence match${highConfidence !== 1 ? 'es' : ''} — review required`
                : 'Potential matches found — manual review recommended'}
            </div>
          </div>
        </div>

        {/* Quick links to sanctions pages */}
        {isSanctioned && (
          <div className="flex flex-wrap gap-2 mt-3">
            {matches.filter((m) => m.score >= 90).map((m, i) => {
              const url = m.sanctions_url || SOURCE_URLS[m.list_source];
              const label = SOURCE_LABELS[m.list_source]?.label || m.list_source;
              if (!url) return null;
              return (
                <a
                  key={`${m.list_source}-${m.source_id}-${i}`}
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-danger/10 text-danger rounded-lg text-xs font-medium hover:bg-danger/20 transition-colors"
                >
                  <ExternalLink size={11} />
                  {m.sanctions_url ? `View ${m.listed_name.slice(0, 30)} on ${label}` : `Search on ${label}`}
                </a>
              );
            })}
          </div>
        )}
      </div>

      {/* Match list */}
      <div className="bg-surface/50 border-t border-border/30">
        {matches.map((match, i) => (
          <MatchRow key={`${match.list_source}-${match.source_id}-${i}`} match={match} />
        ))}
      </div>

      <div className="px-5 py-2.5 text-[10px] text-muted/50 font-mono border-t border-border/20">
        Fuzzy matching against downloaded sanctions lists · manual verification required
      </div>
    </motion.div>
  );
}
