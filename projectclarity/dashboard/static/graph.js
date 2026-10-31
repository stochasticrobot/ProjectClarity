/** Browser-only view of the dashboard graph API, loaded by graph.html.
 * Backend graph.py supplies evidence and graph_grouping.py owns consolidation;
 * this file only filters, lays out and inspects that saved projection. Expanding
 * Mila, for example, reveals saved neighbors rather than launching research.
 * No inference, persistence writes or model calls occur. Refresh resets history.
 */
'use strict';
const runId = new URLSearchParams(location.search).get('run');
let graph, cy, selected, listLimit = 60, focusId = null;
/** View-only exploration state; saved graph elements are never deleted. */
let baseView=null, expansions=new Map(), viewHistory=[], appliedControls={};
const FILTER_CONTROLS=['search','kind','status','relationship'];
/** Match an edge against exact saved relationship labels and evidence status.
 * Args: edge: Saved edge with category, label and status.
 * Returns: Boolean; unknown statuses count as unreviewed, never supported.
 */
function edgeMatches(edge) {
  const relation=el('relationship').value,status=el('status').value;
  return (!relation||JSON.stringify([edge.category,edge.label])===relation)&&
    (!status||(status==='unreviewed'?!['supported','invalid_evidence'].includes(edge.status):edge.status===status));
}
/** Save a reversible exploration checkpoint before changing the visible scope.
 * Args: None; reads current scope, applied filters, selection and canvas positions.
 * Returns: Nothing. Appends a bounded history entry, including viewport and layout.
 */
function rememberView() {
  viewHistory.push({base:baseView?[...baseView]:null,expansions:[...expansions].map(([k,v])=>[k,[...v]]),
    focusId,selected,controls:{...appliedControls},layout:el('layout-mode').value,
    positions:cy.nodes().map(n=>({id:n.id(),position:{...n.position()}})),zoom:cy.zoom(),pan:{...cy.pan()}});
  if(viewHistory.length>30)viewHistory.shift();
}
/** Restore the previous exploration without discarding evidence or rerunning research.
 * Args: None; consumes the newest saved checkpoint if present.
 * Returns: Nothing. Restores filters, expansion state, positions, inspector and viewport.
 */
function backView() {
  const previous=viewHistory.pop();if(!previous)return;
  baseView=previous.base?new Set(previous.base):null;expansions=new Map(previous.expansions.map(([k,v])=>[k,new Set(v)]));
  focusId=previous.focusId;selected=previous.selected;
  for(const id of FILTER_CONTROLS)el(id).value=previous.controls[id]||'';
  el('layout-mode').value=previous.layout;
  applyFilters();cy.elements().removeClass('highlight label-selected');if(selected&&cy.getElementById(selected).visible())selectNode(selected);else el('inspector').innerHTML='<h2>Explore a discovery</h2><p>Select a node or connection.</p>';
  cy.batch(()=>previous.positions.forEach(p=>cy.getElementById(p.id).position(p.position)));
  cy.viewport({zoom:previous.zoom,pan:previous.pan});
}
/** Collect a node and immediate neighbors through currently allowed connections.
 * Args: id: Saved node identifier.
 * Returns: Set of IDs; follows either endpoint without inferring new relationships.
 */
function neighborsFor(id) {
  const ids=new Set([id]);for(const edge of graph.edges)if(edgeMatches(edge)&&(edge.source===id||edge.target===id)){
    ids.add(edge.source);ids.add(edge.target);
  }return ids;
}
/** Focus, expand or collapse one neighborhood using reversible scope operations.
 * Args: id: Selected node; action: focus, expand or collapse.
 * Returns: Nothing. Keeps overlapping expansions intact; clears name/type search
 * when revealing nodes, retains relationship/evidence filters and adds a Back step.
 */
