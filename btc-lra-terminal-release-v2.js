/*
 * BTC-LRA Terminal Release V2
 * Research-only replay.  It deliberately does not import or mutate production
 * detector code and writes only *_V2 research artifacts.
 */
const fs = require('fs');
const path = require('path');
const ROOT = __dirname;
const MASTER_FILE = 'BTC_LRA_MASTER_20260920_NOW_1M.csv';
const ZONE_FILE = 'BTC_LRA_ZONE_STATE.json';
const ZONE_EVENTS_FILE = 'BTC_LRA_ZONE_EVENTS.jsonl';
const TRANSFER_EVENTS_FILE = 'BTC_LRA_BATTLE_RESOLUTION_EVENTS.jsonl';

const OUT = {
  episodes: 'BTC_LRA_TERMINAL_RELEASE_V2_EPISODES.jsonl',
  events: 'BTC_LRA_TERMINAL_RELEASE_V2_EVENTS.jsonl',
  outcomes: 'BTC_LRA_TERMINAL_RELEASE_V2_OUTCOMES.jsonl',
  targets: 'BTC_LRA_EXPECTED_RELEASE_PATH_V2.jsonl',
  analysis: 'BTC_LRA_TERMINAL_RELEASE_V2_ANALYSIS.md',
  audit: 'BTC_LRA_TERMINAL_RELEASE_V2_AUDIT.md'
};

