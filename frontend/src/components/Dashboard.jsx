import React, { useMemo, useState } from 'react';
import { motion } from 'framer-motion';
import {
  AlertTriangle, AlertCircle, CheckCircle, MinusCircle,
  Globe, Link2, Users, BarChart3, Download, Loader2,
} from 'lucide-react';
import InvestigatorBriefCard from './InvestigatorBriefCard';
import CaseReviewCard from './CaseReviewCard';
import ListScreeningCard from './ListScreeningCard';
import RegulatoryFindingsCard from './RegulatoryFindingsCard';
import RiskCharts from './RiskCharts';
import ResultsTable from './ResultsTable';
import CaseHeader from './CaseHeader';
import ExtractionReviewSections from './ExtractionReviewSections';
import { buildExcerptIndex } from './CitationPopover';

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

function StatCard({ icon: Icon, label, value, sublabel, color, delay = 0 }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay }}
      className="bg-surface rounded-2xl shadow-card border border-border/50 p-5 hover:shadow-lifted transition-shadow"
    >
      <div className="flex items-start justify-between mb-3">
        <div className={`p-2 rounded-xl ${color}`}>
          <Icon size={16} />
        </div>
      </div>
      <div className="font-display font-bold text-3xl text-primary tracking-tight">{value}</div>
      <div className="text-xs font-semibold text-muted mt-1">{label}</div>
      {sublabel && <div className="text-[10px] text-muted/60 mt-0.5">{sublabel}</div>}
    </motion.div>
  );
}

