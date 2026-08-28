import React, { useState, useCallback } from 'react';
import { AnimatePresence } from 'framer-motion';
import InputForm from './components/InputForm';
import ProgressView from './components/ProgressView';
import Dashboard from './components/Dashboard';
import Header from './components/Header';
import LoginScreen from './components/LoginScreen';
import { useAuth } from './auth';

const API_BASE = '/api';

export default function App() {
  const { isAuthed, authedFetch } = useAuth();
  const [view, setView] = useState('input'); // input | progress | dashboard
  const [jobId, setJobId] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const handleSubmit = useCallback(async (formData) => {
    setError(null);
    try {
      const res = await authedFetch(`${API_BASE}/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData),
      });
      if (!res.ok) throw new Error('Failed to start analysis');
      const { job_id } = await res.json();
      setJobId(job_id);
      setView('progress');
    } catch (err) {
      setError(err.message);
    }
  }, [authedFetch]);

  const handleComplete = useCallback(async (jid) => {
    try {
      const res = await authedFetch(`${API_BASE}/result/${jid}`);
      if (!res.ok) throw new Error('Failed to fetch results');
      const data = await res.json();
      setResult({ ...data, _jobId: jid });
      setView('dashboard');
    } catch (err) {
      setError(err.message);
      setView('input');
    }
  }, [authedFetch]);

  const handleReset = useCallback(() => {
    setView('input');
    setJobId(null);
    setResult(null);
    setError(null);
  }, []);

  if (!isAuthed) {
    return <LoginScreen />;
  }

  return (
    <div className="min-h-screen bg-bg">
      <Header onReset={handleReset} showBack={view !== 'input'} onOpenJob={handleComplete} />

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pb-16">
        {error && (
          <div className="mb-6 p-4 bg-danger/10 border border-danger/20 rounded-xl text-danger text-sm font-medium">
            {error}
            <button onClick={() => setError(null)} className="ml-3 underline">Dismiss</button>
          </div>
        )}

        <AnimatePresence mode="wait">
          {view === 'input' && (
            <InputForm key="input" onSubmit={handleSubmit} />
          )}
          {view === 'progress' && jobId && (
            <ProgressView
              key="progress"
              jobId={jobId}
              onComplete={handleComplete}
              onError={(msg) => { setError(msg); setView('input'); }}
            />
          )}
          {view === 'dashboard' && result && (
            <Dashboard key="dashboard" data={result} />
          )}
        </AnimatePresence>
      </main>

      <footer className="text-center py-8 text-muted text-xs font-body">
        Automated NLP evidence-gathering — analyst disposition is required before any case closes.
      </footer>
    </div>
  );
}
