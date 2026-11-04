/*
 * Explain why research is waiting and let its owner grant more allowance or quit.
 * All research screens load this file. Polling only displays saved budget state;
 * continuing always requires an explicit click, bound to the displayed pause.
 */
'use strict';
let budgetState = null;
let budgetSubmitting = false;

/** Show one accessible, text-only budget dialog while the server holds new calls.
 * Args: state — latest public session snapshot, including model_budget accounting.
 * Returns: nothing; preserves button focus during polling and never starts work.
 * Closing with Escape is disabled: leaving the page also leaves research paused.
 */
function renderBudget(state = {}) {
  budgetState = state;
  const budget = state.model_budget;
  let dialog = document.getElementById('budget-dialog');
  const active = ['working', 'search_working', 'run_running', 'selection_running', 'triage_running', 'investigation_running', 'link_ranking_running', 'report_running'].includes(state.status);
  if (!budget?.paused || !active) { if (dialog?.open) dialog.close(); return; }
  if (!dialog) {
    dialog = document.createElement('dialog'); dialog.id = 'budget-dialog';
    dialog.setAttribute('aria-labelledby', 'budget-heading');
    dialog.setAttribute('aria-describedby', 'budget-explanation');
    const heading = document.createElement('h2'); heading.id = 'budget-heading'; heading.textContent = 'Research paused';
    const explanation = document.createElement('p'); explanation.id = 'budget-explanation';
    const usage = document.createElement('p'); usage.id = 'budget-counters';
    const offer = document.createElement('p'); offer.id = 'budget-offer';
    const error = document.createElement('p'); error.id = 'budget-error'; error.setAttribute('role', 'alert');
    const quit = document.createElement('button'); quit.type = 'button'; quit.className = 'secondary'; quit.textContent = 'Quit'; quit.id = 'budget-quit';
    const proceed = document.createElement('button'); proceed.type = 'button'; proceed.textContent = 'Continue'; proceed.id = 'budget-continue';
    quit.addEventListener('click', () => submitBudgetDecision('quit'));
    proceed.addEventListener('click', () => submitBudgetDecision('continue'));
    dialog.addEventListener('cancel', event => event.preventDefault());
    dialog.append(heading, explanation, usage, offer, error, quit, proceed);
    document.querySelector('main').append(dialog);
  }
  document.getElementById('budget-explanation').textContent = `${budget.reason} No further model requests will be sent until you choose. Completed work is saved.`;
  document.getElementById('budget-counters').textContent = `${budget.calls_used.toLocaleString()} / ${budget.call_limit.toLocaleString()} model calls; ${budget.known_tokens.toLocaleString()} known tokens${budget.estimated_tokens ? ' + ' + budget.estimated_tokens.toLocaleString() + ' reserved estimate for unconfirmed usage' : ''} / ${budget.token_limit.toLocaleString()} token allowance.${budget.unknown_usage ? ' Usage remains unconfirmed for ' + budget.unknown_usage + ' attempted calls; it is not assumed free.' : ''}`;
  document.getElementById('budget-offer').textContent = `Continue raises this run’s total limits to ${budget.next_call_limit.toLocaleString()} calls and ${budget.next_token_limit.toLocaleString()} tokens and resumes where it paused. Additional calls may incur charges. Quit stops research and keeps saved results.`;
  if (!dialog.open) { document.getElementById('budget-error').textContent = ''; dialog.showModal(); document.getElementById('budget-quit').focus(); }
}

/** Submit a single decision for the pause currently shown, never an automatic retry.
 * Args: action — 'continue' to increase limits or 'quit' to stop pending work.
 * Returns: promise; refreshes saved state on success. Errors leave the dialog open
 * with its explanation. Duplicate/stale decisions cannot grant multiple budgets.
 */
async function submitBudgetDecision(action) {
  if (budgetSubmitting || !budgetState?.model_budget?.paused) return;
  budgetSubmitting = true;
  const state = budgetState;
  for (const id of ['budget-quit', 'budget-continue']) document.getElementById(id).disabled = true;
  try {
    const response = await fetch(`/api/setup/${state.session_id}/budget`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({revision: state.model_budget.revision, action})
    });
    const next = await response.json();
    if (!response.ok) throw new Error(typeof next.detail === 'string' ? next.detail : 'Could not save your budget choice.');
    renderBudget(next);
    renderActivity(next);
  } catch (error) { document.getElementById('budget-error').textContent = error.message; }
  finally {
    budgetSubmitting = false;
    for (const id of ['budget-quit', 'budget-continue']) document.getElementById(id).disabled = false;
  }
}

