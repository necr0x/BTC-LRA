const fs = require('fs');
const path = require('path');

const root = __dirname;
const input = path.join(root, 'BTC_LRA_DOMINANCE_EPISODES.jsonl');
const output = path.join(root, 'BTC_LRA_DOMINANCE_EPISODES.md');
const records = fs.readFileSync(input, 'utf8').split(/\r?\n/).filter(Boolean).map(JSON.parse);
const f = v => v == null ? '—' : Number(v).toFixed(1);
const signed = v => v == null ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(1)}`;
const pctPair = (buy, sell) => `${f(buy)} / ${f(sell)}`;
const lines = [
  '# BTC-LRA F/E/R Dominance Benchmark Episodes',
  '',
  'Primary source: `BTC_LRA_DOMINANCE_EPISODES.jsonl`. This report is generated from that JSONL schema.',
  '',
  '| EPISODE | STATUS | START | END | F BUY/SELL | E BUY/SELL | R BUY/SELL | E-R DIVERGENCE | OI FLOW | PRICE RESULT DURING | OUTCOME 15M | OUTCOME 30M | OUTCOME 60M | NOTES |',
  '|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|',
];
for (const r of records) {
  const c = r;
  const o = r.outcome;
  const outcome = key => o?.known ? f(o[key]?.price_at_horizon) : 'OPEN';
  const note = r.status === 'LIVE_CANDIDATE' ? 'unfinished; no result' : 'benchmark evidence; comparative validation required';
  lines.push(`| ${r.episode_id} | ${r.status} | ${r.start.replace('T', ' ').replace('-05:00', '')} | ${r.end ? r.end.slice(11, 16) : 'OPEN'} | ${pctPair(c.f_buy_pct, c.f_sell_pct)} | ${pctPair(c.e_buy_pct, c.e_sell_pct)} | ${pctPair(c.r_buy_pct, c.r_sell_pct)} | ${signed(c.e_sell_minus_r_sell_pp)} pp | ${signed(c.oi_flow_btc)} BTC | ${r.price_change_usdt == null ? 'OPEN' : signed(r.price_change_usdt) + ' USDT'} | ${outcome('15m')} | ${outcome('30m')} | ${outcome('60m')} | ${note} |`);
}
lines.push('', '## Causal observation', '', 'The causal section records only information available inside the selected range. `F`, `E`, and `R` are full, event, and rest partitions respectively. Frozen-reference changes are relative percentage changes, not percentage-point changes.', '', '## Outcome', '', 'Outcome fields are calculated after the selected range at 5m, 15m, 30m, and 60m. They are descriptive future measurements and are not used for causal-state classification.', '', '## Interpretation guardrails', '', 'Benchmark evidence only; opposite-side and control cases are required. The episode does not establish whales, retail, confirmed absorption, short accumulation, market-maker activity, or a bullish signal.', '');
fs.writeFileSync(output, lines.join('\n'), 'utf8');