function exploreNode(id,action) {
  rememberView();
  if(action==='focus'){baseView=neighborsFor(id);expansions.clear();focusId=id;}
  else if(action==='expand'){
    if(baseView===null)baseView=new Set(cy.nodes(':visible').map(n=>n.id()));
    expansions.set(id,neighborsFor(id));
  }else expansions.delete(id);
  for(const control of ['search','kind'])el(control).value='';
  applyFilters();arrange();selectNode(id);
}
/** Summarize evidence without assigning a group's strongest status to every record.
 * Args: node: Saved node with original observation references.
 * Returns: Escaped status/count text, or an explicit no-evidence description.
 */
function statusSummary(node) {
  const counts={};for(const id of node.observation_ids||[]){const status=graph.observations[id]?.status||'unreviewed';counts[status]=(counts[status]||0)+1;}
  return Object.entries(counts).map(([status,count])=>`${count} ${escapeText(status.replaceAll('_',' '))}`).join(' · ')||'No attached evidence';
}

/** Fixed local SVG symbols; no external images, fonts or requests are needed. */
const NODE_VISUALS = {
  person: {label:'Person',color:'#2563a6',shape:'ellipse',path:'<circle cx="12" cy="7" r="3"/><path d="M5 21v-3a7 7 0 0 1 14 0v3z"/>'},
  institution: {label:'Institution / organization',color:'#7652a3',shape:'round-rectangle',path:'<path d="M3 8l9-5 9 5v2H3zM4 20h16v2H4zM5 11h3v8H5zM11 11h3v8h-3zM17 11h3v8h-3z"/>'},
  place: {label:'Place / district',color:'#087f73',shape:'ellipse',path:'<path fill-rule="evenodd" d="M12 2a8 8 0 0 0-8 8c0 6 8 12 8 12s8-6 8-12a8 8 0 0 0-8-8zm0 4a3 3 0 1 1 0 6 3 3 0 0 1 0-6z"/>'},
  statement: {label:'Claim / statement',color:'#a05d17',shape:'round-rectangle',path:'<path d="M3 3h18v14H10l-5 4v-4H3z"/><path stroke="#a05d17" stroke-width="2" d="M7 7h10M7 11h8"/>'},
  other: {label:'Other / unspecified',color:'#64748b',shape:'diamond',path:'<circle cx="12" cy="6" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="18" r="2"/>'}
};
/** Classify saved types for presentation without changing identity or evidence.
 * Args: node: Saved graph node, optionally with a label-defining first record field.
 * Returns: Visual category; unrecognized types stay explicitly unspecified. No inference calls.
 */
function nodeCategory(node) {
  const kind=String(node.kind||'').toLowerCase().replaceAll('_',' ').replace(/\s+/g,' ').trim();
  const first=node.fields?.[0]?.name;
  if(['person','human','individual','researcher','member of parliament','mp'].includes(kind)||['person_name','member_name','researcher_name'].includes(first))return 'person';
  if(['organization','organisation','institution','university','college','school','company','political party','party','lab','laboratory','research lab','research laboratory','research institute','academic institution','academic department','academic faculty','university research institute','organization name','organisation name','institution name','affiliation','affiliation name','party affiliation','employer','legislative body','legislative chamber'].includes(kind))return 'institution';
  if(['place','location','city','town','country','province','state','region','district','electoral district','federal electoral district','electoral district (former name)','electoral district (new name)'].includes(kind))return 'place';
  if(node.origin==='claim'||kind==='statement')return 'statement';
  return 'other';
}
/** Build a self-contained trusted symbol shared by the canvas, legend and list.
 * Args: category: Key from NODE_VISUALS, never source-provided SVG markup.
 * Returns: SVG data URI; does not fetch any resource or modify graph data.
 */
