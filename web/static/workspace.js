(() => {
  const api = new window.FinGuardApi();
  const $ = (id) => document.getElementById(id);
  let runId = null; let lastSeq = 0; let timer = null;
  let decisionInFlight = false;
  const text = (value) => value == null ? "" : String(value);
  const esc = (value) => text(value).replace(/[&<>\"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[char]));
  function showError(error, target) {
    const message = error && error.message ? error.message : "The API request failed.";
    if (target) target.textContent = message;
    return message;
  }
  function renderTask(task) {
    const pending = task.pending_proposal;
    const mode = task.data_mode ? ` · ${esc(String(task.data_mode).toUpperCase())} DATA` : "";
    $("task").innerHTML = `<strong>${esc(task.status)}</strong> <span class="muted">${esc(task.run_id)} · ${esc(task.step)}${mode}</span>` + (task.final_answer ? `<p>${esc(task.final_answer)}</p>` : task.error ? `<p class="error">${esc(JSON.stringify(task.error))}</p>` : "");
    const approval = $("approval");
    if (pending) {
      approval.classList.remove("hidden");
      $("proposal").innerHTML = `<div>${esc(pending.side)} ${esc(pending.quantity)} ${esc(pending.symbol)} at ${esc(pending.reference_price)}</div><div class="muted">Proposal ${esc(pending.proposal_id)} · version ${esc(pending.version)}</div>`;
    } else approval.classList.add("hidden");
  }
  async function refresh() {
    if (!runId) return;
    const selectedRun = runId;
    const task = await api.getTask(selectedRun);
    if (selectedRun !== runId) return;
    renderTask(task);
    const events = await api.getEvents(selectedRun, lastSeq);
    if (selectedRun !== runId) return;
    if (lastSeq === 0) $("events").innerHTML = "";
    events.forEach(event => { lastSeq = Math.max(lastSeq, Number(event.seq) || 0); const li = document.createElement("li"); li.textContent = `[${event.seq}] ${event.event_type} ${event.status || ""} ${event.error_code || ""}`; $("events").appendChild(li); });
    if (["COMPLETED", "FAILED", "CANCELLED"].includes(task.status) && timer) { clearInterval(timer); timer = null; }
  }
  async function portfolio() { try { const value = await api.getPortfolio(); $("portfolio").textContent = JSON.stringify(value, null, 2); } catch (error) { showError(error, $("portfolio")); } }
  $("task-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const task = await api.createTask($("prompt").value, $("mode").value, $("allow").checked); runId = task.run_id; lastSeq = 0; api.stopPolling(); renderTask(task); await refresh(); api.startPolling(runId, {onUpdate: ({task: updated, events}) => { if (runId === updated.run_id) { renderTask(updated); if (lastSeq === 0) $("events").innerHTML = ""; events.forEach(eventItem => { lastSeq = Math.max(lastSeq, Number(eventItem.seq) || 0); const li = document.createElement("li"); li.textContent = `[${eventItem.seq}] ${eventItem.event_type} ${eventItem.status || ""} ${eventItem.error_code || ""}`; $("events").appendChild(li); }); } }, onError: (error) => showError(error, $("task"))}, {afterSeq: lastSeq}); } catch (error) { showError(error, $("task")); } });
  $("approval").addEventListener("click", async (event) => { const decision = event.target.dataset.decision; if (!decision || !runId || decisionInFlight) return; decisionInFlight = true; const buttons = $("approval").querySelectorAll("button[data-decision]"); buttons.forEach(button => { button.disabled = true; }); try { const task = await api.getTask(runId); const pending = task.pending_proposal; if (!pending) return; const result = await api.decideProposal(runId, pending.proposal_id, decision, task.version); if (result && result.order_status === "BLOCKED") $("task").textContent = `Order blocked: ${esc(result.receipt || result.blocked_code || "risk check failed")}`; await refresh(); await portfolio(); } catch (error) { showError(error, $("task")); } finally { decisionInFlight = false; buttons.forEach(button => { button.disabled = false; }); } });
  $("later").addEventListener("click", () => $("approval").classList.add("hidden"));
  portfolio().catch(() => { $("portfolio").textContent = "Portfolio unavailable"; });
})();
