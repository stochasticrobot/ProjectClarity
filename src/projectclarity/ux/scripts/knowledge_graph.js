/* Explore the shared evidence graph with server-owned Cypher and inspect original assertions.
 * Cytoscape draws the returned evidence only; it does not infer relationships.
 */
'use strict';
let graph;
let explorationRevision = 0;
const researchSession = new URLSearchParams(location.search).get('session');
let researchState;
let contextTimer;

/** Read a page element without interpreting user content as HTML.
 * Args: id — fixed element identifier.
 * Returns: existing DOM element, or null when absent.
 */
function element(id) { return document.getElementById(id); }

/** Call the local collection API and report readable errors.
 * Args: path — same-origin route; data — optional JSON POST body.
 * Returns: parsed response; throws a sanitized error on unsuccessful requests.
 */
async function graphApi(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Check the graph filters.');
  return body;
}

/** Show the complete source assertion, not merely its shortened graph label.
 * Args: row — returned subject, assertion and optional object.
 * Returns: nothing; updates the text-only inspector and highlights and centers
 * the matching graph assertion and its edges. Replaces any previous selection.
 * On narrow screens, moves to the inspector so selection never appears inert.
 */
function inspectAssertion(row) {
  const a = row.assertion;
  if (graph) {
    graph.elements().removeClass('highlighted');
    graph.elements().unselect();
    const node = graph.getElementById(a.id);
    if (node.length) {
      node.select().addClass('highlighted');
      node.connectedEdges().addClass('highlighted');
      graph.center(node);
    }
  }
  for (const button of element('assertions').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.assertionId === a.id));
  }
  element('inspector').textContent = `${a.statement}\n\nQuote: ${a.quote}\nSource: ${a.source_url}\nReview: ${a.review_status}\nQuote rechecked: ${a.quote_rechecked}\nSession: ${a.session_id}\nRound: ${a.round}; document: ${a.document_id}; chunk: ${a.chunk_index}\nOriginal fact: ${a.source_fact_id}`;
  if (matchMedia('(max-width: 760px)').matches) element('evidence-inspector').scrollIntoView({block: 'start', behavior: 'auto'});
}

/** Retrieve a bounded graph and draw the actual Neo4j result with an accessible list.
 * Args: event — optional form submission to suppress browser navigation.
 * Returns: promise resolving after rendering or visible error; no research starts.
 */