export default function Dashboard({ data }) {
  const jobId = data.job_id || data._jobId;
  const {
    summary, reports, name_co_results, social_links, website,
    highlight_keywords, list_screening, is_sanctioned, regulatory_findings,
  } = data;
  // Accept the new key (investigator_brief) and fall back to the legacy
  // key (llm_verdict) so older report payloads still render.
  const investigatorBrief = data.investigator_brief ?? data.llm_verdict ?? null;
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState(null);
  const [hitlRefreshKey, setHitlRefreshKey] = useState(0);

  const highlightRegex = useMemo(() => buildHighlightRegex(highlight_keywords), [highlight_keywords]);
  const excerptIndex = useMemo(() => buildExcerptIndex(data), [data]);

  const allResults = useMemo(() => {
    const items = [];
    for (const report of reports || []) {
      if (report.country === 'Sanctions/OFAC') continue;
      for (const ar of report.analyzed_results || []) {
        items.push({ ...ar, country: report.country });
      }
    }
    for (const r of name_co_results || []) {
      if (!items.some((i) => i.url === r.url)) {
        items.push(r);
      }
    }
    return items;
  }, [reports, name_co_results]);

  const socialCount = Object.keys(social_links || {}).length;

  const handleDownloadReport = async () => {
    console.log('Download clicked. jobId:', jobId, 'data.job_id:', data.job_id);
    if (!jobId) {
      setDownloadError('No job ID available. Please run a new analysis.');
      return;
    }
    setDownloading(true);
    setDownloadError(null);
    try {
      const res = await fetch(`/api/report/${jobId}`);
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Server returned ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const disposition = res.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="?([^"]+)"?/);
      a.download = match ? match[1] : `sanctions_report_${website.replace(/\./g, '_')}.html`;
      a.href = url;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Download failed:', err);
      setDownloadError(err.message);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="pt-8">
      {/* Page header with download button */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="mb-8"
      >
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <Globe size={16} className="text-accent" />
              <span className="text-xs font-mono text-muted">{website}</span>
            </div>
            <h2 className="font-display font-bold text-2xl sm:text-3xl text-primary tracking-tight">
              Analysis Results
            </h2>
            <p className="text-muted text-sm mt-1">
              {data.timestamp ? new Date(data.timestamp).toLocaleString() : ''}
            </p>
          </div>
          <motion.button
            onClick={handleDownloadReport}
            disabled={downloading}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
            className="flex items-center gap-2 px-5 py-3 rounded-xl bg-accent text-white font-display font-bold text-sm
                       shadow-glow hover:shadow-lifted transition-all
                       disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {downloading ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
            {downloading ? 'Downloading…' : 'Download HTML Report'}
          </motion.button>
          {downloadError && (
            <div className="text-xs text-danger bg-danger/10 px-3 py-2 rounded-lg">
              {downloadError}
            </div>
          )}
        </div>
      </motion.div>

      <CaseHeader key={hitlRefreshKey} jobId={jobId} />

      <InvestigatorBriefCard brief={investigatorBrief} excerptIndex={excerptIndex} />

      <CaseReviewCard
        key={`case-review-${hitlRefreshKey}`}
        jobId={jobId}
        caseReview={data.case_review}
        onChanged={() => setHitlRefreshKey((k) => k + 1)}
      />

      <ListScreeningCard matches={list_screening} isSanctioned={is_sanctioned} />

      {!is_sanctioned && (
        <>
          <RegulatoryFindingsCard report={regulatory_findings} highlightRegex={highlightRegex} />

          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <StatCard icon={AlertTriangle} label="High Risk" value={summary?.total_high || 0}
              sublabel="Direct business relationships" color="bg-danger/10 text-danger" delay={0.1} />
            <StatCard icon={AlertCircle} label="Medium Risk" value={summary?.total_medium || 0}
              sublabel="Indirect relationships" color="bg-warning/10 text-warning" delay={0.15} />
            <StatCard icon={CheckCircle} label="Low Risk" value={summary?.total_low || 0}
              sublabel="Compliance mentions" color="bg-success/10 text-success" delay={0.2} />
            <StatCard icon={MinusCircle} label="Minimal Risk" value={summary?.total_minimal || 0}
              sublabel="General references" color="bg-muted/10 text-muted" delay={0.25} />
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <StatCard icon={BarChart3} label="URLs Analyzed" value={summary?.total_analyzed || 0}
              sublabel="Across all jurisdictions" color="bg-accent/10 text-accent" delay={0.3} />
            <StatCard icon={Link2} label="Name Co-occurrence" value={summary?.name_co_count || 0}
              sublabel="Open web matches" color="bg-danger/10 text-danger" delay={0.35} />
            <StatCard icon={Users} label="Social Profiles" value={socialCount}
              sublabel={socialCount > 0 ? Object.keys(social_links).join(', ') : 'None found'}
              color="bg-accent/10 text-accent" delay={0.4} />
            <StatCard icon={Globe} label="Jurisdictions"
              value={Object.keys(summary?.country_breakdown || {}).filter((c) => c !== 'Sanctions/OFAC').length}
              sublabel="Countries scanned" color="bg-primary/10 text-primary" delay={0.45} />
          </div>

          <RiskCharts summary={summary} />

          {socialCount > 0 && (
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.35 }}
              className="bg-surface rounded-2xl shadow-card border border-border/50 p-5 mb-6">
              <h3 className="text-xs font-bold text-muted uppercase tracking-wider mb-3">Social Media Profiles</h3>
              <div className="flex flex-wrap gap-3">
                {Object.entries(social_links).map(([platform, url]) => (
                  <a key={platform} href={url} target="_blank" rel="noopener noreferrer"
                    className="flex items-center gap-2 px-4 py-2.5 bg-bg rounded-xl border border-border/50
                               hover:border-accent/30 hover:bg-accent/5 transition-all text-sm font-medium text-primary">
                    <span>{platform === 'facebook' ? '📘' : '💼'}</span>
                    {platform.charAt(0).toUpperCase() + platform.slice(1)}
                  </a>
                ))}
              </div>
            </motion.div>
          )}

          <div className="mb-8">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-display font-bold text-lg text-primary">All Results</h3>
              <span className="text-xs text-muted font-mono">{allResults.length} total</span>
            </div>
            <ResultsTable
              results={allResults}
              highlightKeywords={highlight_keywords}
              jobId={jobId}
              onHitlChanged={() => setHitlRefreshKey((k) => k + 1)}
            />
          </div>

          <ExtractionReviewSections
            needsAnalystReview={data.needs_analyst_review}
            notAttempted={data.not_attempted}
          />
        </>
      )}
    </div>
  );
}
