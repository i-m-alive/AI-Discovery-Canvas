'use client';

import { useState } from 'react';

// Cumulative value vs delivery cost over the ROI horizon — inline SVG,
// no charting dependency (precedent: DiagramCanvas renders the process
// flows natively). Two series, so a legend is always present and both
// lines are direct-labeled at their end; colors come from the pp-chart
// CSS classes (light/dark steps validated with the dataviz palette
// checker: #6d5ce8+#b3781a on white, #8b7cf0+#b98432 on the dark
// panel). Hover shows a per-period tooltip; identity is never
// color-alone (legend + end labels + the tooltip's text).

const W = 560, H = 240;
const PAD = { top: 16, right: 84, bottom: 30, left: 44 };

export default function RoiChart({ series, currency = '£' }) {
  const [hover, setHover] = useState(null);   // index into series
  if (!series || series.length === 0) return null;

  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const maxY = Math.max(...series.map((p) => Math.max(p.cumulative_value, p.cumulative_cost)), 0.1);
  // 4 gridlines on a rounded-up scale so the top line isn't glued to the frame.
  const yTop = maxY * 1.15;
  const x = (i) => PAD.left + (series.length === 1 ? plotW / 2 : (i / (series.length - 1)) * plotW);
  const y = (v) => PAD.top + plotH - (v / yTop) * plotH;

  const path = (key) => series.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${y(p[key])}`).join(' ');
  const areaPath = `${path('cumulative_value')} L${x(series.length - 1)},${y(0)} L${x(0)},${y(0)} Z`;
  const ticks = [0.25, 0.5, 0.75, 1].map((f) => +(yTop * f).toFixed(1));
  const last = series.length - 1;

  return (
    <div className="pp-chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="pp-roi-svg" role="img"
        aria-label={`Cumulative value versus delivery cost, ${currency} millions, over ${series.length} periods`}>
        <defs>
          <linearGradient id="pp-roi-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" className="pp-grad-top" />
            <stop offset="100%" className="pp-grad-bot" />
          </linearGradient>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} className="pp-grid" />
            <text x={PAD.left - 7} y={y(t) + 3.5} className="pp-axis-txt" textAnchor="end">{t}</text>
          </g>
        ))}
        <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} className="pp-axis" />
        <path d={areaPath} fill="url(#pp-roi-fill)" />
        <path d={path('cumulative_value')} className="pp-line-value" />
        <path d={path('cumulative_cost')} className="pp-line-cost" />
        {series.map((p, i) => (
          <g key={p.period}>
            {/* hover hit target — much bigger than the mark */}
            <rect x={x(i) - plotW / (2 * series.length)} y={PAD.top}
              width={plotW / series.length} height={plotH} fill="transparent"
              onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} />
            <circle cx={x(i)} cy={y(p.cumulative_value)} r={hover === i ? 5 : 3.5}
              className="pp-dot-value" pointerEvents="none" />
            <circle cx={x(i)} cy={y(p.cumulative_cost)} r={hover === i ? 5 : 3.5}
              className="pp-dot-cost" pointerEvents="none" />
            <text x={x(i)} y={H - 10} className="pp-axis-txt" textAnchor="middle">{p.period}</text>
          </g>
        ))}
        {/* direct end labels — series identity is never color-alone */}
        <text x={x(last) + 9} y={y(series[last].cumulative_value) + 3.5} className="pp-lbl-value">Value</text>
        <text x={x(last) + 9} y={y(series[last].cumulative_cost) + 3.5} className="pp-lbl-cost">Cost</text>
        {hover != null && (
          <g pointerEvents="none">
            <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + plotH} className="pp-crosshair" />
            <g transform={`translate(${Math.min(x(hover) + 10, W - 160)}, ${PAD.top + 6})`}>
              <rect width="150" height="52" rx="8" className="pp-tip-bg" />
              <text x="10" y="16" className="pp-tip-title">{series[hover].period}</text>
              <circle cx="14" cy="29" r="3.5" className="pp-dot-value" />
              <text x="23" y="33" className="pp-tip-txt">Value {currency}{series[hover].cumulative_value}M</text>
              <circle cx="14" cy="43" r="3.5" className="pp-dot-cost" />
              <text x="23" y="47" className="pp-tip-txt">Cost {currency}{series[hover].cumulative_cost}M</text>
            </g>
          </g>
        )}
      </svg>
      <div className="pp-legend">
        <span className="pp-legend-item"><i className="pp-swatch pp-swatch-value" />Cumulative value</span>
        <span className="pp-legend-item"><i className="pp-swatch pp-swatch-cost" />Delivery cost</span>
        <span className="pp-legend-unit">{currency}M, cumulative</span>
      </div>
    </div>
  );
}
