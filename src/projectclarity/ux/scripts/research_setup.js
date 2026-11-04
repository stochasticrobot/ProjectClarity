/*
 * Make the opening research form respond to the user.
 *
 * This is the browser-side behaviour for research_setup.html. It sends the user's
 * question to the application, displays any clarification questions, submits the
 * answers and follows progress to the next screen. It also remembers which question
 * the browser was showing so the user can return to it.
 *
 * The research runs on the application's server, not in this page. This file helps
 * the user start and follow that work without accidentally starting it again when
 * the page refreshes.
 */
'use strict';
const $ = (id) => document.getElementById(id);
// An explicit fresh-start URL clears only the browser pointer, never saved runs.
const freshStart = new URLSearchParams(location.search).get('new') === '1';
if (freshStart) { localStorage.removeItem('clarity.setup'); history.replaceState(null, '', '/'); }
let sessionId = localStorage.getItem('clarity.setup');
let timer = null;
let renderedQuestions = '';

/** Show a user-facing status or error without rendering HTML.
 * Args: text — readable message; error — whether it describes a failure.
 * Returns: nothing; updates the live status region.
 */
function message(text, error = false) {
  if(error)showActivityIssue();
  $('message').textContent = text;
  $('message').className = error ? 'error' : '';
}

/** Send a same-origin JSON request and reject HTTP/network errors visibly.
 * Args: path — API route; data — optional POST body (omitted for GET).
 * Returns: decoded public session snapshot; throws with HTTP status on failure.
 */
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  });
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(typeof body.detail === 'string' ? body.detail : 'Please check your entries and try again.');
    error.status = response.status;
    throw error;
  }
  return body;
}

/** Build a text-only DOM node so model and user content cannot execute markup.
 * Args: tag — element type; text — literal text content.
 * Returns: detached element, without HTML parsing or side effects.
 */
function node(tag, text) { const item = document.createElement(tag); item.textContent = text; return item; }

/** Render saved state while preserving clarification drafts during polling.
 * Args: state — public session snapshot from the server.
 * Returns: nothing; updates sections and schedules polling only during work.
 */
function render(state) {
  renderStages(state);
  clearTimeout(timer);
  if (state.status.startsWith('link_ranking_')) { location.href = `/links/${sessionId}`; return; }
  if (state.status.startsWith('report_')) { location.href = `/report/${sessionId}`; return; }
  if (state.status.startsWith('investigation_')) { location.href = `/investigation/${sessionId}`; return; }
  if (state.status.startsWith('triage_')) { location.href = `/triage/${sessionId}`; return; }
  if (state.status.startsWith('selection_')) { location.href = `/evidence/${sessionId}`; return; }
  if (state.status.startsWith('run_')) { location.href = `/run/${sessionId}`; return; }
  $('start-run').hidden = state.status !== 'queries_ready' || !!state.auto_advance;
  $('run-limit').hidden = state.status !== 'queries_ready' || !!state.auto_advance;
  $('intro').hidden = true;
  $('result').hidden = false;
  const busy = ['working', 'search_working'].includes(state.status) || (state.auto_advance && ['ready','queries_ready'].includes(state.status));
  const searching = state.status === 'search_working' || (state.auto_advance && ['ready','queries_ready'].includes(state.status));
  $('effective-question').textContent = state.research_question || state.original_question;
  $('new-question').disabled = busy;
  $('ready-note').hidden = state.status !== 'ready' || !!state.auto_advance;
  $('search-section').hidden = !['ready', 'search_working', 'queries_ready', 'search_failed'].includes(state.status);
  $('prepare-searches').hidden = state.status !== 'ready' || !!state.auto_advance;
  $('prepare-searches').disabled = busy;
  $('searches').replaceChildren();
  for (const search of state.searches || []) {
    const li = node('li', ''); li.append(node('strong', search.query), node('p', search.purpose)); $('searches').append(li);
  }
  $('search-note').textContent = state.status === 'queries_ready' ? (state.auto_advance ? 'Queries prepared. Starting research…' : 'Queries prepared. Searches have not been executed yet.') : '';
  const searchScreen = searching || ['queries_ready', 'search_failed'].includes(state.status);
  $('definition').hidden = !state.information_needed || searchScreen;
  $('requirements').replaceChildren();
  for (const item of state.information_needed || []) {
    const li = node('li', ''); li.append(node('strong', item.name), node('p', item.description)); $('requirements').append(li);
  }
  $('clarification').hidden = state.status !== 'clarification';
  if (state.status === 'clarification' && renderedQuestions !== JSON.stringify(state.questions)) {
    renderedQuestions = JSON.stringify(state.questions);
    $('questions').replaceChildren();
    state.questions.forEach((question, index) => {
      const label = node('label', question); label.htmlFor = `answer-${index}`;
      const input = document.createElement('textarea'); input.id = label.htmlFor; input.rows = 2; input.required = true; input.maxLength = 4000;
      input.value = sessionStorage.getItem(`${sessionId}.answer.${index}`) || '';
      input.addEventListener('input', () => sessionStorage.setItem(`${sessionId}.answer.${index}`, input.value));
      $('questions').append(label, input);
    });
    $('questions').querySelector('textarea')?.focus();
  }
  $('answer-form').querySelector('button').disabled = busy;
  $('answer-history').hidden = searchScreen || !(state.answers?.length);
  $('history').replaceChildren();
  for (const pair of state.answers || []) $('history').append(node('p', `${pair.question}\n${pair.answer}`));
  message(busy ? (searching ? 'Preparing two complementary searches…' : 'Working on your definition… You can leave this page and return.') : ['failed', 'search_failed', 'launch_failed'].includes(state.status) ? state.error : '', ['failed', 'search_failed', 'launch_failed'].includes(state.status));
  if (busy) timer = setTimeout(refresh, 1500);
}

