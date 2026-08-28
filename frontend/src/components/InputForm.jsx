import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Globe, Building2, Scale, Zap, Search } from 'lucide-react';
import CountrySelector from './CountrySelector';

export default function InputForm({ onSubmit }) {
  const [website, setWebsite] = useState('');
  const [skipContent, setSkipContent] = useState(false);
  const [businessName, setBusinessName] = useState('');
  const [legalName, setLegalName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const [availableBuiltins, setAvailableBuiltins] = useState([]);
  const [selectedCountries, setSelectedCountries] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/countries')
      .then((r) => r.json())
      .then((data) => {
        if (cancelled) return;
        setAvailableBuiltins(data.builtins || []);
        setSelectedCountries({
          builtins: data.default_selected || [],
          custom: [],
        });
      })
      .catch(() => {
        if (cancelled) return;
        setAvailableBuiltins([]);
        setSelectedCountries({ builtins: [], custom: [] });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const hasInput = website.trim() || businessName.trim() || legalName.trim();
  const totalCountries =
    (selectedCountries?.builtins?.length ?? 0) + (selectedCountries?.custom?.length ?? 0);
  const hasCountries = totalCountries > 0;
  const canSubmit = hasInput && hasCountries && selectedCountries !== null;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    await onSubmit({
      website: website.trim(),
      skip_content: skipContent,
      business_name: businessName.trim(),
      legal_name: legalName.trim(),
      run_name_cooccurrence: !!(businessName.trim() || legalName.trim()),
      selected_builtins: selectedCountries.builtins,
      custom_countries: selectedCountries.custom,
    });
    setSubmitting(false);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -20 }}
      transition={{ duration: 0.4 }}
      className="pt-12 sm:pt-20"
    >
      {/* Hero */}
      <div className="text-center mb-10">
        <motion.div
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ delay: 0.1, type: 'spring', stiffness: 200 }}
          className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-accent/10 mb-5"
        >
          <Search size={28} className="text-accent" />
        </motion.div>
        <h2 className="font-display font-bold text-3xl sm:text-4xl text-primary tracking-tight mb-3">
          Sanctions Site Analysis
        </h2>
        <p className="text-muted max-w-lg mx-auto text-sm leading-relaxed">
          Scan a website for sanctions-related content across OFAC-relevant
          jurisdictions using NLP-based risk assessment.
        </p>
      </div>

      {/* Form card */}
      <form onSubmit={handleSubmit} className="max-w-xl mx-auto">
        <div className="bg-surface rounded-2xl shadow-card border border-border/50 overflow-hidden">
          {/* Website input */}
          <div className="p-6 pb-4">
            <label className="block text-xs font-semibold text-muted uppercase tracking-wider mb-2">
              Target Website
            </label>
            <div className="relative">
              <Globe size={18} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-muted/50" />
              <input
                type="text"
                value={website}
                onChange={(e) => setWebsite(e.target.value)}
                placeholder="example.com"
                className="w-full pl-11 pr-4 py-3.5 bg-bg border border-border rounded-xl text-primary
                           placeholder:text-muted/40 font-mono text-sm
                           focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent
                           transition-all"
              />
            </div>
          </div>

          {/* Quick toggle */}
          <div className="px-6 pb-4">
            <label className="flex items-center gap-3 cursor-pointer group">
              <div className={`relative w-10 h-6 rounded-full transition-colors ${skipContent ? 'bg-accent' : 'bg-border'}`}>
                <div className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full shadow transition-transform ${skipContent ? 'translate-x-4' : ''}`} />
              </div>
              <input type="checkbox" checked={skipContent} onChange={(e) => setSkipContent(e.target.checked)} className="sr-only" />
              <div>
                <span className="text-sm font-medium text-primary flex items-center gap-1.5">
                  <Zap size={14} className="text-warning" /> Quick scan
                </span>
                <span className="text-xs text-muted">Skip deep content analysis for faster results</span>
              </div>
            </label>
          </div>

          {/* Name fields */}
          <div className="border-t border-border/50 px-6 py-5 space-y-3">
            <div>
              <label className="block text-xs font-semibold text-muted uppercase tracking-wider mb-1.5">
                Business / Trading Name
              </label>
              <div className="relative">
                <Building2 size={16} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-muted/50" />
                <input
                  type="text"
                  value={businessName}
                  onChange={(e) => setBusinessName(e.target.value)}
                  placeholder="Acme Corp"
                  className="w-full pl-10 pr-4 py-2.5 bg-bg border border-border rounded-xl text-primary
                             placeholder:text-muted/40 text-sm
                             focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent"
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-semibold text-muted uppercase tracking-wider mb-1.5">
                Legal Name
              </label>
              <div className="relative">
                <Scale size={16} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-muted/50" />
                <input
                  type="text"
                  value={legalName}
                  onChange={(e) => setLegalName(e.target.value)}
                  placeholder="Acme Corporation LLC"
                  className="w-full pl-10 pr-4 py-2.5 bg-bg border border-border rounded-xl text-primary
                             placeholder:text-muted/40 text-sm
                             focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent"
                />
              </div>
            </div>
            <p className="text-[10px] text-muted/50 pt-1">
              Enter at least one field above to run an analysis.
            </p>
          </div>

          {/* Country selector */}
          <div className="border-t border-border/50 px-6 py-5">
            {selectedCountries !== null && (
              <CountrySelector
                selected={selectedCountries}
                onChange={setSelectedCountries}
                availableBuiltins={availableBuiltins}
              />
            )}
            {selectedCountries !== null && !hasCountries && (
              <p className="text-[10px] text-warning pt-1.5">
                Select at least one jurisdiction to run an analysis.
              </p>
            )}
          </div>
        </div>

        {/* Submit */}
        <motion.button
          type="submit"
          disabled={submitting || !canSubmit}
          whileHover={{ scale: 1.01 }}
          whileTap={{ scale: 0.98 }}
          className="mt-5 w-full py-4 rounded-2xl bg-accent text-white font-display font-bold text-base
                     shadow-glow hover:shadow-lifted transition-all
                     disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none"
        >
          {submitting ? 'Starting…' : 'Run Analysis'}
        </motion.button>

        <p className="text-center text-xs text-muted/60 mt-4">
          {totalCountries > 0
            ? `Scanning ${totalCountries} jurisdiction${totalCountries === 1 ? '' : 's'}`
            : 'Pick one or more jurisdictions to scan'}
        </p>
      </form>
    </motion.div>
  );
}
