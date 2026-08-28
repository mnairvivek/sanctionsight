import React, { useState, useRef, useEffect, useMemo } from 'react';
import { X, Plus, Globe2 } from 'lucide-react';

// selected: { builtins: string[], custom: [{name, variations: string[]}] }
// availableBuiltins: full list of server-known built-in countries
export default function CountrySelector({ selected, onChange, availableBuiltins }) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef(null);
  const containerRef = useRef(null);

  const selectedBuiltinSet = useMemo(
    () => new Set(selected?.builtins ?? []),
    [selected],
  );
  const selectedCustomNames = useMemo(
    () => new Set((selected?.custom ?? []).map((c) => c.name.toLowerCase())),
    [selected],
  );

  const q = query.trim();
  const qLower = q.toLowerCase();

  const filteredBuiltins = useMemo(() => {
    const unselected = (availableBuiltins ?? []).filter((c) => !selectedBuiltinSet.has(c));
    if (!q) return unselected;
    return unselected.filter((c) => c.toLowerCase().includes(qLower));
  }, [availableBuiltins, selectedBuiltinSet, q, qLower]);

  const exactBuiltinMatch = useMemo(
    () => (availableBuiltins ?? []).some((c) => c.toLowerCase() === qLower),
    [availableBuiltins, qLower],
  );
  const duplicateCustom = selectedCustomNames.has(qLower);
  // Parse "Name, var1, var2" — first chunk is the name.
  const parsedCustom = useMemo(() => {
    if (!q) return null;
    const parts = q.split(',').map((p) => p.trim()).filter(Boolean);
    if (!parts.length) return null;
    return { name: parts[0], variations: parts.slice(1) };
  }, [q]);
  const canAddCustom = !!parsedCustom && !exactBuiltinMatch && !duplicateCustom;

  // Options shown in the dropdown, in selection order.
  const options = useMemo(() => {
    const opts = filteredBuiltins.map((name) => ({ kind: 'builtin', name }));
    if (canAddCustom) opts.push({ kind: 'custom', parsed: parsedCustom });
    return opts;
  }, [filteredBuiltins, canAddCustom, parsedCustom]);

  useEffect(() => {
    if (activeIdx >= options.length) setActiveIdx(Math.max(0, options.length - 1));
  }, [options.length, activeIdx]);

  // Click-outside closes the dropdown.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const addBuiltin = (name) => {
    if (selectedBuiltinSet.has(name)) return;
    onChange({
      builtins: [...(selected?.builtins ?? []), name],
      custom: selected?.custom ?? [],
    });
    setQuery('');
    setActiveIdx(0);
  };

  const addCustom = (parsed) => {
    if (!parsed?.name) return;
    if (selectedCustomNames.has(parsed.name.toLowerCase())) return;
    onChange({
      builtins: selected?.builtins ?? [],
      custom: [
        ...(selected?.custom ?? []),
        { name: parsed.name, variations: parsed.variations },
      ],
    });
    setQuery('');
    setActiveIdx(0);
  };

  const removeBuiltin = (name) => {
    onChange({
      builtins: (selected?.builtins ?? []).filter((c) => c !== name),
      custom: selected?.custom ?? [],
    });
  };

  const removeCustom = (name) => {
    onChange({
      builtins: selected?.builtins ?? [],
      custom: (selected?.custom ?? []).filter((c) => c.name !== name),
    });
  };

  const removeLast = () => {
    const customs = selected?.custom ?? [];
    const builtins = selected?.builtins ?? [];
    if (customs.length) {
      onChange({ builtins, custom: customs.slice(0, -1) });
    } else if (builtins.length) {
      onChange({ builtins: builtins.slice(0, -1), custom: customs });
    }
  };

  const commitActive = () => {
    const opt = options[activeIdx];
    if (!opt) return;
    if (opt.kind === 'builtin') addBuiltin(opt.name);
    else addCustom(opt.parsed);
  };

  const onKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (options.length) commitActive();
      else if (canAddCustom) addCustom(parsedCustom);
    } else if (e.key === 'Escape') {
      setOpen(false);
    } else if (e.key === 'Backspace' && !query) {
      e.preventDefault();
      removeLast();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActiveIdx((i) => Math.min(options.length - 1, i + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIdx((i) => Math.max(0, i - 1));
    }
  };

  const totalSelected = (selected?.builtins?.length ?? 0) + (selected?.custom?.length ?? 0);

  return (
    <div ref={containerRef} className="relative">
      <label className="block text-xs font-semibold text-muted uppercase tracking-wider mb-1.5">
        Jurisdictions
      </label>

      <div
        className="w-full min-h-[3rem] px-2.5 py-2 bg-bg border border-border rounded-xl
                   focus-within:ring-2 focus-within:ring-accent/30 focus-within:border-accent
                   transition-all"
        onClick={() => {
          inputRef.current?.focus();
          setOpen(true);
        }}
      >
        <div className="flex flex-wrap items-center gap-1.5">
          {(selected?.builtins ?? []).map((name) => (
            <span
              key={`b-${name}`}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg bg-accent/10 text-accent text-xs font-medium"
            >
              {name}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  removeBuiltin(name);
                }}
                className="hover:text-accent/70"
                aria-label={`Remove ${name}`}
              >
                <X size={12} />
              </button>
            </span>
          ))}
          {(selected?.custom ?? []).map((c) => (
            <span
              key={`c-${c.name}`}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg border border-dashed border-accent/40 bg-muted/10 text-primary text-xs font-medium"
              title={
                c.variations?.length
                  ? `Custom · variations: ${c.variations.join(', ')}`
                  : 'Custom country (no variations)'
              }
            >
              <Globe2 size={10} className="opacity-60" />
              {c.name}
              {c.variations?.length > 0 && (
                <span className="text-[10px] text-muted/70">+{c.variations.length}</span>
              )}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  removeCustom(c.name);
                }}
                className="hover:text-primary/70"
                aria-label={`Remove ${c.name}`}
              >
                <X size={12} />
              </button>
            </span>
          ))}
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
              setActiveIdx(0);
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={onKeyDown}
            placeholder={totalSelected ? '' : 'Add a country…'}
            className="flex-1 min-w-[120px] bg-transparent outline-none text-sm text-primary placeholder:text-muted/40 py-1"
          />
        </div>
      </div>

      {open && (options.length > 0 || q) && (
        <div className="absolute z-20 left-0 right-0 mt-1 bg-surface border border-border rounded-xl shadow-lifted max-h-64 overflow-auto">
          {options.map((opt, i) => {
            const isActive = i === activeIdx;
            if (opt.kind === 'builtin') {
              return (
                <button
                  type="button"
                  key={`opt-b-${opt.name}`}
                  onMouseEnter={() => setActiveIdx(i)}
                  onClick={() => addBuiltin(opt.name)}
                  className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between transition-colors
                              ${isActive ? 'bg-accent/10 text-accent' : 'text-primary hover:bg-muted/20'}`}
                >
                  <span>{opt.name}</span>
                  <span className="text-[10px] text-muted/60 uppercase tracking-wider">Built-in</span>
                </button>
              );
            }
            return (
              <button
                type="button"
                key="opt-custom"
                onMouseEnter={() => setActiveIdx(i)}
                onClick={() => addCustom(opt.parsed)}
                className={`w-full text-left px-3 py-2 text-sm flex items-center gap-2 transition-colors border-t border-border/50
                            ${isActive ? 'bg-accent/10 text-accent' : 'text-primary hover:bg-muted/20'}`}
              >
                <Plus size={14} />
                <span>
                  Add custom: <span className="font-medium">{opt.parsed.name}</span>
                  {opt.parsed.variations.length > 0 && (
                    <span className="text-muted/70 text-xs">
                      {' '}
                      · {opt.parsed.variations.length} variation
                      {opt.parsed.variations.length === 1 ? '' : 's'}
                    </span>
                  )}
                </span>
              </button>
            );
          })}
          {options.length === 0 && q && (
            <div className="px-3 py-2 text-xs text-muted">
              Already selected — nothing to add.
            </div>
          )}
        </div>
      )}

      <p className="text-[10px] text-muted/50 pt-1.5">
        Built-ins come with engine-maintained variations. Custom countries can take
        comma-separated synonyms: <span className="font-mono">Zimbabwe, Zimbabwean, Harare, .zw</span>.
      </p>
    </div>
  );
}
