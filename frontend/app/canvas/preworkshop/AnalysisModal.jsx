'use client';

import { useState } from 'react';
import { Icon } from '../../lib/icons';

// The Pre-Workshop Analysis scorecard — the machine-readable half of the
// 'analyze' agent's output (analysis_json on the generated doc; the full
// prose document lives in the normal DocumentViewer via "Open full
// document"). Gaps are ACTIONABLE, not just reported: a 'research' gap
// fires deepresearch with its topic (onResearch — the dashboard wires
// this into the real Run Deep Research flow, so the Research Chain
// animates as usual); an 'ask_client' gap copies its question to the
// clipboard for the workshop agenda; 'request_document' is a label —
// there's no client-facing channel to send a request through yet, so it
// honestly stays a to-do for the BA.
const RESOLUTION_GROUPS = [
  { key: 'research', title: 'Resolve by research', hint: 'The web can answer these — one click runs the research agent.' },
  { key: 'ask_client', title: 'Ask the client', hint: 'Only the client can answer these — copy into your workshop agenda.' },
  { key: 'request_document', title: 'Request a document', hint: 'A missing artifact — ask the client to share it.' },
];

function scoreTone(score) {
  return score < 40 ? 'low' : score < 70 ? 'mid' : 'high';
}

export default function AnalysisModal({ name, analysis, onClose, onResearch, onOpenDoc }) {
  const [copiedIdx, setCopiedIdx] = useState(null);
  const readiness = analysis.readiness || [];
  const gaps = analysis.gaps || [];
  const avg = readiness.length ? Math.round(readiness.reduce((a, r) => a + r.score, 0) / readiness.length) : null;

  async function copyQuestion(idx, text) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedIdx(idx);
      setTimeout(() => setCopiedIdx((c) => (c === idx ? null : c)), 1600);
    } catch { /* clipboard blocked — the text is visible to copy manually */ }
  }

  return (
    <div className="pw-modal-backdrop" onClick={onClose}>
      <div className="pw-modal pw-modal-analysis" onClick={(e) => e.stopPropagation()}>
        <div className="pw-modal-head">
          <div>
            <span className="pw-modal-title">{name || 'Pre-Workshop Analysis'}</span>
            {avg != null && <div className="pw-analysis-avg">Overall readiness: <b>{avg}%</b></div>}
          </div>
          {onOpenDoc && (
            <button className="btn" onClick={onOpenDoc}><Icon name="doc-text" />Open full document</button>
          )}
          <button className="pw-modal-close" onClick={onClose} title="Close"><Icon name="x" /></button>
        </div>

        <div className="pw-modal-body pw-analysis-body">
          {readiness.length > 0 && (
            <section className="pw-analysis-section">
              <div className="pw-analysis-h">Readiness scorecard</div>
              {readiness.map((r) => (
                <div className="pw-score-row" key={r.dimension}>
                  <div className="pw-score-lbl">{r.dimension}</div>
                  <div className="pw-score-track">
                    <div className={`pw-score-fill pw-score-${scoreTone(r.score)}`} style={{ width: `${r.score}%` }} />
                  </div>
                  <div className="pw-score-num">{r.score}%</div>
                  {r.note && <div className="pw-score-note">{r.note}</div>}
                </div>
              ))}
            </section>
          )}

          {RESOLUTION_GROUPS.map((g) => {
            const items = gaps.filter((x) => x.resolution === g.key);
            if (!items.length) return null;
            return (
              <section className="pw-analysis-section" key={g.key}>
                <div className="pw-analysis-h">{g.title} <span className="pw-analysis-count">{items.length}</span></div>
                <div className="pw-analysis-hint">{g.hint}</div>
                {items.map((gap, i) => {
                  const idx = `${g.key}-${i}`;
                  return (
                    <div className="pw-gap-row" key={idx}>
                      <span className={`pw-gap-dot pw-gap-${gap.severity}`} title={`${gap.severity} severity`} />
                      <div className="pw-gap-main">
                        <div className="pw-gap-desc">
                          <span className="pw-tag pw-gap-area">{gap.area}</span>{gap.description}
                        </div>
                        {gap.suggested_action && <div className="pw-gap-action">{gap.suggested_action}</div>}
                      </div>
                      {g.key === 'research' && onResearch && (
                        <button className="btn solid pw-gap-btn"
                          onClick={() => onResearch(gap.suggested_action || gap.description)}>
                          <Icon name="sparkles" />Research this
                        </button>
                      )}
                      {g.key === 'ask_client' && (
                        <button className="btn pw-gap-btn"
                          onClick={() => copyQuestion(idx, gap.suggested_action || gap.description)}>
                          <Icon name="check" />{copiedIdx === idx ? 'Copied' : 'Copy question'}
                        </button>
                      )}
                    </div>
                  );
                })}
              </section>
            );
          })}

          {gaps.length === 0 && readiness.length === 0 && (
            <div className="pw-empty">This analysis has no structured scorecard — open the full document instead.</div>
          )}
        </div>
      </div>
    </div>
  );
}