let roundBudgetState = null;
let roundBudgetSubmitting = false;
/** Explain the saved pre-ranking checkpoint without starting or funding work.
 * Args: state — public session with a pending round_budget_decision, if any.
 * Returns: nothing; displays an accessible choice on every research page. Closing
 * or reloading never approves a round; the server persists the pending choice.
 */
function renderRoundBudget(state = {}) {
  roundBudgetState = state;
  const decision = state.round_budget_decision;
  let dialog = document.getElementById('round-budget-dialog');
  if (state.status !== 'link_ranking_waiting' || decision?.status !== 'pending') {
    if (dialog?.open) dialog.close();
    return;
  }
  if (!dialog) {
    dialog = document.createElement('dialog'); dialog.id = 'round-budget-dialog';
    dialog.setAttribute('aria-labelledby', 'round-budget-heading');
    dialog.setAttribute('aria-describedby', 'round-budget-explanation');
    const heading = document.createElement('h2'); heading.id = 'round-budget-heading';
    heading.textContent = 'Research another round?';
    const explanation = document.createElement('p'); explanation.id = 'round-budget-explanation';
    const error = document.createElement('p'); error.id = 'round-budget-error'; error.setAttribute('role', 'alert');
    const report = document.createElement('button'); report.id = 'round-budget-report'; report.type = 'button';
    report.textContent = 'Prepare report now';
    const proceed = document.createElement('button'); proceed.id = 'round-budget-continue'; proceed.type = 'button';
    proceed.className = 'secondary'; proceed.textContent = 'Rank links and research another round';
    report.addEventListener('click', () => submitRoundBudget('report'));
    proceed.addEventListener('click', () => submitRoundBudget('continue'));
    dialog.addEventListener('cancel', event => event.preventDefault());
    dialog.append(heading, explanation, error, report, proceed); document.querySelector('main').append(dialog);
  }
  const usage = decision.usage;
  document.getElementById('round-budget-explanation').textContent = `More than half of a budget has been used, or some usage is unconfirmed: ${usage.calls_used.toLocaleString()} / ${usage.call_limit.toLocaleString()} calls and ${(usage.known_tokens + usage.estimated_tokens).toLocaleString()} / ${usage.token_limit.toLocaleString()} tokens. Link ranking has not started. You can prepare the report from saved evidence, or spend more on ranking and round ${decision.round + 1}. Continuing does not increase your budget limits. Reporting also uses the remaining budget.`;
  if (!dialog.open) { dialog.showModal(); document.getElementById('round-budget-report').focus(); }
}

/** Submit only the currently displayed round choice, protected against double clicks.
 * Args: action — 'continue' for exploration or 'report' to skip ranking.
 * Returns: promise; navigates to the queued work or shows an error without losing
 * the pending choice. Stale requests cannot start another round or raise limits.
 */
async function submitRoundBudget(action) {
  if (roundBudgetSubmitting || roundBudgetState?.round_budget_decision?.status !== 'pending') return;
  roundBudgetSubmitting = true;
  const state = roundBudgetState;
  for (const id of ['round-budget-report', 'round-budget-continue']) document.getElementById(id).disabled = true;
  try {
    const response = await fetch(`/api/setup/${state.session_id}/round-budget`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({decision_id: state.round_budget_decision.id, action})
    });
    const next = await response.json();
    if (!response.ok) throw new Error(typeof next.detail === 'string' ? next.detail : 'Could not save your choice.');
    renderRoundBudget(next);
    location.assign(`/links/${state.session_id}`);
  } catch (error) { document.getElementById('round-budget-error').textContent = error.message; }
  finally {
    roundBudgetSubmitting = false;
    for (const id of ['round-budget-report', 'round-budget-continue']) document.getElementById(id).disabled = false;
  }
}
