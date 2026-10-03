const fs = require('fs');
const path = require('path');

const root = __dirname;
const input = path.join(root, 'BTC_LRA_DOMINANCE_EPISODES.jsonl');
const output = path.join(root, 'BTC_LRA_DOMINANCE_EPISODES.md');
const records = fs.readFileSync(input, 'utf8').split(/\r?\n/).filter(Boolean).map(JSON.parse);
const f = v => v == null ? '—' : Number(v).toFixed(1);
const signed = v => v == null ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(1)}`;
const pctPair = x => `${f(x.buy_pct)} / ${f(x.sell_pct)}`;
const lines = [
  '# BTC-LRA F/E/R Dominance Benchmark Episodes',
  '',
  'Primary source: `BTC_LRA_DOMINANCE_EPISODES.jsonl`. This report is generated from that JSONL schema.',
  '',
  '| Episode | Status | Range (Panama) | F BUY/SELL | E BUY/SELL | R BUY/SELL | OI FLOW BTC | Events | Price Δ USDT | E−R SELL pp | 5m / 15m / 30m / 60m close |',
  '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|',
];
for (const r of records) {
  const c = r.causal_observation;
  const o = r.outcome;
  const range = `${r.range.start.replace('T', ' ').replace('-05:00', '')} → ${r.range.end.slice(11, 16)}`;
  lines.push(`| ${r.episode_id} | ${r.status} | ${range} | ${pctPair(c.F)} | ${pctPair(c.E)} | ${pctPair(c.R)} | ${signed(c.oi_flow_btc)} | ${c.event_count} | ${signed(c.price_change_usdt)} | ${signed(c.E_SELL_MINUS_R_SELL)} | ${[o['5m'], o['15m'], o['30m'], o['60m']].map(x => f(x.price_at_horizon)).join(' / ')} |`);
}
lines.push('', '## Causal observation', '', 'The causal section records only information available inside the selected range. `F`, `E`, and `R` are full, event, and rest partitions respectively. Frozen-reference changes are relative percentage changes, not percentage-point changes.', '', '## Outcome', '', 'Outcome fields are calculated after the selected range at 5m, 15m, 30m, and 60m. They are descriptive future measurements and are not used for causal-state classification.', '', '## Interpretation guardrails', '', 'Benchmark evidence only; opposite-side and control cases are required. The episode does not establish whales, retail, confirmed absorption, short accumulation, market-maker activity, or a bullish signal.', '');
fs.writeFileSync(output, lines.join('\n'), 'utf8');