async function explore(event) {
  if (event) event.preventDefault();
  const revision = ++explorationRevision;
  if (graph) { graph.destroy(); graph = null; }
  element('collection-sessions').replaceChildren();
  element('assertions').replaceChildren(); element('inspector').textContent = ''; element('cypher').textContent = '';
  try {
    if (researchSession) await loadResearchContext();
    await showCollectionSessions(revision);
    const query = new URLSearchParams({search: element('search').value, limit: element('limit').value});
    const path = researchState?.collection_id ? `/api/collections/${encodeURIComponent(researchState.collection_id)}/graph` : '/api/knowledge-graph';
    const data = await graphApi(`${path}?${query}`);
    if (revision !== explorationRevision) return;
    const nodes = new Map(), edges = [];
    for (const row of data.rows) {
      const a = row.assertion;
      for (const mention of [row.subject, row.object].filter(Boolean)) {
        nodes.set(mention.id, {data: {id: mention.id, label: mention.name, kind: 'mention'}});
      }
      nodes.set(a.id, {data: {id: a.id, label: a.predicate, kind: 'assertion', row}});
      edges.push({data: {id: `${a.id}:subject`, source: row.subject.id, target: a.id}});
      if (row.object) edges.push({data: {id: `${a.id}:object`, source: a.id, target: row.object.id}});
      const li = document.createElement('li'), button = document.createElement('button');
      button.type = 'button'; button.textContent = a.statement;
      button.dataset.assertionId = a.id; button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => inspectAssertion(row)); li.append(button); element('assertions').append(li);
    }
    graph = cytoscape({container: element('graph'), elements: [...nodes.values(), ...edges],
      style: [
        {selector: 'node', style: {'label': 'data(label)', 'font-size': 11, 'text-wrap': 'wrap', 'text-max-width': 110, 'background-color': '#3b82f6'}},
        {selector: 'node[kind="assertion"]', style: {'shape': 'diamond', 'background-color': '#d97706'}},
        {selector: 'edge', style: {'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'width': 1.5}},
        {selector: 'node.highlighted', style: {'background-color': '#facc15', 'border-width': 5, 'border-color': '#713f12', 'width': 44, 'height': 44, 'z-index': 10}},
        {selector: 'edge.highlighted', style: {'line-color': '#a16207', 'target-arrow-color': '#a16207', 'width': 4, 'z-index': 9}}
      ], layout: {name: 'cose', animate: false}});
    graph.on('tap', 'node[kind="assertion"]', (event) => inspectAssertion(event.target.data('row')));
    element('cypher').textContent = data.cypher + '\n\n' + JSON.stringify(data.parameters, null, 2);
    element('graph-message').textContent = data.rows.length ? `${data.rows.length} assertions displayed.${data.has_more ? ' More exist; narrow the search or raise the limit.' : ''}` : (element('search').value ? 'No assertions match this search.' : (researchSession ? 'No graph evidence is available for this research yet. See Contributing research for graph preparation status.' : 'No evidence in the graph yet. Start with a research question; older research has not been imported.'));
  } catch (error) { if (revision === explorationRevision) element('graph-message').textContent = error.message; }
}

/** Change graph magnification around its viewport center without rerunning a query.
 * Args: factor — positive zoom multiplier; values above one zoom in.
 * Returns: nothing; clamps magnification to the renderer's limits. Does nothing
 * before a graph is available, and preserves the current assertion highlight.
 */
function zoomGraph(factor) {
  if (!graph) return;
  graph.zoom({level: Math.max(graph.minZoom(), Math.min(graph.maxZoom(), graph.zoom() * factor)),
    renderedPosition: {x: graph.width() / 2, y: graph.height() / 2}});
}

element('zoom-in').addEventListener('click', () => zoomGraph(1.25));
element('zoom-out').addEventListener('click', () => zoomGraph(1 / 1.25));
element('graph-filters').addEventListener('submit', explore);
explore();

/** List contributing sessions and expose an explicit projection retry.
 * Args: revision — current exploration request number, used to ignore stale replies.
 * Returns: promise after rendering; errors propagate to the explorer status.
 */
async function showCollectionSessions(revision) {
  const path = researchState?.collection_id ? `/api/collections/${encodeURIComponent(researchState.collection_id)}/sessions` : '/api/knowledge-graph/sessions';
  const rows = await graphApi(path);
  if (revision !== explorationRevision) return;
  element('collection-sessions').replaceChildren();
  for (const row of rows) {
    const li = document.createElement('li'), button = document.createElement('button');
    li.append(document.createTextNode(`${row.question} — graph ${row.graph.status} `));
    button.type = 'button'; button.textContent = 'Refresh saved evidence';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await graphApi(`/api/setup/${row.session_id}/graph-sync`, {});
        if (result.status !== 'completed') throw new Error(result.error);
        await explore();
      } catch (error) { element('graph-message').textContent = error.message; }
      finally { button.disabled = false; }
    });
    li.append(button); element('collection-sessions').append(li);
  }
}

/** Read background task status to keep the report link and available graph current.
 * Args: none; the session identifier comes from the workflow navigation URL.
 * Returns: promise after rendering the research question and report link. Polls only while
 * work is active; report updates do not redraw the graph or clear the inspector.
 */
async function loadResearchContext() {
  clearTimeout(contextTimer);
  researchState = await graphApi(`/api/setup/${encodeURIComponent(researchSession)}`);
  if (typeof renderCaptcha === 'function') renderCaptcha(researchState);
  element('research-context').hidden = false;
  element('research-question').textContent = researchState.research_question || researchState.original_question;
  const graphStatus = researchState.knowledge_graph?.status || 'not prepared';
  const reportStatus = researchState.report?.status;
  element('open-report').hidden = !reportStatus;
  element('open-report').textContent = reportStatus === 'running' ? 'Report progress →' : 'View report →';
  element('open-report').href = `/report/${researchSession}`;
  const busy = ['queued', 'running'].includes(graphStatus) || reportStatus === 'running' ||
    ['working', 'search_working', 'run_running', 'selection_running', 'triage_running', 'investigation_running', 'link_ranking_running'].includes(researchState.status);
  if (busy) contextTimer = setTimeout(pollResearchContext, 1500);
}

/** Refresh progress without disturbing an assertion the user is reading.
 * Args: none; uses the current saved session context.
 * Returns: promise; redraws only when a pending graph first becomes available.
 * Errors are shown in the status region and do not start research or reporting.
 */
async function pollResearchContext() {
  const previous = researchState?.knowledge_graph?.status;
  try {
    await loadResearchContext();
    if (previous !== 'completed' && researchState.knowledge_graph?.status === 'completed') await explore();
  } catch (error) { element('graph-message').textContent = error.message; }
}
