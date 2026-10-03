const fs = require('fs');
const path = require('path');

const store = path.join(__dirname, 'BTC_LRA_DOMINANCE_EPISODES.jsonl');

function updateLiveCandidate(patch) {
  if (!patch || !patch.episode_id) throw new Error('patch must include episode_id');
  const records = fs.readFileSync(store, 'utf8').split(/\r?\n/).filter(Boolean).map(JSON.parse);
  const index = records.findIndex(record => record.episode_id === patch.episode_id);
  if (index < 0) throw new Error(`episode not found: ${patch.episode_id}`);
  const updated = { ...records[index], ...patch, updated_at: patch.updated_at || new Date().toISOString() };
  if (updated.status === 'LIVE_CANDIDATE' && updated.outcome?.known !== false) {
    throw new Error('LIVE_CANDIDATE must keep outcome.known=false');
  }
  records[index] = updated;
  const temporary = `${store}.tmp`;
  fs.writeFileSync(temporary, records.map(JSON.stringify).join('\n') + '\n', 'utf8');
  fs.renameSync(temporary, store);
  return updated;
}

if (require.main === module) {
  const patchPath = process.argv[2];
  if (!patchPath) throw new Error('usage: node update_live_candidate.js <patch.json>');
  const updated = updateLiveCandidate(JSON.parse(fs.readFileSync(path.resolve(patchPath), 'utf8')));
  console.log(JSON.stringify({ episode_id: updated.episode_id, status: updated.status, updated_at: updated.updated_at }));
}

module.exports = { updateLiveCandidate };