/** Reload a durable session and recover gracefully from unavailable server/state.
 * Args: none; uses the locally stored session ID.
 * Returns: promise; resets a deleted session to the new-question form on 404.
 * Other failures remain visible without clearing saved context or making model calls.
 */
async function refresh() {
  try { render(await api(`/api/setup/${sessionId}`)); }
  catch (error) {
    if (error.status === 404) { newQuestion(); return; }
    message(error.message, true); $('new-question').disabled = false;
  }
}

/** Submit the research question once with a persistent idempotency identifier.
 * Args: event — browser form submission.
 * Returns: promise; saves run-wide budgets with the question, starts polling,
 * or restores the form after a rejected request.
 */
async function submitQuestion(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button'); button.disabled = true;
  sessionId = crypto.randomUUID(); localStorage.setItem('clarity.setup', sessionId);
  showActivity({label:'Submitting your question',tone:'working',busy:true});
  message('Defining what’s needed…');
  try { render(await api('/api/setup', {session_id: sessionId, question: $('question').value, auto_advance: true, model_request_budget: Number($('model-request-budget').value), model_token_budget: Number($('model-token-budget').value), research_rounds: Number($('research-rounds').value), extraction_concurrency: Number($('extraction-concurrency').value), selection_concurrency: Number($('selection-concurrency').value), triage_concurrency: Number($('triage-concurrency').value), investigation_concurrency: Number($('investigation-concurrency').value), link_ranking_concurrency: Number($('link-ranking-concurrency').value), report_selection_concurrency: Number($('report-selection-concurrency').value)})); }
  catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
}

/** Submit ordered clarification answers without changing their wording or binding.
 * Args: event — clarification form submission.
 * Returns: promise; shows revised definition after polling or preserves drafts on error.
 */
async function submitAnswers(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button'); button.disabled = true;
  const answers = [...$('questions').querySelectorAll('textarea')].map(input => input.value);
  try { render(await api(`/api/setup/${sessionId}/answers`, {answers})); }
  catch (error) { message(error.message, true); button.disabled = false; }
}

/** Reset only the visible form, preserving saved sessions and provider receipts.
 * Args: none.
 * Returns: nothing; clears the active-session pointer and cancels local polling.
 */
function newQuestion() {
  clearTimeout(timer); localStorage.removeItem('clarity.setup'); sessionId = null; renderedQuestions = ''; renderStages();
  $('intro').hidden = false; $('result').hidden = true; $('question').value = ''; message(''); $('question').focus();
}
$('question-form').addEventListener('submit', submitQuestion);
$('answer-form').addEventListener('submit', submitAnswers);
$('new-question').addEventListener('click', newQuestion);
if (sessionId) refresh();

/** Prepare queries once from the server's saved finalized definition.
 * Args: none.
 * Returns: promise; polls saved results without rerunning setup or executing searches.
 */
async function prepareSearches() {
  $('prepare-searches').disabled = true;
  try { render(await api(`/api/setup/${sessionId}/searches`, {})); }
  catch (error) { message(error.message, true); $('prepare-searches').disabled = false; }
}
$('prepare-searches').addEventListener('click', prepareSearches);

/** Start the prepared searches and navigate to the separate live run monitor.
 * Args: none.
 * Returns: promise; navigates after successful queueing, preserves setup on error.
 */
async function startRun() {
  $('start-run').disabled = true;
  try { await api(`/api/setup/${sessionId}/run`, {}); location.href = `/run/${sessionId}`; }
  catch (error) { message(error.message, true); $('start-run').disabled = false; }
}
$('start-run').addEventListener('click', startRun);

renderStages();

/** Reveal an invalid setting so the browser can focus its corrective message.
 * Args: none; uses the extraction limit field's native validity event.
 * Returns: nothing; opens settings without submitting or changing the value.
 */
function revealInvalidSettings(){ $('research-settings').open = true; }
for(const id of ['research-rounds','extraction-concurrency','selection-concurrency','triage-concurrency','investigation-concurrency','link-ranking-concurrency','report-selection-concurrency']) $(id).addEventListener('invalid', revealInvalidSettings);
