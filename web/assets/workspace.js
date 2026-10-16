(() => {
  let runId = null, proposalId = null, version = null, timer = null, afterSeq = 0;
  const $ = (id) => document.getElementById(id);
  const json = (v) => JSON.stringify(v, null, 2);
  async function refresh() {
    if (!runId) return;
    const [taskRes, eventRes] = await Promise.all([
      fetch(`/api/tasks/${encodeURIComponent(runId)}`),
      fetch(`/api/tasks/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`)
    ]);
    if (!taskRes.ok) return;
    const task = await taskRes.json();
    const events = eventRes.ok ? await eventRes.json() : [];
    $('status').textContent = `${task.status} · run ${task.run_id}`;
    version = task.version;
    $('answer').textContent = task.final_answer || (task.error ? json(task.error) : 'Agent is running...');
    for (const event of events) { afterSeq = Math.max(afterSeq, event.seq || 0); const li = document.createElement('li'); li.textContent = `${event.seq ?? ''} ${event.event_type || event.type || 'event'}${event.error_code ? ` · ${event.error_code}` : ''}`; $('trace').appendChild(li); }
    if (task.pending_proposal) { proposalId = task.pending_proposal.proposal_id; $('proposal').textContent = json(task.pending_proposal); $('approval').hidden = false; } else { $('approval').hidden = true; }
    if (['COMPLETED','FAILED','CANCELLED'].includes(task.status)) { clearInterval(timer); timer = null; }
  }
  async function portfolio() { const r = await fetch('/api/portfolio'); if (r.ok) $('portfolio').textContent = json(await r.json()); }
  $('task-form').addEventListener('submit', async (e) => { e.preventDefault(); const r = await fetch('/api/tasks', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt:$('prompt').value, data_mode:'demo', allow_paper_order_proposals:$('allow').checked})}); if (!r.ok) { $('answer').textContent = await r.text(); return; } const task = await r.json(); runId = task.run_id; proposalId = null; version = task.version; afterSeq = 0; $('trace').innerHTML = ''; await refresh(); await portfolio(); if (!timer) timer = setInterval(refresh, 1500); });
  async function decide(decision) { if (!runId || !proposalId) return; const r = await fetch(`/api/proposals/${encodeURIComponent(proposalId)}/decision?run_id=${encodeURIComponent(runId)}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({decision, expected_version:version})}); $('answer').textContent = r.ok ? json(await r.json()) : await r.text(); await refresh(); await portfolio(); }
  $('approve').addEventListener('click', () => decide('approve')); $('reject').addEventListener('click', () => decide('reject')); portfolio();
})();
