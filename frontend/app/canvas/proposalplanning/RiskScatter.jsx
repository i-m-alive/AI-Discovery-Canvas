'use client';

import { useState } from 'react';

// Benefit (y) vs delivery & adoption risk (x), 0–100 each — inline SVG
// scatter with quadrant gridlines at 50. Single series (identity comes
// from each point's own label, not color), so no legend box; every
// point is direct-labeled, and hover enlarges the mark + shows the
// evidence note. "Top-left = high benefit, low risk (prioritize)".

const W = 560, H = 300;
const PAD = { top: 18, right: 20, bottom: 44, left: 44 };

export default function RiskScatter({ items }) {
  const [hover, setHover] = useState(null);
  if (!items || items.length === 0) return null;

  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const x = (risk) => PAD.left + (risk / 100) * plotW;
  const y = (benefit) => PAD.top + plotH - (benefit / 100) * plotH;

  return (
    <div className="pp-chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="pp-risk-svg" role="img"
        aria-label={`Benefit versus risk scatter, ${items.length} items`}>
        {[0, 25, 50, 75, 100].map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={PAD.top} y2={PAD.top + plotH}
              className={t === 50 ? 'pp-grid-mid' : 'pp-grid'} />
            <line x1={PAD.left} x2={PAD.left + plotW} y1={y(t)} y2={y(t)}
              className={t === 50 ? 'pp-grid-mid' : 'pp-grid'} />
            <text x={x(t)} y={H - 26} className="pp-axis-txt" textAnchor="middle">{t}</text>
            <text x={PAD.left - 7} y={y(t) + 3.5} className="pp-axis-txt" textAnchor="end">{t}</text>
          </g>
        ))}
        <text x={PAD.left + plotW} y={H - 26} className="pp-axis-lbl" textAnchor="end" dy="14">Risk →</text>
        <text transform={`translate(12, ${PAD.top + plotH / 2}) rotate(-90)`}
          className="pp-axis-lbl" textAnchor="middle">Benefit →</text>
        {items.map((it, i) => (
          <g key={it.label}
            onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
            {/* hit target larger than the mark */}
            <circle cx={x(it.risk)} cy={y(it.benefit)} r="16" fill="transparent" />
            <circle cx={x(it.risk)} cy={y(it.benefit)} r={hover === i ? 10 : 8}
              className="pp-risk-dot" />
            <text x={x(it.risk)} y={y(it.benefit) - 13} className="pp-risk-lbl"
              textAnchor="middle">{it.label}</text>
          </g>
        ))}
        {hover != null && items[hover].note && (
          <g pointerEvents="none">
            <g transform={`translate(${Math.min(x(items[hover].risk) + 14, W - 230)}, ${Math.max(PAD.top, y(items[hover].benefit) - 46)})`}>
              <rect width="220" height="40" rx="8" className="pp-tip-bg" />
              <text x="10" y="16" className="pp-tip-title">{items[hover].label} · B{items[hover].benefit}/R{items[hover].risk}</text>
              <text x="10" y="31" className="pp-tip-txt">{items[hover].note.slice(0, 42)}{items[hover].note.length > 42 ? '…' : ''}</text>
            </g>
          </g>
        )}
      </svg>
      <div className="pp-legend">
        <span className="pp-legend-unit">Top-left = high benefit, low risk (prioritize) · bottom-right = reconsider</span>
      </div>
    </div>
  );
}
