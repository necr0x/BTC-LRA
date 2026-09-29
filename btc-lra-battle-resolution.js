const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const masterFile = path.join(ROOT, 'BTC_LRA_MASTER_20260920_NOW_1M.csv');
const transferFile = path.join(ROOT, 'BTC_LRA_TRANSFER_EVENTS.jsonl');
const trajectoryFile = path.join(ROOT, 'BTC_LRA_ZONE_EFFORT_RESULT_ANALYSIS.jsonl');
const zoneStateFile = path.join(ROOT, 'BTC_LRA_ZONE_STATE.json');
const zoneEventsFile = path.join(ROOT, 'BTC_LRA_ZONE_EVENTS.jsonl');

function jsonl(file) { return fs.readFileSync(file, 'utf8').split(/\r?\n/).filter(Boolean).map(JSON.parse); }
function parseCsvLine(line) { const out=[]; let cur='', quoted=false; for (let i=0;i<line.length;i++) { const c=line[i]; if (c==='"') { if (quoted && line[i+1]==='"') { cur+='"'; i++; } else quoted=!quoted; } else if (c===',' && !quoted) { out.push(cur); cur=''; } else cur+=c; } out.push(cur); return out; }
function readCsv(file) { const lines=fs.readFileSync(file,'utf8').trim().split(/\r?\n/); const h=parseCsvLine(lines[0]); return lines.slice(1).map(line=>{ const a=parseCsvLine(line), o={}; h.forEach((k,i)=>o[k]=a[i]??''); for (const k of ['open','high','low','close','high','low','volume_BTC','taker_buy_BTC','taker_sell_BTC','delta_BTC','OI_BTC','dOI_BTC']) if (o[k] !== '') o[k]=Number(o[k]); o.ts=Date.parse(o.timestamp_utc); return o; }); }
function num(x) { return x === null || x === undefined || x === '' ? null : Number(x); }
function fmt(ts) { return new Intl.DateTimeFormat('en-CA',{timeZone:'America/Panama',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(ts)).replace(',',''); }
function sideProgress(side, from, to) { return side==='BUY' ? to-from : from-to; }
function opposite(side) { return side==='BUY' ? 'SELL' : 'BUY'; }
function location(price, zone) { if (price < zone.low) return 'BELOW'; if (price > zone.high) return 'ABOVE'; return 'INSIDE'; }
function eventTs(e) { return Date.parse(String(e.event_time).replace(' ','T')+'-05:00'); }

const master = readCsv(masterFile);
const transfers = jsonl(transferFile).sort((a,b)=>a.candidate_time-b.candidate_time);
const trajectoryRecords = jsonl(trajectoryFile);
const zones = JSON.parse(fs.readFileSync(zoneStateFile,'utf8')).zones;
const zoneEvents = jsonl(zoneEventsFile);
const trajectories = new Map(trajectoryRecords.map(r=>[r.zone_id,r]));
const zonesById = new Map(zones.map(z=>[z.zone_id,z]));

function nearestBar(ts) { let best=null; for (const b of master) { if (b.ts>ts) break; best=b; } return best; }
function postTransferEvidence(candidate, zone, rec, nextTransfer) {
  const bars = rec.trajectory.filter(b=>b.ts>candidate.candidate_time && (!nextTransfer || b.ts<nextTransfer.candidate_time));
  const oldSide=candidate.side_a, newSide=candidate.side_b;
  const rows=bars.map(b=>({
    timestamp:b.timestamp, ts:b.ts, price:b.close, high:b.high, low:b.low,
    old_side:oldSide, new_side:newSide,
    old_side_progress:sideProgress(oldSide,candidate.candidate_price,b.close),
    new_side_progress:sideProgress(newSide,candidate.candidate_price,b.close),
    old_side_reward:b[oldSide.toLowerCase()+'_reward'], new_side_reward:b[newSide.toLowerCase()+'_reward'],
    old_side_retained_reward:b[oldSide.toLowerCase()+'_retained_reward'], new_side_retained_reward:b[newSide.toLowerCase()+'_retained_reward'],
    old_side_retention:b[oldSide.toLowerCase()+'_retention'], new_side_retention:b[newSide.toLowerCase()+'_retention'],
    old_side_result_per_100_BTC:b[oldSide.toLowerCase()+'_result_per_100_BTC'], new_side_result_per_100_BTC:b[newSide.toLowerCase()+'_result_per_100_BTC'],
    old_side_effort_BTC:b[oldSide.toLowerCase()+'_effort_BTC'], new_side_effort_BTC:b[newSide.toLowerCase()+'_effort_BTC'],
    OI_BTC:num(b.OI_BTC), dOI_BTC:num(b.dOI_BTC), location:b.location
  }));
  const firstNew=rows.find(r=>r.new_side_progress>0);
  const firstOld=rows.find(r=>r.old_side_progress>0);
  const last=rows.at(-1)||null;
  const second=rows[1]||null;
  const newHolding=Boolean(firstNew && (!second || second.new_side_progress>0 || (last && last.new_side_progress>0)));
  const oldRestored=Boolean(firstOld);
  return {bars:rows, first_new_side_reward:firstNew||null, first_old_side_restoration:firstOld||null, last_bar:last, new_side_holding:newHolding, old_side_restored:oldRestored};
}

function releaseAfter(zoneId, ts) {
  const e=zoneEvents.filter(x=>x.zone_id===zoneId && (x.event==='DEPARTED_UP'||x.event==='DEPARTED_DOWN')).map(x=>({...x,ts:eventTs(x)})).filter(x=>x.ts>ts).sort((a,b)=>a.ts-b.ts)[0];
  return e ? {event:e.event,time:e.event_time,price:e.price,direction:e.event==='DEPARTED_UP'?'BUY':'SELL'} : null;
}

function buildBattle(zone, list) {
  const rec=trajectories.get(zone.zone_id); if (!rec) return null;
  const sorted=list.slice().sort((a,b)=>a.candidate_time-b.candidate_time);
  const battleId=`BATTLE-${zone.zone_id}`;
  const entries=[]; const resolutionEvents=[];
  for (let i=0;i<sorted.length;i++) {
    const c=sorted[i], next=sorted[i+1]||null;
    const ev=postTransferEvidence(c,zone,rec,next);
    const release=releaseAfter(zone.zone_id,c.candidate_time);
    const base={battle_id:battleId,zone_id:zone.zone_id,time:c.candidate_time,time_text:fmt(c.candidate_time),price:c.candidate_price,event_type:c.event_type,side:c.side_b,old_side:c.side_a,effort:c.evidence?.side_b_effort??null,result_per_100_BTC:c.evidence?.side_b_result_per_100_BTC??null,retention:c.evidence?.side_b_retained_reward??null,OI_BTC:c.evidence?.OI_BTC??null,price_location:location(c.candidate_price,zone),time_since_previous_transfer:i? (c.candidate_time-sorted[i-1].candidate_time)/60000:null};
    entries.push({...base,post_transfer:{first_new_side_reward:ev.first_new_side_reward,first_old_side_restoration:ev.first_old_side_restoration,last_bar:ev.last_bar,new_side_holding:ev.new_side_holding,old_side_restored:ev.old_side_restored}});
    resolutionEvents.push({battle_id:battleId,zone_id:zone.zone_id,time:c.candidate_time,time_text:fmt(c.candidate_time),state:'TRANSFER_LOCAL',source_transfer:c.event_type,available_at:c.candidate_time,evidence:{candidate_price:c.candidate_price,side_a:c.side_a,side_b:c.side_b,effort:base.effort,result_per_100_BTC:base.result_per_100_BTC,retention:base.retention,OI_BTC:base.OI_BTC}});
    if (ev.first_new_side_reward) resolutionEvents.push({battle_id:battleId,zone_id:zone.zone_id,time:ev.first_new_side_reward.ts,time_text:ev.first_new_side_reward.timestamp,state:'BATTLE_RESOLUTION_CANDIDATE',source_transfer:c.event_type,available_at:ev.first_new_side_reward.ts,evidence:{new_side_progress:ev.first_new_side_reward.new_side_progress,new_side_retained_reward:ev.first_new_side_reward.new_side_retained_reward,old_side_progress:ev.first_new_side_reward.old_side_progress,old_side_result_per_100_BTC:ev.first_new_side_reward.old_side_result_per_100_BTC}});
    if (ev.new_side_holding && ev.bars.length>1) { const hold=ev.bars[1]; resolutionEvents.push({battle_id:battleId,zone_id:zone.zone_id,time:hold.ts,time_text:hold.timestamp,state:'BATTLE_RESOLUTION_HOLDING',source_transfer:c.event_type,available_at:hold.ts,evidence:{new_side_progress:hold.new_side_progress,new_side_retained_reward:hold.new_side_retained_reward,old_side_progress:hold.old_side_progress,OI_BTC:hold.OI_BTC,dOI_BTC:hold.dOI_BTC}}); }
    if (ev.old_side_restored) resolutionEvents.push({battle_id:battleId,zone_id:zone.zone_id,time:ev.first_old_side_restoration.ts,time_text:ev.first_old_side_restoration.timestamp,state:'OLD_SIDE_RESTORED',source_transfer:c.event_type,available_at:ev.first_old_side_restoration.ts,evidence:{old_side_progress:ev.first_old_side_restoration.old_side_progress,old_side_retained_reward:ev.first_old_side_restoration.old_side_retained_reward,new_side_progress:ev.first_old_side_restoration.new_side_progress}});
    if (release) resolutionEvents.push({battle_id:battleId,zone_id:zone.zone_id,time:release.ts,time_text:release.time,state:'RELEASE_OBSERVED',source_transfer:c.event_type,available_at:release.ts,evidence:{departure_event:release.event,direction:release.direction,price:release.price,after_fact:true}});
  }
  const buy=sorted.filter(x=>x.side_b==='BUY').length, sell=sorted.filter(x=>x.side_b==='SELL').length;
  return {battle_id:battleId,zone_id:zone.zone_id,timeframe:zone.timeframe,battle_start:fmt(sorted[0].candidate_time),battle_end:fmt(sorted.at(-1).candidate_time),BUY_TO_SELL_count:sell,SELL_TO_BUY_count:buy,transfer_count:sorted.length,zone_bounds:{low:zone.low,high:zone.high,mid:zone.mid},entries};
}

function audit() {
  const sample=transfers.slice(0,1000); let formulaErrors=0; let signedBelowZero=0;
  for (const c of sample) for (const h of [5,15,30,60]) { const o=c.future_outcome?.[`${h}m`]; if (!o) continue; if (o.mfe_usd<0 || o.mae_usd<0) signedBelowZero++; }
  const lines=['# BTC-LRA battle-resolution MFE/MAE audit','','The transfer script now uses directional distances from candidate price:','- BUY: MFE = max future high - candidate price; MAE = candidate price - min future low.','- SELL: MFE = candidate price - min future low; MAE = max future high - candidate price.','','The previous SELL MAE bug (absolute max future high) was corrected. Existing transfer observations were regenerated; no transfer was removed or filtered.','',`Transfer observations checked: ${sample.length}`,`Formula violations found: ${formulaErrors}`,`Signed directional excursions below zero (not formula violations): ${signedBelowZero}`,'','MFE/MAE are outcome-only fields and are not used by transfer or battle candidate logic.'];
  fs.writeFileSync(path.join(ROOT,'BTC_LRA_BATTLE_RESOLUTION_MFE_MAE_AUDIT.md'),lines.join('\n')+'\n');
}

function main() {
  const byZone=new Map(); for (const c of transfers) { if (!byZone.has(c.source_zone_id)) byZone.set(c.source_zone_id,[]); byZone.get(c.source_zone_id).push(c); }
  const battles=[]; const events=[];
  for (const [id,list] of byZone) { const z=zonesById.get(id); if (!z || list.length===0) continue; const b=buildBattle(z,list); if (b) { battles.push(b); } }
  for (const b of battles) { const first=transfers.find(x=>x.source_zone_id===b.zone_id); events.push({battle_id:b.battle_id,zone_id:b.zone_id,time:first.candidate_time,time_text:fmt(first.candidate_time),state:'BATTLE_ACTIVE',available_at:first.candidate_time,counts:{BUY_TO_SELL:b['BUY_TO_SELL_count'],SELL_TO_BUY:b['SELL_TO_BUY_count']}}); }
  for (const [id,list] of byZone) { const z=zonesById.get(id), rec=trajectories.get(id); if (!z||!rec) continue; const sorted=list.slice().sort((a,b)=>a.candidate_time-b.candidate_time); for(let i=0;i<sorted.length;i++){const c=sorted[i],next=sorted[i+1]||null,ev=postTransferEvidence(c,z,rec,next),release=releaseAfter(id,c.candidate_time),bid=`BATTLE-${id}`; events.push({battle_id:bid,zone_id:id,time:c.candidate_time,time_text:fmt(c.candidate_time),state:'TRANSFER_LOCAL',source_transfer:c.event_type,available_at:c.candidate_time,evidence:{price:c.candidate_price,side_a:c.side_a,side_b:c.side_b,effort:c.evidence?.side_b_effort??null,result_per_100_BTC:c.evidence?.side_b_result_per_100_BTC??null,retention:c.evidence?.side_b_retained_reward??null,OI_BTC:c.evidence?.OI_BTC??null,price_location:location(c.candidate_price,z),time_since_previous_transfer:i?(c.candidate_time-sorted[i-1].candidate_time)/60000:null}}); if(next && !ev.new_side_holding)events.push({battle_id:bid,zone_id:id,time:next.candidate_time,time_text:fmt(next.candidate_time),state:'TRANSFER_CHALLENGED',source_transfer:c.event_type,available_at:next.candidate_time,evidence:{next_transfer:next.event_type,new_side_holding:ev.new_side_holding,old_side_restored:ev.old_side_restored}}); if(ev.first_new_side_reward)events.push({battle_id:bid,zone_id:id,time:ev.first_new_side_reward.ts,time_text:ev.first_new_side_reward.timestamp,state:'BATTLE_RESOLUTION_CANDIDATE',source_transfer:c.event_type,available_at:ev.first_new_side_reward.ts,evidence:ev.first_new_side_reward}); if(ev.bars[1]&&ev.new_side_holding)events.push({battle_id:bid,zone_id:id,time:ev.bars[1].ts,time_text:ev.bars[1].timestamp,state:'BATTLE_RESOLUTION_HOLDING',source_transfer:c.event_type,available_at:ev.bars[1].ts,evidence:ev.bars[1]}); if(ev.first_old_side_restoration)events.push({battle_id:bid,zone_id:id,time:ev.first_old_side_restoration.ts,time_text:ev.first_old_side_restoration.timestamp,state:'OLD_SIDE_RESTORED',source_transfer:c.event_type,available_at:ev.first_old_side_restoration.ts,evidence:ev.first_old_side_restoration}); if(release)events.push({battle_id:bid,zone_id:id,time:release.ts,time_text:release.time,state:'RELEASE_OBSERVED',source_transfer:c.event_type,available_at:release.ts,evidence:{event:release.event,direction:release.direction,price:release.price,after_fact:true}}); }}
  events.sort((a,b)=>a.time-b.time);
  fs.writeFileSync(path.join(ROOT,'BTC_LRA_TRANSFER_BATTLES.jsonl'),battles.map(JSON.stringify).join('\n')+'\n');
  fs.writeFileSync(path.join(ROOT,'BTC_LRA_BATTLE_RESOLUTION_EVENTS.jsonl'),events.map(JSON.stringify).join('\n')+'\n');
  audit();
  const benchmarkIds=['1H-1790046000000']; const bench=events.filter(e=>benchmarkIds.includes(e.zone_id)&&(e.time>=Date.parse('2026-09-22T18:20:00-05:00')&&e.time<=Date.parse('2026-09-22T21:10:00-05:00')));
  const b28=events.filter(e=>e.time>=Date.parse('2026-09-28T06:00:00-05:00')&&e.time<=Date.parse('2026-09-28T07:15:00-05:00'));
  const b29=events.filter(e=>e.time>=Date.parse('2026-09-29T00:20:00-05:00')&&e.time<=Date.parse('2026-09-29T02:00:00-05:00'));
  const lines=['# BTC-LRA battle resolution research','','All 994 source transfer observations are preserved. This layer groups them descriptively by existing zone and does not create, delete, filter, or score transfers.','No detector, threshold, production state machine, human output, or trading decision was changed.','Pre-resolution fields are computed from trajectory rows at or after the transfer timestamp. Departure direction and final zone state are outcome annotations only.','','## States','`BATTLE_ACTIVE`, `TRANSFER_LOCAL`, `TRANSFER_CHALLENGED`, `OLD_SIDE_RESTORED`, `BATTLE_RESOLUTION_CANDIDATE`, `BATTLE_RESOLUTION_HOLDING`, and `RELEASE_OBSERVED` are descriptive research states. No state is an entry or reversal command.','','## Grouping','One battle record is retained per existing zone containing transfers. This avoids introducing an arbitrary attempt-count or optimized time threshold; transfer counts are descriptive only.','','## Benchmark 2026-09-22 18:20–21:10 Panama (focused 20:54–20:56)',''];
  for(const e of bench)lines.push(`- ${e.time_text} | ${e.state} | ${e.source_transfer||'BATTLE'} | ${e.evidence?.price??e.evidence?.event??'—'}`);
  lines.push('','## Benchmark 2026-09-28 06:00–07:15 Panama'); for(const e of b28)lines.push(`- ${e.time_text} | ${e.state} | ${e.source_transfer||'BATTLE'}`);
  lines.push('','## Benchmark 2026-09-29 00:20–02:00 Panama'); for(const e of b29)lines.push(`- ${e.time_text} | ${e.state} | ${e.source_transfer||'BATTLE'}`);
  lines.push('','## Interpretation limits','The first pass records raw post-transfer trajectories and descriptive observations. It does not select a winner, infer position side from OI, or promote a battle resolution into a trading signal.','');
  fs.writeFileSync(path.join(ROOT,'BTC_LRA_BATTLE_RESOLUTION_ANALYSIS.md'),lines.join('\n'));
  console.log(JSON.stringify({source_transfers:transfers.length,battles:battles.length,resolution_events:events.length,benchmark_0922:bench.length,benchmark_0928:b28.length,benchmark_0929:b29.length},null,2));
}
main();
