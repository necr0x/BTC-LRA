const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const TF_MINUTES = { '5M': 5, '15M': 15, '1H': 60, '4H': 240 };
const HORIZONS = [5, 15, 30, 60];
const master = readCsv(path.join(ROOT, 'BTC_LRA_MASTER_20260920_NOW_1M.csv'));
const zones = JSON.parse(fs.readFileSync(path.join(ROOT, 'BTC_LRA_ZONE_STATE.json'), 'utf8')).zones;
const zoneEvents = readJsonl(path.join(ROOT, 'BTC_LRA_ZONE_EVENTS.jsonl'));

function readJsonl(file) { return fs.readFileSync(file, 'utf8').trim().split(/\r?\n/).filter(Boolean).map(JSON.parse); }
function parseCsvLine(line) { const out=[]; let cur='', quoted=false; for(let i=0;i<line.length;i++){const c=line[i]; if(c==='"'){if(quoted&&line[i+1]==='"'){cur+='"';i++;}else quoted=!quoted;}else if(c===','&&!quoted){out.push(cur);cur='';}else cur+=c;}out.push(cur);return out; }
function readCsv(file) { const lines=fs.readFileSync(file,'utf8').trim().split(/\r?\n/); const h=parseCsvLine(lines[0]); return lines.slice(1).map(x=>{const a=parseCsvLine(x),o={};h.forEach((k,i)=>o[k]=a[i]??''); for(const k of ['open','high','low','close','volume_BTC','taker_buy_BTC','taker_sell_BTC','delta_BTC','delta_volume_ratio','OI_BTC','dOI_BTC','dOI_pct','distance_to_lr_low','distance_to_lr_high']) if(o[k]!=='') o[k]=Number(o[k]); o.ts=Date.parse(o.timestamp_utc); return o;}); }
function n(x){return x===''||x==null?null:Number(x);}
function signedReward(side, from, to){return side==='BUY'?Math.max(0,to-from):Math.max(0,from-to);}
function sideProgress(side, base, close){return side==='BUY'?Math.max(0,close-base):Math.max(0,base-close);}
function fmt(ts){return new Intl.DateTimeFormat('en-CA',{timeZone:'America/Panama',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(ts)).replace(',','');}
function nearestBar(ts){let best=null;for(const b of master){if(b.ts>ts)break;best=b;}return best;}
function futureOutcome(ts, side, price){const res={};for(const h of HORIZONS){const end=ts+h*60000,bars=master.filter(x=>x.ts>ts&&x.ts<=end);if(!bars.length){res[`${h}m`]=null;continue;}const favorable=side==='BUY'?Math.max(...bars.map(x=>x.high))-price:price-Math.min(...bars.map(x=>x.low));const adverse=side==='BUY'?price-Math.min(...bars.map(x=>x.low)):Math.max(...bars.map(x=>x.high))-price;res[`${h}m`]={mfe_usd:favorable,mae_usd:adverse};}return res;}
function zoneTrajectory(z){
  const start=z.end_ts, departure=z.departure_ts||start, end=(departure||start)+60*60000;
  const bars=master.filter(b=>b.ts>=start&&b.ts<=end); const baseBar=bars[0]||nearestBar(start); if(!baseBar)return {trajectory:[],candidate:null};
  const base=baseBar.close; let buyEff=0,sellEff=0,buyReward=0,sellReward=0,buyMax=0,sellMax=0,prev=base,lastOI=baseBar.OI_BTC||null;
  const trajectory=[]; let priorBuyEff=null,priorSellEff=null,firstOpp=null,candidate=null;
  const departureBar=master.find(b=>b.ts===departure)||nearestBar(departure); const departureEvent=zoneEvents.find(e=>e.zone_id===z.zone_id&&(e.event==='DEPARTED_UP'||e.event==='DEPARTED_DOWN')); const departureSide=departureEvent?.event==='DEPARTED_UP'?'BUY':departureEvent?.event==='DEPARTED_DOWN'?'SELL':null;
  for(const b of bars){
    const buy=n(b.taker_buy_BTC)||0,sell=n(b.taker_sell_BTC)||0; buyEff+=buy;sellEff+=sell;
    const br=signedReward('BUY',prev,b.close),sr=signedReward('SELL',prev,b.close); buyReward+=br;sellReward+=sr;
    const bp=sideProgress('BUY',base,b.close),sp=sideProgress('SELL',base,b.close); buyMax=Math.max(buyMax,bp);sellMax=Math.max(sellMax,sp);
    const oi=n(b.OI_BTC); const oiChange=oi==null||lastOI==null?null:oi-lastOI;if(oi!=null)lastOI=oi;
    const buyEffcy=buyEff?buyReward/buyEff*100:null,sellEffcy=sellEff?sellReward/sellEff*100:null;
    const row={timestamp:fmt(b.ts),ts:b.ts,close:b.close,high:b.high,low:b.low,buy_effort_BTC:buy,sell_effort_BTC:sell,buy_cumulative_effort:buyEff,sell_cumulative_effort:sellEff,buy_reward:br,sell_reward:sr,buy_cumulative_reward:buyReward,sell_cumulative_reward:sellReward,buy_retained_reward:bp,sell_retained_reward:sp,buy_retention:buyMax?bp/buyMax:null,sell_retention:sellMax?sp/sellMax:null,buy_result_per_100_BTC:buyEff?buyReward/buyEff*100:null,sell_result_per_100_BTC:sellEff?sellReward/sellEff*100:null,oi_change_during_bar:oiChange,oi_BTC:oi,location:b.location};
    const prevBuy=priorBuyEff==null?buyEffcy:priorBuyEff,prevSell=priorSellEff==null?sellEffcy:priorSellEff;
    row.buy_efficiency_decay=buyEffcy!=null&&prevBuy!=null&&buyEffcy<prevBuy&&br===0;
    row.sell_efficiency_decay=sellEffcy!=null&&prevSell!=null&&sellEffcy<prevSell&&sr===0;
    row.departure=(departureBar&&b.ts===departureBar.ts)?departureSide:null;
    trajectory.push(row);
    const oldSide=departureSide, newSide=oldSide==='BUY'?'SELL':oldSide==='SELL'?'BUY':null;
    if(oldSide&&newSide&&!candidate){const decay=oldSide==='BUY'?row.buy_efficiency_decay:row.sell_efficiency_decay;const newReward=newSide==='BUY'?br:sr;const newRetained=newSide==='BUY'?bp:sp;if(decay&&newReward>0&&!firstOpp){firstOpp={ts:b.ts,side:newSide,price:b.close};}if(firstOpp&&newRetained>0&&b.ts>=firstOpp.ts){candidate={candidate_time:b.ts,candidate_price:b.close,side_a:oldSide,side_b:newSide,reason:'SIDE_A efficiency decayed; SIDE_B first reward became retained',available_at:b.ts,source_zone_id:z.zone_id};}}
    priorBuyEff=buyEffcy;priorSellEff=sellEffcy;prev=b.close;
  }
  if(candidate){candidate.future_outcome=futureOutcome(candidate.candidate_time,candidate.side_b,candidate.candidate_price);const after=trajectory.filter(x=>x.ts>candidate.candidate_time);candidate.transfer_confirmed_after_fact=after.some(x=>candidate.side_b==='BUY'?x.buy_retained_reward>0:x.sell_retained_reward>0);candidate.classification=candidate.transfer_confirmed_after_fact?'TRANSFER_CONFIRMED_AFTER_FACT':'TRANSFER_CANDIDATE';}
  return {trajectory,candidate,baseline:{timestamp:fmt(baseBar.ts),price:base,zone_end_time:fmt(start),departure_time:departure?fmt(departure):null}};
}
function main(){
  const records=[],transfers=[];for(const z of zones){const r=zoneTrajectory(z);records.push({...z,second_layer:'effort_result_walk_forward',baseline:r.baseline,trajectory:r.trajectory,candidate:r.candidate});if(r.candidate)transfers.push(r.candidate);}
  fs.writeFileSync(path.join(ROOT,'BTC_LRA_ZONE_EFFORT_RESULT_ANALYSIS.jsonl'),records.map(x=>JSON.stringify(x)).join('\n')+'\n');
  fs.writeFileSync(path.join(ROOT,'BTC_LRA_TRANSFER_EVENTS.jsonl'),transfers.map(x=>JSON.stringify(x)).join('\n')+'\n');
  const bench=transfers.filter(x=>(x.candidate_time>=Date.parse('2026-09-28T06:30:00-05:00')&&x.candidate_time<=Date.parse('2026-09-28T07:05:00-05:00'))||(x.candidate_time>=Date.parse('2026-09-29T00:30:00-05:00')&&x.candidate_time<=Date.parse('2026-09-29T02:30:00-05:00')));
  const controls=records.filter(x=>x.departure_ts&&!x.candidate).slice(0,5);
  const lines=['# BTC-LRA effort/result transfer research','',`Scope: existing zones only; master 1m dataset ${fmt(master[0].ts)} -> ${fmt(master.at(-1).ts)} Panama UTC-5.`,`No zones were created or modified. No thresholds, detectors, state machines, live output or trading decisions were changed.`,`This layer records BUY/SELL effort, directional reward, retained reward, result per 100 BTC, OI change and efficiency trajectory from each zone's BALANCE_ACTIVE evidence through departure plus 60m.`,`TRANSFER_CANDIDATE is fixed only from data available at its candidate timestamp. TRANSFER_CONFIRMED_AFTER_FACT uses later bars only as an outcome label.`,'','## Benchmark cases',''];
  for(const x of bench)lines.push(`- ${x.source_zone_id} | ${x.candidate_time?fmt(x.candidate_time):''} | ${x.side_a}->${x.side_b} | price=${x.candidate_price} | ${x.classification} | outcome=${JSON.stringify(x.future_outcome)}`);
  lines.push('','## Control cases (departure without an in-window transfer candidate)','');for(const x of controls)lines.push(`- ${x.zone_id} | ${x.timeframe} | departure=${x.departure_time||'NONE'} | state=${x.state} | bounds=${x.low}..${x.high}`);
  lines.push('','## Measured fields','- `effort`: taker buy/sell volume from 1m source.','- `price reward`: positive close-to-close movement for the side on each closed bar.','- `retained reward`: current progress from zone baseline in the side direction.','- `efficiency trajectory`: cumulative directional reward / cumulative side effort * 100.','- OI is used only where the source has an actual 5m sample; it is not interpolated.','- Future MFE/MAE are stored only after candidate timestamp and are never used to create the candidate.','', '## Interpretation boundary','- `NO_TRANSFER` means no causal transfer candidate was observed in the stored walk-forward window.','- `TRANSFER_CANDIDATE` means the causal sequence was observable at that time.','- `TRANSFER_CONFIRMED_AFTER_FACT` is an outcome label, not a live decision.','- These labels do not mean reversal, trapped inventory, market-maker activity, or an entry signal.','');
  fs.writeFileSync(path.join(ROOT,'BTC_LRA_EFFORT_TRANSFER_ANALYSIS.md'),lines.join('\n'));console.log(JSON.stringify({zones:records.length,transfer_events:transfers.length,benchmarks:bench.length,controls:controls.length},null,2));
}
main();
