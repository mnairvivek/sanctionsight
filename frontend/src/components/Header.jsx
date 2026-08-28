import React, { useState } from 'react';
import { Shield, ArrowLeft, LogOut, Inbox } from 'lucide-react';
import { motion } from 'framer-motion';
import { useAuth } from '../auth';
import AnalystQueue from './AnalystQueue';

export default function Header({ onReset, showBack, onOpenJob }) {
  const { user, logout } = useAuth();
  const [queueOpen, setQueueOpen] = useState(false);

  return (
    <>
      <header className="sticky top-0 z-50 backdrop-blur-xl bg-primary/95 border-b border-white/5">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            {showBack && (
              <motion.button
                initial={{ opacity: 0, x: -12 }}
                animate={{ opacity: 1, x: 0 }}
                onClick={onReset}
                className="p-2 rounded-lg hover:bg-white/10 text-white/70 hover:text-white transition-colors"
              >
                <ArrowLeft size={18} />
              </motion.button>
            )}
            <Shield size={22} className="text-accent" />
            <div>
              <h1 className="text-white font-display font-bold text-base tracking-tight leading-none">
                SanctionSight
              </h1>
              <span className="text-white/40 text-[10px] font-mono tracking-widest uppercase">
                Investigator Workspace
              </span>
            </div>
          </div>

          {user && (
            <div className="flex items-center gap-3 text-xs">
              <button
                onClick={() => setQueueOpen(true)}
                title="My queue"
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg hover:bg-white/10 text-white/80 hover:text-white transition-colors"
              >
                <Inbox size={14} />
                <span className="hidden sm:inline text-[11px] font-medium">Queue</span>
              </button>
              <div className="text-right leading-tight">
                <div className="text-white/90 font-medium">{user.email}</div>
                <div className="text-white/40 font-mono uppercase tracking-wider text-[10px]">
                  {user.role}
                </div>
              </div>
              <button
                onClick={logout}
                title="Sign out"
                className="p-2 rounded-lg hover:bg-white/10 text-white/60 hover:text-white transition-colors"
              >
                <LogOut size={16} />
              </button>
            </div>
          )}
        </div>
      </header>

      {queueOpen && (
        <AnalystQueue
          onClose={() => setQueueOpen(false)}
          onOpenJob={(jid) => {
            setQueueOpen(false);
            onOpenJob?.(jid);
          }}
        />
      )}
    </>
  );
}
