import React, { useEffect, useState, useRef } from 'react';
import { motion } from 'framer-motion';
import { Loader2, CheckCircle, AlertTriangle } from 'lucide-react';

const ENTITIES = ['Iran', 'Syria', 'North Korea', 'Cuba', 'Luhansk', 'Donetsk', 'Crimea', 'Ukraine', 'Russia', 'Belarus', 'Myanmar', 'Venezuela'];

export default function ProgressView({ jobId, onComplete, onError }) {
  const [progress, setProgress] = useState(0);
  const [currentStep, setCurrentStep] = useState('Connecting…');
  const [status, setStatus] = useState('pending');
  const [steps, setSteps] = useState([]);
  const seenSteps = useRef(new Set());

  useEffect(() => {
    const evtSource = new EventSource(`/api/stream/${jobId}`);

    evtSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setProgress(data.progress || 0);
        setCurrentStep(data.current_step || '');
        setStatus(data.status);

        // Track completed steps
        if (data.current_step && !seenSteps.current.has(data.current_step)) {
          seenSteps.current.add(data.current_step);
          setSteps((prev) => [...prev, { label: data.current_step, time: new Date() }]);
        }

        if (data.status === 'completed') {
          evtSource.close();
          setTimeout(() => onComplete(jobId), 600);
        }
        if (data.status === 'failed') {
          evtSource.close();
          onError(data.error || 'Analysis failed');
        }
      } catch { /* ignore parse errors */ }
    };

    evtSource.onerror = () => {
      evtSource.close();
      // Try polling as fallback
      const poll = setInterval(async () => {
        try {
          const res = await fetch(`/api/status/${jobId}`);
          const data = await res.json();
          setProgress(data.progress || 0);
          setCurrentStep(data.current_step || '');
          setStatus(data.status);
          if (data.status === 'completed') { clearInterval(poll); onComplete(jobId); }
          if (data.status === 'failed') { clearInterval(poll); onError(data.error); }
        } catch { /* continue polling */ }
      }, 2000);
      return () => clearInterval(poll);
    };

    return () => evtSource.close();
  }, [jobId]);

  const activeEntity = ENTITIES.find((e) => currentStep.includes(e));

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -20 }}
      className="pt-16 max-w-lg mx-auto"
    >
      <div className="text-center mb-8">
        <motion.div
          animate={{ rotate: status === 'running' ? 360 : 0 }}
          transition={{ repeat: Infinity, duration: 2, ease: 'linear' }}
          className="inline-block mb-4"
        >
          <Loader2 size={40} className="text-accent" />
        </motion.div>
        <h2 className="font-display font-bold text-2xl text-primary mb-1">
          Gathering evidence
        </h2>
        <p className="text-muted text-sm">{currentStep}</p>
      </div>

      {/* Progress bar */}
      <div className="bg-surface rounded-2xl shadow-card border border-border/50 p-6 mb-6">
        <div className="flex justify-between text-xs font-mono text-muted mb-2">
          <span>Progress</span>
          <span>{Math.round(progress)}%</span>
        </div>
        <div className="h-2 bg-bg rounded-full overflow-hidden">
          <motion.div
            className="h-full bg-gradient-to-r from-accent to-accent/70 rounded-full"
            initial={{ width: 0 }}
            animate={{ width: `${progress}%` }}
            transition={{ duration: 0.5, ease: 'easeOut' }}
          />
        </div>
      </div>

      {/* Entity grid */}
      <div className="bg-surface rounded-2xl shadow-card border border-border/50 p-6">
        <div className="text-xs font-semibold text-muted uppercase tracking-wider mb-3">
          Jurisdictions
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          {ENTITIES.map((entity) => {
            const done = steps.some((s) => s.label.includes(entity) && !currentStep.includes(entity));
            const active = activeEntity === entity;
            return (
              <div
                key={entity}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-all ${
                  active
                    ? 'bg-accent/10 text-accent font-medium'
                    : done
                    ? 'bg-success/5 text-success/80'
                    : 'text-muted/50'
                }`}
              >
                {done ? (
                  <CheckCircle size={14} />
                ) : active ? (
                  <motion.div animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: 1, ease: 'linear' }}>
                    <Loader2 size={14} />
                  </motion.div>
                ) : (
                  <div className="w-3.5 h-3.5 rounded-full border border-current opacity-30" />
                )}
                {entity}
              </div>
            );
          })}
        </div>
      </div>
    </motion.div>
  );
}
