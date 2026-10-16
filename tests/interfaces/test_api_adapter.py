from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for browser adapter smoke tests")
def test_browser_adapter_fake_http_contract_and_deduplication():
    adapter = Path(__file__).parents[2] / "web" / "static" / "api-adapter.js"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const calls = [];
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const response = (status, body) => ({ok: status >= 200 && status < 300, status, async text() { return JSON.stringify(body); }});
let decisionCalls = 0;
const fetchImpl = async (url, options = {}) => {
  calls.push({url, options});
  if (url.endsWith('/api/network')) throw new Error('offline');
  if (url.endsWith('/api/config/public')) return response(200, {supports_live_data:false, paper_account:true});
      if (url.endsWith('/api/portfolio')) return response(200, {cash:'100.00', equity:'100.00', positions:[]});
  if (url.endsWith('/api/tasks')) return response(200, {run_id:'r1', status:'WAITING_APPROVAL', version:4, data_mode:'demo'});
  if (url.includes('/events?after_seq=4')) return response(200, [{seq:5, event_type:'PRICE_GAP'}]);
  if (url.includes('/events?after_seq=0')) return response(200, [{seq:4, event_type:'WAITING_APPROVAL'}]);
  if (url.endsWith('/api/tasks/r1')) return response(200, {run_id:'r1', status:'WAITING_APPROVAL', version:4, pending_proposal:{proposal_id:'p1'}});
  if (url.includes('/decision')) {
    decisionCalls += 1;
    await wait(20);
    const body = JSON.parse(options.body);
    if (body.expected_version !== 4) return response(409, {error:{code:'VERSION_CONFLICT', message:'stale version'}});
    if (body.decision === 'approve') return response(200, {order_status:'BLOCKED', blocked_code:'PRICE_GAP_EXCEEDED', version:4});
    return response(409, {error:{code:'VERSION_CONFLICT', message:'stale version'}});
  }
  return response(503, {detail:'service unavailable'});
};
const context = {console, setTimeout, clearTimeout, fetch: fetchImpl};
context.globalThis = context;
vm.runInNewContext(source, context, {filename: 'api-adapter.js'});
const api = new context.FinGuardApi({baseUrl:'http://test', fetchImpl});
(async () => {
  const task = await api.createTask('research', 'demo', true);
  assert.equal(task.run_id, 'r1');
  assert.equal((await api.getEvents('r1', 4))[0].seq, 5);
  const p1 = api.decideProposal('r1', 'p1', 'approve', 4);
  const p2 = api.decideProposal('r1', 'p1', 'approve', 4);
  assert.strictEqual(p1, p2);
  const blocked = await p1;
  assert.equal(blocked.blocked_code, 'PRICE_GAP_EXCEEDED');
  assert.equal(decisionCalls, 1);
  let conflict = false;
  try { await api.decideProposal('r1', 'p1', 'reject', 3); } catch (error) { conflict = error.isVersionConflict && error.code === 'VERSION_CONFLICT'; }
  assert.equal(conflict, true);
  const config = await api.getPublicConfig();
  assert.equal(config.supports_live_data, false);
      const portfolio = await api.getPortfolio();
      assert.equal(portfolio.cash, '100.00');
      const updates = [];
      const stop = api.startPolling('r1', {onUpdate: update => updates.push(update)}, {afterSeq:4, intervalMs:250});
      await wait(20);
      stop();
      assert.equal(updates.length, 1);
      assert.equal(updates[0].runId, 'r1');
      assert.equal(updates[0].afterSeq, 5);
  let unavailable = false;
  try { await api._request('/api/unavailable'); } catch (error) { unavailable = error.isUnavailable && error.code === 'SERVICE_UNAVAILABLE'; }
  assert.equal(unavailable, true);
      let network = false;
      try { await api._request('/api/network'); } catch (error) { network = error.code === 'NETWORK_ERROR'; }
      assert.equal(network, true);
  console.log(JSON.stringify({calls:calls.length, decisionCalls}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        ["node", "-e", script, str(adapter)],
        cwd=adapter.parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"decisionCalls":2' in completed.stdout
