const fs = require('fs/promises');
const path = require('path');

const ROOT = __dirname;
const API = 'https://fapi.binance.com';
const SYMBOL = 'BTCUSDT';
const TZ = 'America/Panama';
const START = Date.parse('2026-09-20T05:00:00.000Z'); // 00:00 Panama UTC-5
const LR_LOW = 83002.9;
const LR_HIGH = 83255.0;

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function getJson(url) {
  for (let attempt = 1; attempt <= 5; attempt++) {
    const res = await fetch(url);
    if (res.ok) return res.json();
    const body = await res.text();
    if (res.status === 429 || res.status >= 500) { await sleep(attempt * 1000); continue; }
    throw new Error(`${res.status} ${url}: ${body.slice(0, 300)}`);
  }
  throw new Error(`request failed: ${url}`);
}

function panama(ms) {
  const p = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false }).formatToParts(new Date(ms));
  const m = Object.fromEntries(p.map(x => [x.type, x.value]));
  return `${m.year}-${m.month}-${m.day} ${m.hour}:${m.minute}:${m.second}`;
}
function num(x) { return x == null || x === '' ? null : Number(x); }
function csv(v) { if (v == null) return ''; const s = String(v); return /[",\n]/.test(s) ? `"${s.replaceAll('"','""')}"` : s; }
function writeCsv(file, headers, rows) {
  return fs.writeFile(path.join(ROOT, file), [headers.join(','), ...rows.map(r => headers.map(h => csv(r[h])).join(','))].join('\n') + '\n', 'utf8');
}
function signedDelta(k) { return 2 * num(k[9]) - num(k[5]); }

async function fetchKlines(start, end) {
  const out = []; let cursor = start;
  while (cursor < end) {
    const url = `${API}/fapi/v1/klines?symbol=${SYMBOL}&interval=1m&startTime=${cursor}&endTime=${end}&limit=1500`;
    const page = await getJson(url); if (!page.length) break;
    for (const k of page) out.push({
      ts: k[0], open: num(k[1]), high: num(k[2]), low: num(k[3]), close: num(k[4]),
      volume: num(k[5]), takerBuy: num(k[9]), takerSell: num(k[5]) - num(k[9]), delta: signedDelta(k)
    });
    const next = page[page.length - 1][0] + 60000; if (next <= cursor) break; cursor = next;
    await sleep(120);
  }
  return out.filter(x => x.ts >= start && x.ts < end);
}
async function fetchOI(start, end) {
  // Binance's historical OI endpoint returns at most the latest 500 samples
  // for a request range. Walk backward by endTime so the full interval is
  // covered without inventing 1m OI values.
  const out = []; let cursor = end;
  while (cursor > start) {
    const url = `${API}/futures/data/openInterestHist?symbol=${SYMBOL}&period=5m&endTime=${cursor}&limit=500`;
    const page = await getJson(url); if (!page.length) break;
    for (const x of page) out.push({ ts: Number(x.timestamp), oi: num(x.sumOpenInterest), source: 'Binance /futures/data/openInterestHist', interval: '5m' });
    const next = Number(page[0].timestamp) - 300000; if (next >= cursor) break; cursor = next;
    await sleep(150);
  }
  const map = new Map(); for (const x of out) map.set(x.ts, x); return [...map.values()].sort((a,b) => a.ts-b.ts);
}
function addOI(rows, oi) {
  const map = new Map(oi.map(x => [x.ts, x])); let prev = null;
  for (const r of rows) { const x = map.get(r.ts); if (x) { r.oi=x.oi; r.doi=prev == null ? null : x.oi-prev; r.doiPct=prev == null || prev === 0 ? null : (x.oi-prev)/prev*100; prev=x.oi; } else { r.oi=null; r.doi=null; r.doiPct=null; } }
}
function aggregate(rows, minutes) {
  const bucket = new Map(), ms = minutes * 60000;
  for (const r of rows) { const t = Math.floor(r.ts/ms)*ms; if (!bucket.has(t)) bucket.set(t, []); bucket.get(t).push(r); }
  return [...bucket.entries()].sort((a,b)=>a[0]-b[0]).map(([ts, a]) => {
    const oiRows = a.filter(x => x.oi != null); const first=a[0], last=a[a.length-1];
    const vol=a.reduce((s,x)=>s+x.volume,0), delta=a.reduce((s,x)=>s+x.delta,0);
    return { ts, open:first.open, high:Math.max(...a.map(x=>x.high)), low:Math.min(...a.map(x=>x.low)), close:last.close,
      volume:vol, takerBuy:a.reduce((s,x)=>s+x.takerBuy,0), takerSell:a.reduce((s,x)=>s+x.takerSell,0), delta,
      deltaVolumeRatio:vol ? delta/vol : null, oiStart:oiRows.length ? oiRows[0].oi : null, oiEnd:oiRows.length ? oiRows[oiRows.length-1].oi : null,
      doi:oiRows.length > 1 ? oiRows[oiRows.length-1].oi-oiRows[0].oi : null, barCount:a.length };
  });
}
function loc(r) { if (r.high < LR_LOW) return 'BELOW_LR'; if (r.low > LR_HIGH) return 'ABOVE_LR'; if (r.low >= LR_LOW && r.high <= LR_HIGH) return 'INSIDE_LR'; if (r.low < LR_LOW && r.high > LR_HIGH) return 'CROSSES_BOTH'; if (r.low < LR_LOW) return 'CROSSES_LOWER_BOUNDARY'; return 'CROSSES_UPPER_BOUNDARY'; }
function enrich(rows) { return rows.map(r => ({ timestamp_panama:panama(r.ts), timestamp_utc:new Date(r.ts).toISOString(), open:r.open, high:r.high, low:r.low, close:r.close, volume_BTC:r.volume, taker_buy_BTC:r.takerBuy, taker_sell_BTC:r.takerSell, delta_BTC:r.delta, delta_volume_ratio:r.volume ? r.delta/r.volume : null, OI_BTC:r.oi, dOI_BTC:r.doi, dOI_pct:r.doiPct, oi_source:r.oi == null ? null : 'Binance /futures/data/openInterestHist', oi_interval:r.oi == null ? null : '5m', lr_low:LR_LOW, lr_high:LR_HIGH, location:loc(r), distance_to_lr_low:r.close-LR_LOW, distance_to_lr_high:r.close-LR_HIGH })); }
function position(r) { if (r.high < LR_LOW) return 'BELOW_LR'; if (r.low > LR_HIGH) return 'ABOVE_LR'; if (r.low >= LR_LOW && r.high <= LR_HIGH) return 'INSIDE_LR'; return 'TOUCH/CROSS_LR'; }

function describeZones(bars, tf, minutes) {
  const out = [], events = [], step = minutes*60000, window = {5:36,15:24,60:18,240:12}[minutes] || 18;
  let nextCandidateStart = -Infinity;
  for (let i=window-1; i<bars.length; i++) {
    const a=bars.slice(i-window+1,i+1), hi=Math.max(...a.map(x=>x.high)), lo=Math.min(...a.map(x=>x.low)), mid=(hi+lo)/2, width=hi-lo;
    const net=Math.abs(a[a.length-1].close-a[0].open), gross=a.reduce((s,x)=>s+(x.high-x.low),0), vol=a.reduce((s,x)=>s+x.volume,0);
    const ratio=width ? net/width : 1;
    const prior=a[0].open, end=a[a.length-1].close;
    if (ratio <= 0.65 && width > 0 && vol > 0 && a[0].ts >= nextCandidateStart) {
      const start=a[0].ts, zoneId=`${tf}-${start}`; const existing=out.find(z=>z.zone_id===zoneId);
      if (!existing) {
        const z={zone_id:zoneId,timeframe:tf,state:'ACTIVE_BALANCE',start_time:panama(start),start_ts:start,end_time:panama(a[a.length-1].ts),end_ts:a[a.length-1].ts,low:lo,high:hi,mid,width_usd:width,width_pct:width/mid*100,time_inside:window,time_outside:0,total_volume_BTC:vol,volume_per_bar:vol/window,cumulative_delta:a.reduce((s,x)=>s+x.delta,0),OI_start:a.find(x=>x.oiStart!=null)?.oiStart??null,OI_min:null,OI_max:null,OI_current:a.at(-1).oiEnd??null,net_displacement:end-prior,gross_travel_distance:gross,volume_per_net_displacement:Math.abs(end-prior)>0?vol/Math.abs(end-prior):null,zone_activity:{upper:0,middle:0,lower:0},boundary_attacks:0,failed_excursions:0,retention_after_excursions:[],events:[],evidence_end_ts:a.at(-1).ts};
        const o=a.filter(x=>x.high>=lo+width*.66), m=a.filter(x=>x.high>=lo+width*.33&&x.low<=lo+width*.66), l=a.filter(x=>x.low<=lo+width*.33); z.zone_activity={upper:o.length,middle:m.length,lower:l.length}; for(const x of a){for(const q of [x.oiStart,x.oiEnd])if(q!=null){z.OI_min=z.OI_min==null?q:Math.min(z.OI_min,q);z.OI_max=z.OI_max==null?q:Math.max(z.OI_max,q);}}
        out.push(z); nextCandidateStart = a.at(-1).ts + step; events.push({event:'BALANCE_ACTIVE',zone_id:zoneId,timeframe:tf,event_time:z.end_time,price:end,available_at:end,observed:{low:lo,high:hi,volume:vol,net_displacement:end-prior,gross_travel:gross}});
      }
    }
  }
  // Extend each descriptive candidate forward using only bars in chronological order.
  for (const z of out) {
    let departed=false, returns=0, lastTs=z.end_ts;
    const after=bars.filter(x=>x.ts>z.end_ts);
    for (const b of after) {
      lastTs=b.ts; const outsideUp=b.close>z.high, outsideDn=b.close<z.low, inside=b.high>=z.low&&b.low<=z.high;
      if (!departed && (outsideUp||outsideDn)) { departed=true; z.state=outsideUp?'DEPARTED_UP':'DEPARTED_DOWN'; z.departure_time=panama(b.ts); z.departure_ts=b.ts; z.departure_price=b.close; z.events.push({event:z.state,time:panama(b.ts),price:b.close,available_at:panama(b.ts)}); events.push({event:z.state,zone_id:z.zone_id,timeframe:z.timeframe,event_time:panama(b.ts),price:b.close,available_at:panama(b.ts)}); }
      if (departed && inside) { returns++; z.state=returns===1?'FIRST_RETURN':'RETESTED'; z.return_count=returns; z.first_return_time=z.first_return_time||panama(b.ts); z.events.push({event:returns===1?'FIRST_RETURN':'RETESTED',time:panama(b.ts),price:b.close,penetration_depth:Math.max(0,Math.min(z.high,b.high)-Math.max(z.low,b.low)),available_at:panama(b.ts)}); }
      if (departed) { z.post_departure_volume_BTC=(z.post_departure_volume_BTC||0)+b.volume; if(b.oiEnd!=null)z.post_departure_OI_current=b.oiEnd; z.turnover_through_old_zone_BTC=(z.turnover_through_old_zone_BTC||0)+(inside?b.volume:0); z.fully_traversed=z.fully_traversed|| (b.low<=z.low&&b.high>=z.high); }
    }
    z.last_observed_time=panama(lastTs); z.events_count=z.events.length; if (z.state==='ACTIVE_BALANCE' && after.length) z.state='STALE'; if(z.return_count>1 && z.fully_traversed) z.state='CONSUMED';
  }
  // Keep a non-overlapping descriptive sequence within each timeframe. This
  // prevents every sliding window from becoming a duplicate zone while still
  // allowing nested zones across different timeframes.
  const compact = [];
  for (const z of out.sort((a,b) => a.start_ts - b.start_ts)) {
    const last = compact.filter(x => x.timeframe === z.timeframe).at(-1);
    if (!last || z.start_ts >= last.evidence_end_ts) compact.push(z);
  }
  const ids = new Set(compact.map(z => z.zone_id));
  return {zones:compact,events:events.filter(e => !e.zone_id || ids.has(e.zone_id))};
}

async function main() {
  const now=(await getJson(`${API}/fapi/v1/klines?symbol=${SYMBOL}&interval=1m&limit=2`)).at(-1)[0];
  const end=Math.floor((Number(now)-60000)/60000)*60000+60000;
  const rows=await fetchKlines(START,end), oi=await fetchOI(START,end); addOI(rows,oi);
  const one=enrich(rows), aggregates={}; for(const [n,m] of [['5M',5],['15M',15],['1H',60],['4H',240]]) aggregates[n]=aggregate(rows,m);
  const headers=['timestamp_panama','timestamp_utc','open','high','low','close','volume_BTC','taker_buy_BTC','taker_sell_BTC','delta_BTC','delta_volume_ratio','OI_BTC','dOI_BTC','dOI_pct','oi_source','oi_interval','lr_low','lr_high','location','distance_to_lr_low','distance_to_lr_high'];
  await writeCsv('BTC_LRA_MASTER_20260920_NOW_1M.csv',headers,one);
  for(const [n,a] of Object.entries(aggregates)){ const h=['timestamp_panama','timestamp_utc','open','high','low','close','volume_BTC','taker_buy_BTC','taker_sell_BTC','delta_BTC','delta_volume_ratio','OI_start_BTC','OI_end_BTC','dOI_BTC','bar_count','position_relative_to_LR']; await writeCsv(`BTC_LRA_MASTER_20260920_NOW_${n}.csv`,h,a.map(x=>({timestamp_panama:panama(x.ts),timestamp_utc:new Date(x.ts).toISOString(),open:x.open,high:x.high,low:x.low,close:x.close,volume_BTC:x.volume,taker_buy_BTC:x.takerBuy,taker_sell_BTC:x.takerSell,delta_BTC:x.delta,delta_volume_ratio:x.deltaVolumeRatio,OI_start_BTC:x.oiStart,OI_end_BTC:x.oiEnd,dOI_BTC:x.doi,bar_count:x.barCount,position_relative_to_LR:position(x)}))); }
  const allZones=[],allEvents=[]; for(const [n,m] of [['5M',5],['15M',15],['1H',60],['4H',240]]){ const z=describeZones(aggregates[n],n,m); allZones.push(...z.zones); allEvents.push(...z.events); }
  const memory=allZones.map(z=>({...z,observed_data_only:true,interpretation:null,inference:null}));
  await fs.writeFile(path.join(ROOT,'BTC_LRA_MARKET_RISK_MEMORY.jsonl'),memory.map(x=>JSON.stringify(x)).join('\n')+'\n','utf8');
  await fs.writeFile(path.join(ROOT,'BTC_LRA_ZONE_EVENTS.jsonl'),allEvents.map(x=>JSON.stringify(x)).join('\n')+'\n','utf8');
  await fs.writeFile(path.join(ROOT,'BTC_LRA_ZONE_STATE.json'),JSON.stringify({generated_at:new Date().toISOString(),timezone:TZ,source:'Binance Futures',zones:memory},null,2)+'\n','utf8');
  const missing=[]; for(let i=1;i<rows.length;i++)if(rows[i].ts-rows[i-1].ts!==60000)missing.push({from:panama(rows[i-1].ts),to:panama(rows[i].ts),missing_minutes:(rows[i].ts-rows[i-1].ts)/60000-1});
  const start=panama(rows[0].ts), finish=panama(rows.at(-1).ts), zoneCounts=Object.fromEntries(['5M','15M','1H','4H'].map(tf=>[tf,memory.filter(z=>z.timeframe===tf).length]));
  const chronology = allEvents.sort((a,b)=>String(a.event_time).localeCompare(String(b.event_time))).map(e=>`- ${e.event_time} | ${e.timeframe} | ${e.event} | zone=${e.zone_id} | price=${e.price} | available_at=${e.available_at}`).join('\n');
  const zoneTable = memory.map(z=>`- ${z.zone_id} | ${z.timeframe} | ${z.start_time} -> ${z.last_observed_time} | ${z.low}..${z.high} | vol=${z.total_volume_BTC} BTC | delta=${z.cumulative_delta} | state=${z.state} | departure=${z.departure_time||'NONE'} | first_return=${z.first_return_time||'NONE'} | retests=${z.return_count||0} | post_departure_vol=${z.post_departure_volume_BTC||0}`).join('\n');
  const lrCandidates = memory.filter(z => z.low <= LR_LOW && z.high >= LR_HIGH).map(z=>`${z.zone_id} ${z.timeframe} ${z.start_time} ${z.low}..${z.high}`).join('\n') || 'No automatically detected candidate fully contains the fixed LR bounds.';
  /*
  const md=`# BTC-LRA market risk memory research\n\n## Scope\n- Observed period: **${start} → ${finish} Panama UTC-5**\n- Source: Binance USDⓈ-M Futures public REST API, ${SYMBOL}.\n- 1m OHLCV/taker data: \\`/fapi/v1/klines\\`; historical OI: \\`/futures/data/openInterestHist\\`, actual **5m** resolution; OI is not interpolated into 1m.\n- 1m bars: **${rows.length}**; missing intervals: **${missing.length}**.\n- Research layer is replay-only and does not change \\`btc-lra-001.py\\`, detectors, thresholds, human output, or trading decisions.\n\n## Dataset audit\n- 5m/15m/1h/4h are aggregates of the same 1m source rows.\n- 1m OI fields are populated only on source 5m timestamps; other rows remain blank.\n- Zone candidates by timeframe: ${JSON.stringify(zoneCounts)}.\n${missing.length?`- Missing 1m intervals: ${JSON.stringify(missing.slice(0,20))}`:'- Continuity: no missing 1m intervals detected.'}\n\n## Walk-forward descriptive memory\nZones are deterministic descriptive candidates built from completed bars only. They retain their records after departure and record departure, returns/retests, turnover through the old area, post-departure volume/OI observations, and full traversal flags. No trapped-inventory, market-maker, absorption, advantage-transfer, or trading classification is asserted.\n\nStates used: \\`FORMING → ACTIVE_BALANCE → DEPARTED_UP/DOWN → FIRST_RETURN → RETESTED → CONSUMED/STALE\\`.\n\n## Chronology\nThe raw chronological evidence is in the master CSVs. Zone event timestamps and available-at timestamps are in \\`BTC_LRA_ZONE_EVENTS.jsonl\\`; each event uses only bars at or before its timestamp. Future bars are not used for state transitions.\n\n## Important limitation\nThis first replay records measurable price/volume/delta/OI behavior and descriptive zones. It does not infer who is trapped, who is market maker inventory, or whether any release is a valid entry.\n`;
  */
  const md = [
    '# BTC-LRA market risk memory research', '', '## Scope',
    '- Observed period: **' + start + ' -> ' + finish + ' Panama UTC-5**',
    '- Source: Binance USD-M Futures public REST API, ' + SYMBOL + '.',
    '- 1m OHLCV/taker data: /fapi/v1/klines; historical OI: /futures/data/openInterestHist, actual 5m resolution; OI is not interpolated into 1m.',
    '- 1m bars: **' + rows.length + '**; missing intervals: **' + missing.length + '**.',
    '- Research layer is replay-only and does not change btc-lra-001.py, detectors, thresholds, human output, or trading decisions.', '',
    '## Dataset audit', '- 5m/15m/1h/4h are aggregates of the same 1m source rows.',
    '- 1m OI fields are populated only on source 5m timestamps; other rows remain blank.',
    '- Zone candidates by timeframe: ' + JSON.stringify(zoneCounts) + '.',
    missing.length ? '- Missing 1m intervals: ' + JSON.stringify(missing.slice(0,20)) : '- Continuity: no missing 1m intervals detected.', '',
    '## Walk-forward descriptive memory',
    'Zones are deterministic descriptive candidates built from completed bars only. They retain their records after departure and record departure, returns/retests, turnover through the old area, post-departure volume/OI observations, and full traversal flags. No trapped-inventory, market-maker, absorption, advantage-transfer, or trading classification is asserted.', '',
    'States used: FORMING -> ACTIVE_BALANCE -> DEPARTED_UP/DOWN -> FIRST_RETURN -> RETESTED -> CONSUMED/STALE.', '',
    '## Chronology', 'The raw chronological evidence is in the master CSVs. Zone event timestamps and available-at timestamps are in BTC_LRA_ZONE_EVENTS.jsonl; each event uses only bars at or before its timestamp. Future bars are not used for state transitions.', chronology, '',
    '## Zone inventory', zoneTable, '',
    '## Fixed CASE2 benchmark cross-check', 'The fixed LR values are used here only as an audit reference, not as detector input. Automatically detected candidates containing both bounds:', lrCandidates, '',
    '## Observed data', 'Price, OHLCV, taker flow, delta and actual 5m OI samples are recorded in the master CSVs. Zone records preserve descriptive balance metrics, departure/return/retest observations, turnover through the old area and post-departure OI/volume summaries.', '',
    '## Inference', 'No trading inference is made by this replay. A departure is a geometric state transition; it is not labeled as a reversal, trapped inventory, or control transfer.', '',
    '## Hypothesis', 'Future research may test whether repeated loss of retained result plus opposite-side retained reward coincides with risk migration. This output does not score or decide that hypothesis.', '',
    '## Important limitation', 'This first replay records measurable price/volume/delta/OI behavior and descriptive zones. It does not infer who is trapped, who is market maker inventory, or whether any release is a valid entry.', ''
  ].join('\n');
  await fs.writeFile(path.join(ROOT,'BTC_LRA_MASTER_ANALYSIS_20260920_NOW.md'),md,'utf8');
  console.log(JSON.stringify({start,finish,oneMin:rows.length,oiSamples:oi.length,missing1m:missing.length,zones:zoneCounts},null,2));
}
main().catch(err=>{console.error(err.stack||err);process.exitCode=1;});