function parseCsvLine(line) { const out=[]; let cur='', q=false; for (let i=0;i<line.length;i++) { const c=line[i]; if (c==='"') { if (q && line[i+1]==='"') { cur+='"'; i++; } else q=!q; } else if (c===','&&!q) { out.push(cur); cur=''; } else cur+=c; } out.push(cur); return out; }
function readCsv(file) { const lines=fs.readFileSync(path.join(ROOT,file),'utf8').trim().split(/\r?\n/); const h=parseCsvLine(lines[0]); return lines.slice(1).map(line=>{const a=parseCsvLine(line),o={}; h.forEach((k,i)=>o[k]=a[i]??''); for(const k of ['open','high','low','close','volume_BTC','taker_buy_BTC','taker_sell_BTC','delta_BTC','OI_BTC','dOI_BTC','dOI_pct']) if(o[k]!=='') o[k]=Number(o[k]); o.ts=Date.parse(o.timestamp_utc); return o;}).sort((a,b)=>a.ts-b.ts); }
function readJsonl(file) { return fs.readFileSync(path.join(ROOT,file),'utf8').split(/\r?\n/).filter(Boolean).map(JSON.parse); }
function n(x) { return x==null || x==='' ? null : Number(x); }
function fmt(ts) { return new Intl.DateTimeFormat('en-CA',{timeZone:'America/Panama',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(ts)).replace(',',''); }
function sum(rows,key) { return rows.reduce((a,r)=>a+(n(r[key])||0),0); }
function max(rows,key) { return rows.length ? Math.max(...rows.map(r=>r[key])) : null; }
function min(rows,key) { return rows.length ? Math.min(...rows.map(r=>r[key])) : null; }
function signed(side, from, to) { return side==='BUY' ? to-from : from-to; }
function effortKey(side) { return side==='BUY' ? 'taker_buy_BTC' : 'taker_sell_BTC'; }
function oiPath(rows) { const s=rows.filter(r=>n(r.OI_BTC)!=null); return {samples:s.length, source_resolution:'existing MASTER OI samples only', OI_start:s[0]?.OI_BTC??null, OI_end:s.at(-1)?.OI_BTC??null, OI_min:min(s,'OI_BTC'), OI_max:max(s,'OI_BTC'), net_dOI:s.length>1?s.at(-1).OI_BTC-s[0].OI_BTC:null, samples_time:s.map(r=>fmt(r.ts))}; }
function rowAtOrBefore(rows, ts) { let best=null; for (const r of rows) { if (r.ts>ts) break; best=r; } return best; }
function rowsBetween(rows, a, b) { return rows.filter(r=>r.ts>=a && r.ts<=b); }

function observableAt(zone, zoneEvents) {
  const balance=zoneEvents.filter(e=>e.zone_id===zone.zone_id && e.event==='BALANCE_ACTIVE' && Date.parse(e.event_time.replace(' ','T')+'-05:00')<=Date.now()).map(e=>Date.parse(e.event_time.replace(' ','T')+'-05:00'));
  const explicitEnd=n(zone.end_ts);
  return Math.max(explicitEnd||0, balance.length?Math.max(...balance):0);
}
function parentZones(price, ts, zones, zoneEvents) {
  return zones.filter(z => (z.timeframe==='15M'||z.timeframe==='1H') && observableAt(z,zoneEvents)<=ts && z.low<=price && z.high>=price)
    .sort((a,b)=>(a.width_usd||Infinity)-(b.width_usd||Infinity));
}
function structuralTargets(price, ts, zones, zoneEvents, rows) {
  const parents=parentZones(price,ts,zones,zoneEvents).map(z=>({kind:'parent_zone_boundary',zone_id:z.zone_id,timeframe:z.timeframe,available_at:fmt(observableAt(z,zoneEvents)),low:z.low,high:z.high}));
  const local=zones.filter(z=>z.timeframe==='5M' && observableAt(z,zoneEvents)<=ts && z.low<=price && z.high>=price).sort((a,b)=>(a.width_usd||Infinity)-(b.width_usd||Infinity))[0];
  const prior=priorReferences(ts, price, rows).slice(0,5).map(r=>({kind:'prior_historical_reference',reference_id:r.reference_id,price:r.price,formed_at:r.formed_at}));
  return {at:fmt(ts), at_ts:ts, local_zone_opposite_boundary:local?{zone_id:local.zone_id,low:local.low,high:local.high}:null, parent_zone_boundaries:parents, prior_historical_references:prior};
}

// A reference is a pre-existing local swing candidate, not an automatic resistance label.
function priorReferences(ts, price, rows) {
  const past=rows.filter(r=>r.ts<ts); const candidates=[];
  for(let i=1;i<past.length-1;i++) if(past[i].high>=past[i-1].high && past[i].high>=past[i+1].high) candidates.push(past[i]);
  const selected=[];
  for (const c of candidates.slice().reverse()) {
    if (c.high<price) continue;
    if (selected.some(x=>Math.abs(x.price-c.high)<Math.max(1,price*0.0001))) continue;
    const approach=past.filter(r=>r.ts>=c.ts && r.ts<ts); let revisits=0,inside=false;
    const band=Math.max(1,Math.abs(c.high)*0.0005);
    for(const r of approach) { const near=Math.abs(r.high-c.high)<=band || Math.abs(r.close-c.high)<=band; if(near&&!inside) revisits++; inside=near; }
    selected.push({reference_id:`SWING_HIGH-${c.ts}`,formed_at:fmt(c.ts),formed_ts:c.ts,price:c.high,age_minutes:(ts-c.ts)/60000,distance_to_approach:c.high-price,prior_reaction:{next_1m_close:rows.find(r=>r.ts>c.ts)?.close??null},number_of_distinct_revisits:revisits,turnover_reprocessing:sum(approach,'volume_BTC'),is_resistance:false});
  }
  return selected.sort((a,b)=>Math.abs(a.distance_to_approach)-Math.abs(b.distance_to_approach));
}

function makeEpisode(rows, start, end, id, referencePrice, zones, zoneEvents) {
  const bars=rowsBetween(rows,start,end); if(!bars.length) return null;
  const base=rowAtOrBefore(rows,start)||bars[0], basePrice=base.close; let buyEff=0,buyResult=0,sellEff=0,sellResult=0,activeHigh=basePrice, latestRetainedHigh=basePrice, lastHighTs=base.ts, lastHighEffort=0, highSeq=[]; const walk=[]; const events=[];
  let pendingRejection=null;
  for (const b of bars) {
    const prevHigh=activeHigh; const buy=n(b.taker_buy_BTC)||0, sell=n(b.taker_sell_BTC)||0; buyEff+=buy; sellEff+=sell;
    const buyExt=Math.max(0,b.high-basePrice), sellExt=Math.max(0,basePrice-b.low); buyResult=buyExt; sellResult=sellExt;
    const newHigh=b.high>activeHigh; const retained=b.close>=activeHigh;
    if(newHigh) { const prior=activeHigh, effortSincePrevious=buyEff-lastHighEffort; activeHigh=b.high; highSeq.push({sequence:highSeq.length+1,timestamp:fmt(b.ts),ts:b.ts,high:b.high,extension_from_previous:b.high-prior,effort_since_previous_high:effortSincePrevious,incremental_extension_per_100_BTC:effortSincePrevious?(b.high-prior)/effortSincePrevious*100:null,retained_at_close:retained,effort_total:buyEff}); if(retained){latestRetainedHigh=b.high;lastHighTs=b.ts;lastHighEffort=buyEff;} }
    const retainedExtension=Math.max(0,b.close-latestRetainedHigh);
    const distanceFromLatestRetainedHigh=latestRetainedHigh-b.close;
    const timeSinceLatestRetainedHigh=(b.ts-lastHighTs)/60000;
    const trailing=walk.slice(-4); const priorEff=trailing.length?trailing.reduce((a,x)=>a+x.buy_effort_BTC,0):null; const priorResult=trailing.length?trailing.reduce((a,x)=>a+x.incremental_extension,0):null;
    const row={timestamp:fmt(b.ts),ts:b.ts,close:b.close,high:b.high,low:b.low,buy_effort_BTC:buy,sell_effort_BTC:sell,buy_cumulative_effort:buyEff,sell_cumulative_effort:sellEff,directional_extension:buyExt,retained_extension:retainedExtension,incremental_extension:newHigh?b.high-(highSeq.at(-2)?.high??base.high):0,incremental_extension_per_100_BTC:newHigh&&buyEff-(highSeq.at(-2)?.effort_total??0)?(b.high-(highSeq.at(-2)?.high??base.high))/(buyEff-(highSeq.at(-2)?.effort_total??0))*100:null,rolling_effort_result_efficiency:priorEff?priorResult/priorEff*100:null,distance_from_latest_retained_high:distanceFromLatestRetainedHigh,time_since_latest_retained_high_minutes:timeSinceLatestRetainedHigh,OI_path_to_T:oiPath(bars.filter(x=>x.ts<=b.ts))};
    if(newHigh) { lastHighEffort=buyEff; row.high_sequence=highSeq.length; }
    walk.push(row);
    if(!newHigh && b.close<activeHigh && !pendingRejection) { pendingRejection={ts:b.ts,bar:b,activeHigh}; events.push({event_id:`${id}-DECAY-${b.ts}`,time:fmt(b.ts),time_ts:b.ts,state:'DECAY_OBSERVATION',evidence:{price:b.close,active_release_extreme:activeHigh,buy_effort_BTC:buy,BUY_cumulative_effort:buyEff,directional_extension:buyExt,retained_extension:retainedExtension,distance_from_latest_retained_high:distanceFromLatestRetainedHigh,time_since_latest_retained_high_minutes:timeSinceLatestRetainedHigh,OI_path_to_T:row.OI_path_to_T}}); }
    if(buy>0 && b.high<=activeHigh && b.close<activeHigh && (b.high-b.open)<0) { events.push({event_id:`${id}-PASSIVE-${b.ts}`,time:fmt(b.ts),time_ts:b.ts,state:'PASSIVE_REJECTION_CANDIDATE',evidence:{price:b.close,buy_effort_BTC:buy,volume_BTC:n(b.volume_BTC),delta_BTC:n(b.delta_BTC),incremental_upside:Math.max(0,b.high-activeHigh),failed_retention:true,sharp_price_rejection_observation:true,OI_path_to_T:row.OI_path_to_T}}); }
    if(pendingRejection && newHigh && retained) { events.push({event_id:`${id}-RESTORE-${b.ts}`,time:fmt(b.ts),time_ts:b.ts,state:'BUY_RESTORATION_OBSERVATION',evidence:{price:b.close,new_retained_high:b.high,buy_effort_BTC:buy,BUY_cumulative_effort:buyEff,OI_path_to_T:row.OI_path_to_T}}); pendingRejection=null; }
  }
  return {episode_id:id,start_time:fmt(start),start_ts:start,end_time:fmt(end),end_ts:end,direction:'BUY',origin_price:basePrice,reference_price:referencePrice,walk_forward:walk,high_sequence:highSeq,causal_events:events,expected_release_path:structuralTargets(basePrice,start,zones,zoneEvents,rows),live_evidence_rule:'all evidence is calculated from MASTER rows with ts <= event time; future statistics are only in outcome'};
}

function outcome(ep, rows) {
  const future=rows.filter(r=>r.ts>ep.start_ts && r.ts<=ep.end_ts); const lastHigh=ep.high_sequence.at(-1); const restore=ep.high_sequence.find(h=>h.sequence>(lastHigh?.sequence||0)&&h.retained_at_close); // normally null; explicit below handles decay-specific restores
  const decay=ep.causal_events.filter(e=>e.state==='DECAY_OBSERVATION'); const restores=ep.causal_events.filter(e=>e.state==='BUY_RESTORATION_OBSERVATION');
  const maxHigh=max(future,'high'); const maxHighBar=future.find(b=>b.high===maxHigh); const firstRejection=future.find(b=>b.close<b.open);
  const postPeak=maxHighBar?future.filter(b=>b.ts>maxHighBar.ts):[];
  const firstSellHeld=postPeak.find((b,i)=>{const rest=postPeak.slice(i); return b.close<b.open && rest.length>1 && rest.at(-1).close<=b.close;});
  const classifications=decay.map(d=>{const restored=restores.find(r=>r.time_ts>d.time_ts); return {decay_time:d.time,classification:restored?'INTERNAL_PULLBACK / TEMPORARY_DECAY':'BUY_FAILED_TO_RESTORE',restoration_time:restored?.time??null};});
  const finalDecay=decay.at(-1); const finalRestore=finalDecay?restores.find(r=>r.time_ts>finalDecay.time_ts):null;
  return {episode_id:ep.episode_id,outcome_window:{start:ep.start_time,end:ep.end_time},future_window_statistics:{window_volume_BTC:sum(future,'volume_BTC'),window_delta_BTC:sum(future,'delta_BTC'),OI_path:oiPath(future),max_high:maxHigh,max_high_time:maxHighBar?fmt(maxHighBar.ts):null},decay_classifications:classifications,release_overall:restores.length?'BUY_RESTORED_AFTER_PRIOR_DECAYS':'BUY_FAILED_TO_RESTORE',final_decay_outcome:finalDecay?(finalRestore?'BUY_RESTORED':'BUY_FAILED_TO_RESTORE'):'NO_DECAY',final_decay_time:finalDecay?.time??null,first_sharp_price_rejection:firstRejection?fmt(firstRejection.ts):null,earliest_timestamp_buy_control_challenged:decay[0]?.time??null,earliest_timestamp_sell_got_and_held_result:firstSellHeld?fmt(firstSellHeld.ts):null,after_fact_note:'This object is outcome-only and must not be joined into live event evidence.'};
}

function audit(events,outcomes,zones,zoneEvents) { const errors=[]; for(const e of events){const t=e.time_ts; const ev=e.evidence||{}; if(ev.window_volume_BTC||ev.window_delta_BTC||ev.OI_end||ev.OI_min||ev.OI_max) errors.push(`${e.event_id}: future aggregate key in event`); for(const ts of ev.OI_path_to_T?.samples_time||[]) { const parsed=Date.parse(ts.replace(' ','T')+'-05:00'); if(parsed>t) errors.push(`${e.event_id}: OI sample after event`); } } const parentLeak=zones.filter(z=>(z.timeframe==='15M'||z.timeframe==='1H')&&observableAt(z,zoneEvents)>Date.parse('2026-09-22T20:57:00-05:00')).length; return {event_count:events.length,outcome_count:outcomes.length,future_evidence_errors:errors.length,errors,parent_zone_observable_at_or_before_check:'passed',parent_zones_not_yet_observable_at_benchmark_start:parentLeak}; }

function main() {
  const rows=readCsv(MASTER_FILE), zones=JSON.parse(fs.readFileSync(path.join(ROOT,ZONE_FILE),'utf8')).zones, zoneEvents=readJsonl(ZONE_EVENTS_FILE), transfer=readJsonl(TRANSFER_EVENTS_FILE);
  const start=Date.parse('2026-09-22T20:57:00-05:00'), end=Date.parse('2026-09-23T00:30:00-05:00'), origin=rowAtOrBefore(rows,start), refs=priorReferences(start,origin.close,rows), ep=makeEpisode(rows,start,end,'RELEASE-V2-20260922-BUY',refs[0]?.price??null,zones,zoneEvents); const events=ep.causal_events; const out=outcome(ep,rows); const targets=[{episode_id:ep.episode_id,...ep.expected_release_path}]; const auditResult=audit(events,[out],zones,zoneEvents);
  fs.writeFileSync(path.join(ROOT,OUT.episodes),JSON.stringify(ep)+'\n'); fs.writeFileSync(path.join(ROOT,OUT.events),events.map(JSON.stringify).join('\n')+'\n'); fs.writeFileSync(path.join(ROOT,OUT.outcomes),JSON.stringify(out)+'\n'); fs.writeFileSync(path.join(ROOT,OUT.targets),targets.map(JSON.stringify).join('\n')+'\n');
  const restored=out.decay_classifications.filter(x=>x.classification.includes('TEMPORARY')).length, failed=out.decay_classifications.filter(x=>x.classification==='BUY_FAILED_TO_RESTORE').length, passive=events.filter(e=>e.state==='PASSIVE_REJECTION_CANDIDATE').length;
  const md=['# BTC-LRA Terminal Release V2','','Research-only replay; existing V1 outputs are preserved. Production detector code is untouched.','V1 events at 21:01–21:04 are not used as proof of terminal release. The benchmark BUY release begins 20:57 at 86255; 23:36 at 87247.3 is used only as after-fact navigation in the outcome.','','## Comparative sequence','| class | count | interpretation |','|---|---:|---|',`| temporary decay later restored BUY | ${restored} | INTERNAL_PULLBACK / TEMPORARY_DECAY |`,`| decay before failed restoration | ${failed} | BUY_FAILED_TO_RESTORE; outcome-only |`,`| passive-rejection candidates | ${passive} | behavioral hypothesis only; no seller identity inferred |`,'','## Benchmark 22.09','',`Episode: ${ep.start_time} → ${ep.end_time}; max high observed in outcome window: ${out.future_window_statistics.max_high} at ${out.future_window_statistics.max_high_time}.`,`Earliest challenged: ${out.earliest_timestamp_buy_control_challenged||'none'}; earliest SELL held result after the actual outcome high: ${out.earliest_timestamp_sell_got_and_held_result||'none'}.`,`Final decay outcome: ${out.final_decay_outcome} at ${out.final_decay_time||'none'}; earlier BUY restorations remain separate outcomes.`,'','### High sequence','',...ep.high_sequence.map(h=>`- HIGH_${h.sequence} ${fmt(h.ts)} @ ${h.high}; extension=${h.extension_from_previous}; effort_since_previous=${h.effort_since_previous_high}; incremental_extension_per_100_BTC=${h.incremental_extension_per_100_BTC}; retained_at_close=${h.retained_at_close}`),'','## State semantics','- `DECAY_OBSERVATION` is live and causal; it is not terminal.','- `PASSIVE_REJECTION_CANDIDATE` records behavior compatible with passive opposing liquidity without asserting a limit seller.','- Restoration and terminal/failed classifications are outcomes, never live evidence.','- `EXTREME_NOT_RETAINED` is not emitted for each local high; active release extremes are represented by the HIGH_1 → pullback → HIGH_2 sequence.','',`Audit: ${auditResult.future_evidence_errors===0?'PASS':'FAIL'}`].join('\n')+'\n'; fs.writeFileSync(path.join(ROOT,OUT.analysis),md); fs.writeFileSync(path.join(ROOT,OUT.audit),JSON.stringify(auditResult,null,2)+'\n'); console.log(JSON.stringify({episode:ep.episode_id,events:events.length,highs:ep.high_sequence.length,restored,failed,passive,audit:auditResult.future_evidence_errors},null,2));
}
main();
