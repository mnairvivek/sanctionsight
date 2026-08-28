import React from 'react';
import { motion } from 'framer-motion';
import {
  PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, Legend,
} from 'recharts';

const RISK_COLORS = {
  HIGH: '#FF5630',
  MEDIUM: '#FFB800',
  LOW: '#00D924',
  MINIMAL: '#697386',
};

const CustomTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="bg-primary text-white px-3 py-2 rounded-lg shadow-lifted text-xs">
      <div className="font-semibold">{d.name || d.country}</div>
      <div>{payload[0].value} result{payload[0].value !== 1 ? 's' : ''}</div>
    </div>
  );
};

export default function RiskCharts({ summary }) {
  if (!summary) return null;

  // Donut data
  const donutData = [
    { name: 'High', value: summary.total_high, color: RISK_COLORS.HIGH },
    { name: 'Medium', value: summary.total_medium, color: RISK_COLORS.MEDIUM },
    { name: 'Low', value: summary.total_low, color: RISK_COLORS.LOW },
    { name: 'Minimal', value: summary.total_minimal, color: RISK_COLORS.MINIMAL },
  ].filter((d) => d.value > 0);

  // Country bar data — defensively drop the legacy "Sanctions/OFAC" pseudo-country
  const countryData = Object.entries(summary.country_breakdown || {})
    .filter(([country]) => country !== 'Sanctions/OFAC')
    .map(([country, stats]) => ({
      country: country.length > 10 ? country.slice(0, 8) + '…' : country,
      fullCountry: country,
      high: stats.high || 0,
      medium: stats.medium || 0,
      low: stats.low || 0,
      minimal: stats.minimal || 0,
      total: stats.search_results || 0,
    }))
    .filter((d) => d.high + d.medium + d.low + d.minimal > 0);

  const totalFindings = donutData.reduce((a, b) => a + b.value, 0);

  return (
    <div className="grid lg:grid-cols-2 gap-6 mb-6">
      {/* Donut chart */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        className="bg-surface rounded-2xl shadow-card border border-border/50 p-6"
      >
        <h3 className="text-xs font-bold text-muted uppercase tracking-wider mb-4">
          Risk Distribution
        </h3>
        {totalFindings > 0 ? (
          <div className="flex items-center gap-6">
            <ResponsiveContainer width="50%" height={180}>
              <PieChart>
                <Pie
                  data={donutData}
                  cx="50%"
                  cy="50%"
                  innerRadius={50}
                  outerRadius={75}
                  paddingAngle={3}
                  dataKey="value"
                  strokeWidth={0}
                >
                  {donutData.map((entry, i) => (
                    <Cell key={i} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip content={<CustomTooltip />} />
              </PieChart>
            </ResponsiveContainer>
            <div className="flex-1 space-y-2">
              {donutData.map((d) => (
                <div key={d.name} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <div className="w-2.5 h-2.5 rounded-full" style={{ background: d.color }} />
                    <span className="text-primary font-medium">{d.name}</span>
                  </div>
                  <span className="font-mono text-muted text-xs">{d.value}</span>
                </div>
              ))}
              <div className="pt-2 border-t border-border/50 flex justify-between text-xs font-semibold">
                <span className="text-primary">Total</span>
                <span className="font-mono text-primary">{totalFindings}</span>
              </div>
            </div>
          </div>
        ) : (
          <div className="h-[180px] flex items-center justify-center text-muted text-sm">
            No findings to display
          </div>
        )}
      </motion.div>

      {/* Country breakdown bar chart */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.3 }}
        className="bg-surface rounded-2xl shadow-card border border-border/50 p-6"
      >
        <h3 className="text-xs font-bold text-muted uppercase tracking-wider mb-4">
          Findings by Jurisdiction
        </h3>
        {countryData.length > 0 ? (
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={countryData} barGap={2}>
              <XAxis
                dataKey="country"
                tick={{ fontSize: 10, fill: '#697386' }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fontSize: 10, fill: '#697386' }}
                axisLine={false}
                tickLine={false}
                allowDecimals={false}
              />
              <Tooltip content={<CustomTooltip />} />
              <Bar dataKey="high" stackId="a" fill={RISK_COLORS.HIGH} radius={[0, 0, 0, 0]} />
              <Bar dataKey="medium" stackId="a" fill={RISK_COLORS.MEDIUM} />
              <Bar dataKey="low" stackId="a" fill={RISK_COLORS.LOW} />
              <Bar dataKey="minimal" stackId="a" fill={RISK_COLORS.MINIMAL} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-[180px] flex items-center justify-center text-muted text-sm">
            No country-level findings
          </div>
        )}
      </motion.div>
    </div>
  );
}