function nodeIcon(category) {
  return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="white">${NODE_VISUALS[category].path}</svg>`);
}
/** Render an accessible decorative badge beside a textual category/name.
 * Args: node: Saved node classified with nodeCategory.
 * Returns: Trusted fixed-icon markup; adjacent text supplies its accessible meaning.
 */
function nodeBadge(node) {
  const category=nodeCategory(node),visual=NODE_VISUALS[category];
  return `<span class="node-badge badge-${category}"><img src="${nodeIcon(category)}" alt=""></span>`;
}

/** Look up a fixed UI control.
 * Args: id: Document element ID.
 * Returns: Matching element, or null; no mutation.
 */
function el(id) { return document.getElementById(id); }
/** Encode untrusted source text for inert HTML display.
 * Args: value: Saved label, quotation or metadata.
 * Returns: Escaped text; never an executable fragment.
 */
function escapeText(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
/** Link a source only when its scheme is safe.
 * Args: source: Saved source URL or local document label.
 * Returns: Escaped link or plain text, without navigating or fetching.
 */
function sourceLink(source) { return /^https?:\/\//i.test(source || '') ? `<a href="${escapeText(source)}" target="_blank" rel="noopener noreferrer">${escapeText(source)}</a>` : escapeText(source); }
/** Render one evidence observation without hiding its review state.
 * Args: o: Saved extraction or database observation.
 * Returns: Inert inspector markup, including available source quotations.
 */
function evidenceHTML(o) {
  const quotes=Array.isArray(o.evidence)?o.evidence:(o.evidence?[o.evidence]:[]);
  return `<article><span class="pill">${escapeText(o.status)}</span><p>${escapeText(o.statement)}</p>${o.value!=null?`<p>Value: ${escapeText(o.value)}</p>`:''}<div class="fine">${sourceLink(o.source)}</div>${quotes.map(q=>`<blockquote>${escapeText(q.quote || q.text || (typeof q==='string'?q:JSON.stringify(q)))}</blockquote>`).join('')}${(o.qualifications||[]).map(q=>`<p class="fine">${escapeText(q)}</p>`).join('')}${o.review_reason?`<details><summary>Review reasoning</summary><p>${escapeText(o.review_reason)}</p></details>`:''}<div class="fine">${escapeText(o.id)} · ${escapeText(o.origin)}</div></article>`;
}
/** Select a node from canvas or keyboard list and expose all its observations.
 * Args: id: Existing graph-node key.
 * Returns: Nothing. Updates selection, viewport and detail panel. Hidden-node navigation reveals
 * that node within the current scope and saves a Back checkpoint; no graph data changes.
 */
function selectNode(id) {
  const n=graph.nodes.find(n=>n.id===id);if(!n)return;
  if(cy.getElementById(id).style('display')==='none'){rememberView();if(baseView!==null)baseView.add(id);for(const control of FILTER_CONTROLS)el(control).value='';applyFilters();}
  selected=id;cy.elements().removeClass('highlight label-selected');const item=cy.getElementById(id);item.closedNeighborhood().addClass('highlight');item.addClass('label-selected');cy.center(item);
  const neighbors=graph.edges.filter(e=>e.source===id||e.target===id);
  const neighborIds=[...new Set(neighbors.map(e=>e.source===id?e.target:e.source))];
  const fields=(n.fields||[]).map(f=>`<dt>${escapeText(f.name.replaceAll('_',' '))}</dt><dd>${escapeText(f.value)}</dd>`).join('');
  el('inspector').innerHTML=`<div class="eyebrow">${escapeText(n.origin)}</div><h2>${nodeBadge(n)} ${escapeText(n.label)}</h2><p>${escapeText(n.kind)}</p><p class="evidence-summary">${statusSummary(n)}</p><p class="fine">${escapeText(n.identity||'Saved statement, not an entity identity.')}</p><p>${(n.sources||[n.source]).map(sourceLink).join('<br>')}</p>${n.members?`<details><summary>${n.members.length} original records · ${n.sources.length} sources</summary>${n.members.map(m=>`<p><strong>${escapeText(m.label)}</strong> · ${escapeText(m.kind)}<br>${sourceLink(m.source)}<br>${statusSummary(m)}</p><dl>${(m.fields||[]).map(f=>`<dt>${escapeText(f.name)}</dt><dd>${escapeText(f.value)}</dd>`).join('')}</dl><p class="fine">${(m.observation_ids||[]).map(escapeText).join(' · ')}</p>`).join('')}</details><p class="fine">Labels: ${n.aliases.map(escapeText).join(' · ')}</p>`:''}${n.alias_evidence_ids?.length?`<details><summary>Saved evidence for the abbreviation</summary>${n.alias_evidence_ids.map(oid=>evidenceHTML(graph.observations[oid])).join('')}</details>`:''}<dl>${fields}</dl><button id="focus-node">Focus this neighborhood</button><button id="expand-node">Expand connections</button><button id="collapse-node" ${expansions.has(id)?'':'disabled'}>Collapse this expansion</button><h3>Neighbors (${neighborIds.length})</h3><p class="fine">${neighbors.length} saved connections; repeated evidence does not add another neighbor.</p>${neighborIds.map(key=>{const other=graph.nodes.find(n=>n.id===key);const labels=[...new Set(neighbors.filter(e=>e.source===key||e.target===key).map(e=>e.label))];return `<button class="neighbor" data-node="${escapeText(other.id)}">${escapeText(labels.join(' / '))} → ${escapeText(other.label)}</button>`;}).join('')||'<p class="fine">No explicit connections were captured.</p>'}<h3>Observations (${n.observation_ids.length})</h3>${n.observation_ids.map(oid=>evidenceHTML(graph.observations[oid])).join('')||'<p>This entity label was extracted without an attached claim. It is not verified.</p>'}`;
  el('focus-node').onclick=()=>exploreNode(id,'focus');el('expand-node').onclick=()=>exploreNode(id,'expand');el('collapse-node').onclick=()=>exploreNode(id,'collapse');
}
/** Inspect one explicit relationship or recorded-field association.
 * Args: id: Edge identifier from a canvas tap.
 * Returns: Nothing. Displays provenance and the distinction from inferred semantics.
 */
function selectEdge(id) {
  const edge=graph.edges.find(e=>e.id===id);if(!edge)return;
  cy.elements().removeClass('label-selected');cy.getElementById(id).addClass('label-selected');
  const a=graph.nodes.find(n=>n.id===edge.source),b=graph.nodes.find(n=>n.id===edge.target);
  el('inspector').innerHTML=`<h2>${escapeText(edge.label)}</h2><p>${escapeText(a.label)} → ${escapeText(b.label)}</p><p class="fine">${edge.category==='record_association'?'Recorded field association, not a separately extracted relationship claim.':'Explicit relationship proposed in saved evidence.'}</p>${evidenceHTML(graph.observations[edge.observation_id])}`;
}
/** Apply view-only filters and provide a fully pageable keyboard node list.
 * Args: None; reads name/type/evidence/relationship controls and expansion scope.
 * Returns: Nothing. Hides nonmatching nodes/edges, updates the pageable list,
 * visible counts and Back availability, and remembers the applied filter values.
 * Relationship filters use exact category/label pairs, never synonym inference.
 * Saved graph records are not dropped; isolated nodes remain without a relation filter.
 */
function applyFilters() {
  const query=el('search').value.toLocaleLowerCase(),kind=el('kind').value,status=el('status').value;
  const neighborhood=baseView===null?null:new Set([...baseView,...[...expansions.values()].flatMap(ids=>[...ids])]);
  const allowedEdges=graph.edges.filter(edgeMatches),endpoints=new Set(allowedEdges.flatMap(e=>[e.source,e.target]));
  const relation=el('relationship').value;
  const matches=graph.nodes.filter(n=>(!query||[n.label,...(n.aliases||[])].some(label=>label.toLocaleLowerCase().includes(query)))&&(!kind||n.kind===kind)&&(!status||n.statuses.some(s=>status==='unreviewed'?!['supported','invalid_evidence'].includes(s):s===status))&&(!neighborhood||neighborhood.has(n.id))&&(!relation||endpoints.has(n.id)));
  const ids=new Set(matches.map(n=>n.id));
  cy.batch(()=>{cy.nodes().forEach(n=>n.style('display',ids.has(n.id())?'element':'none'));cy.edges().forEach(e=>e.style('display',ids.has(e.source().id())&&ids.has(e.target().id())&&edgeMatches(e.data())?'element':'none'));});
  el('node-list').innerHTML=matches.slice(0,listLimit).map(n=>`<button data-node="${escapeText(n.id)}">${nodeBadge(n)} ${escapeText(n.label)}${n.members?` <span class="group-count" title="Original records">×${n.members.length}</span>`:''}<small>${escapeText(n.kind)}</small></button>`).join('')||'<p>No nodes match.</p>';
  el('list-count').textContent=`${matches.length} of ${graph.nodes.length} nodes${baseView?' · exploring':''}${expansions.size?' · '+expansions.size+' expansions':''}`;
  el('more').hidden=matches.length<=listLimit;
  el('graph-status').textContent=`${graph.nodes.length} displayed nodes (${graph.grouping?.original_nodes ?? graph.nodes.length} original records) · ${graph.edges.length} connections · ${graph.counts.observations} observations. Showing ${matches.length} nodes and ${cy.edges(':visible').length} connections.`;
  appliedControls=Object.fromEntries(FILTER_CONTROLS.map(id=>[id,el(id).value]));el('explore-back').disabled=!viewHistory.length;
}
/** Apply the node-label preference without exposing every relationship label.
 * Args: None; reads the Show all names checkbox and current canvas.
 * Returns: Nothing. Names otherwise appear only on hovered/selected nodes;
 * relationship text appears only on a selected edge. Saved data is unchanged.
 */
function applyLabels() {
  if(cy)cy.nodes().toggleClass('all-labels',!!el('all-labels').checked);
}
/** Arrange only visible elements using bundled Cytoscape layouts.
 * Args: None; reads layout-mode and the focused (or selected) visible root.
 * Returns: Nothing. Fits a radial or hierarchical view, or an automatic overview.
 * Hierarchy is visual graph distance, not an inferred organizational rank. Empty
 * views are left alone; hidden nodes and all saved records remain unchanged.
 */
function arrange() {
  const visible=cy.elements(':visible'),nodes=visible.nodes();
  if(!nodes.length)return;
  const root=nodes.filter(n=>n.id()===(focusId||selected));
  const mode=el('layout-mode').value||'auto';
  const common={animate:false,fit:true,padding:45,avoidOverlap:true};
  if(mode==='hierarchical'){
    const options={...common,name:'breadthfirst',directed:false,spacingFactor:1.3};
    if(root.length)options.roots=root;
    visible.layout(options).run();
  }else if(mode==='radial'||focusId||nodes.length>=220){
    const degree=n=>n.connectedEdges().intersection(visible.edges()).length;
    visible.layout({...common,name:'concentric',minNodeSpacing:25,
      concentric:n=>root.length?(n.id()===root.id()?2:1):degree(n),
      levelWidth:()=>root.length?1:4}).run();
  }else{
    visible.layout({...common,name:'cose',numIter:300}).run();
  }
}
/** Fetch and replace the selected run projection; refresh never launches research.
 * Args: None; reads validated run query parameter.
 * Returns: Promise resolving after render and history reset; failures stay visibly
 * reported. Replaces the canvas and grouping options but does not modify receipts.
 */
async function loadGraph() {
  el('reload').disabled=true;
  try {
    if(!runId||!/^[A-Za-z0-9_-]+$/.test(runId))throw Error('Choose a run from the main dashboard.');
    const response=await fetch(`/api/runs/${encodeURIComponent(runId)}/graph?group=${el('group-organizations').checked?'organizations':'none'}`);if(!response.ok)throw Error('Graph data is unavailable. Refresh to try again.');
    graph=await response.json();el('node-legend').innerHTML=Object.entries(NODE_VISUALS).map(([category,v])=>`<span>${nodeBadge({kind:category==='institution'?'organization':category==='other'?'unknown':category})} ${escapeText(v.label)}</span>`).join('');el('question').textContent=graph.question;el('scope').textContent=graph.scope;el('identity').textContent=graph.identity_policy;
    el('graph-warning').textContent=graph.warnings.join(' ');
    el('back').href=`/#${encodeURIComponent(runId)}`;
    el('kind').innerHTML='<option value="">All types</option>'+[...new Set(graph.nodes.map(n=>n.kind))].sort().map(k=>`<option>${escapeText(k)}</option>`).join('');
    if(cy)cy.destroy();focusId=null;baseView=null;expansions.clear();viewHistory=[];
    el('relationship').innerHTML='<option value="">All connections</option>'+[...new Set(graph.edges.map(e=>JSON.stringify([e.category,e.label])))].sort().map(key=>{const [category,label]=JSON.parse(key);return `<option value="${escapeText(key)}">${escapeText(label)} (${category==='record_association'?'record field':'relationship'})</option>`;}).join('');
    cy=cytoscape({container:el('cy'),elements:[...graph.nodes.map(n=>({data:{id:n.id,label:n.label,origin:n.origin,category:nodeCategory(n),color:NODE_VISUALS[nodeCategory(n)].color,shape:NODE_VISUALS[nodeCategory(n)].shape,icon:nodeIcon(nodeCategory(n))}})),...graph.edges.map(e=>({data:e}))],style:[{selector:'node',style:{'background-color':'data(color)',shape:'data(shape)','background-image':'data(icon)','background-width':'70%','background-height':'70%',label:'','text-valign':'bottom','text-margin-y':6,'text-background-color':'#fff','text-background-opacity':0.85,'text-background-padding':2,'font-size':11,width:30,height:30,'text-wrap':'ellipsis','text-max-width':140}},{selector:'edge',style:{width:1,'line-color':'#b7791f','target-arrow-color':'#b7791f','target-arrow-shape':'triangle','curve-style':'bezier','font-size':9}},{selector:'edge[category="record_association"]',style:{'line-style':'dashed'}},{selector:'.highlight',style:{'z-index':10}},{selector:'node.hovered, node.label-selected, node.all-labels, edge.label-selected',style:{label:'data(label)','z-index':20}},{selector:'node.highlight',style:{'border-width':2,'border-color':'#1d3347'}},{selector:'edge[status="supported"]',style:{'line-color':'#26836b','target-arrow-color':'#26836b'}},{selector:'edge[status="invalid_evidence"]',style:{'line-color':'#c56a61','target-arrow-color':'#c56a61','line-style':'dotted'}}],layout:{name:'grid'}});
    cy.on('mouseover','node',e=>e.target.addClass('hovered'));cy.on('mouseout','node',e=>e.target.removeClass('hovered'));
    cy.on('tap','node',e=>selectNode(e.target.id()));cy.on('tap','edge',e=>selectEdge(e.target.id()));
    applyFilters();applyLabels();arrange();if(selected&&graph.nodes.some(n=>n.id===selected))selectNode(selected);
  }catch(error){el('graph-warning').textContent=error.message;el('graph-status').textContent='Graph unavailable.';}finally{el('reload').disabled=false;}
}
/** Wire accessible controls without polling or opening a separate browser panel.
 * Args: None; static page elements must exist.
 * Returns: Nothing. Starts one read-only graph fetch and registers UI callbacks.
 */
function startExplorer() {
  el('all-labels').onchange=applyLabels;el('layout-mode').onchange=()=>cy&&arrange();
  el('reload').onclick=loadGraph;el('group-organizations').onchange=()=>{selected=null;loadGraph();};el('fit').onclick=()=>cy&&cy.fit(cy.elements(':visible'),35);el('layout').onclick=()=>cy&&arrange();
  el('explore-back').onclick=backView;
  el('reset').onclick=()=>{rememberView();focusId=null;baseView=null;expansions.clear();for(const id of FILTER_CONTROLS)el(id).value='';listLimit=60;applyFilters();arrange();};
  for(const id of FILTER_CONTROLS.filter(id=>id!=='search'))el(id).addEventListener('change',()=>{if(graph){rememberView();listLimit=60;applyFilters();}});
  el('search').addEventListener('input',()=>{if(graph){rememberView();listLimit=60;applyFilters();}});
  el('more').onclick=()=>{listLimit+=60;applyFilters();};
  document.addEventListener('click',e=>{const button=e.target.closest('[data-node]');if(button)selectNode(button.dataset.node);});
  loadGraph();
}
startExplorer();
