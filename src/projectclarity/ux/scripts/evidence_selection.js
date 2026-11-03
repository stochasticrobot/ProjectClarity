/*
 * Let the user see which extracted facts were considered useful and why.
 *
 * The application may read many facts that do not answer the user's question. This
 * file fills the evidence-selection page with the facts that were reviewed, the
 * choices made and the reasons given. Filters help the user inspect those choices
 * without changing the saved results.
 *
 * The page updates as selection progresses and offers the relevant start or stop
 * actions. The application does the selection elsewhere; this file makes that work
 * visible. A fact shown as selected is potentially useful, not necessarily proved.
 */
'use strict';
const sid = location.pathname.split('/').pop();
const el = id => document.getElementById(id);
let timer, lastRevision = '', facts = [], resumeArtifact = null;
/** Create literal text without interpreting source content as markup.
 * Args: tag — DOM element name; text — displayed text.
 * Returns: detached safe element.
 */
function node(tag, text) { const n=document.createElement(tag); n.textContent=text; return n; }
/** Read or post to this session's local API.
 * Args: suffix — fixed API path suffix; post — true only for explicit actions; payload — validated action parameters.
 * Returns: decoded response; throws a user-facing error for failed requests.
 */
async function api(suffix='',post=false,payload={}){const r=await fetch(`/api/setup/${sid}${suffix}`,post?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Request failed.');return d;}
/** Display observed counts and independently retain upstream failures.
 * Args: state — current saved session snapshot.
 * Returns: nothing; updates progress without starting work.
 */
function render(state){
  renderStages(state);
  if(state.triage && !new URLSearchParams(location.search).has('stay')){location.replace(`/triage/${sid}`);return;}
  const s=state.selection;
  resumeArtifact=s?.artifact_directory;
  el('resume-controls').hidden=!!state.triage||!resumeArtifact||!['cancelled','interrupted','failed','partial'].includes(s?.status);
  el('triage-next').hidden=!s||s.status==='running';
  el('start-triage').hidden=!!state.triage||!['completed','partial','cancelled','interrupted'].includes(s?.status);
  el('view-triage').hidden=!state.triage;el('view-triage').href=`/triage/${sid}`;
  el('question').textContent=state.research_question||state.original_question;
  el('requirements').replaceChildren();for(const r of state.information_needed||[]){const li=node('li','');li.append(node('strong',r.name),node('p',r.description));el('requirements').append(li);}
  const upstream=state.run?.status;
  el('upstream').hidden=!upstream||upstream==='completed';
  el('upstream').textContent=`Source collection / extraction was ${upstream}. Selection covers only its accepted knowledge—not skipped sources or failed extraction chunks.`;
  el('start').hidden=!!s||!['completed','partial','cancelled','interrupted'].includes(upstream);
  el('stop').hidden=s?.status!=='running';el('stop').disabled=!!state.stop_requested;
  el('status').textContent=s ? (s.error||(state.stop_requested&&s.status==='running'?'Stopping after the current request finishes…':s.status==='running'?'Selecting potentially relevant evidence…':`Evidence selection ${s.status}.`)) : 'Selection has not started. Use the saved extracted knowledge without repeating searches.';
  const total=s?.facts_total||0,reviewed=s?.facts_reviewed||0,selected=s?.facts_selected||0;
  const finished=s?.batches_finished||0,batches=s?.batches_total||0,passed=s?.batches_passed||0;
  el('counts').replaceChildren();for(const [label,value] of [['Available facts',total],['Reviewed',reviewed],['Selected',selected],['Not selected',reviewed-selected],['Unreviewed',total-reviewed]]){const d=node('div','');d.append(node('strong',value),node('span',label));el('counts').append(d);}
  el('flow').replaceChildren();for(const [label,count,cls] of [['Selected',selected,'selected'],['Not selected',reviewed-selected,'unselected'],['Unreviewed',total-reviewed,'unreviewed']]){const d=node('div','');d.className=`flow-part ${cls}`;const meter=document.createElement('progress');meter.max=Math.max(1,total);meter.value=count;meter.setAttribute('aria-label',`${label}: ${count} of ${total}`);d.append(node('strong',`${count} ${label.toLowerCase()}`),meter);el('flow').append(d);}
  el('batch-progress').max=Math.max(1,batches);el('batch-progress').value=finished;
  el('progress-label').textContent=`${finished}/${batches} chunks finished · ${passed} successful · ${finished-passed} failed`;
  el('batches').replaceChildren();for(const b of s?.batches||[]){const tr=node('tr',''),td=node('td',''),a=node('a',b.source_url);a.href=b.source_url;a.target='_blank';a.rel='noopener noreferrer';td.append(a,node('small',`Chunk ${b.chunk_index+1}`));const status=node('td',b.status);if(b.error)status.append(node('small',b.error));tr.append(td,status,node('td',b.facts),node('td',b.selected===null?'—':b.selected));el('batches').append(tr);}
  el('events').replaceChildren();for(const e of [...(s?.events||[])].reverse()){const li=node('li','');li.append(node('time',new Date(e.time).toLocaleTimeString()),node('span',e.message));el('events').append(li);}
}
/** Render filtered exact facts and model inclusion reasons, with original quotes.
 * Args: none; reads cached decisions and user filters.
 * Returns: nothing; no model calls, decisions or records are changed.
 */
function renderFacts(){
  const filter=el('filter').value,source=el('source-filter').value,search=el('find').value.toLowerCase();
  const visible=facts.filter(f=>(filter==='all'||f.selected===(filter==='selected'))&&(source==='all'||f.source.source_url===source)&&`${f.fact.statement} ${f.reason||''} ${f.fact.quote}`.toLowerCase().includes(search));
  el('shown').textContent=`${visible.length} facts shown`;el('evidence-facts').replaceChildren();
  if(!visible.length)el('evidence-facts').append(node('p',facts.length?'No reviewed facts match these filters.':'No reviewed facts yet. Failed and pending chunks are not counted as exclusions.'));
  for(const f of visible){const card=node('article','');card.className='evidence-card';const badge=node('span',f.selected?'Selected':'Not selected');badge.className=f.selected?'badge selected':'badge unselected';card.append(badge,node('p',f.fact.statement));if(f.selected){const reason=node('div','');reason.className='selection-reason';reason.append(node('strong','Why included'),node('p',f.reason));card.append(reason);}card.append(node('blockquote',f.fact.quote));const link=node('a',`Source · chunk ${f.source.chunk_index+1}${f.source.pages?.length?' · PDF page(s) '+ f.source.pages.join(', '):''}`);link.href=f.source.source_url;link.target='_blank';link.rel='noopener noreferrer';const details=node('details','');details.append(node('summary','Record details'),node('p',`Fact ID: ${f.fact_id}`),node('p',`Relationship: ${f.fact.predicate}`));card.append(link,details);el('evidence-facts').append(card);}
}
/** Refresh progress and newly completed decisions, without replaying selection.
 * Args: none.
 * Returns: promise; polls only while active, retries read failures visibly.
 */
async function refresh(){clearTimeout(timer);try{const state=await api();if(followCurrentRound(state))return;render(state);const s=state.selection;const revision=JSON.stringify([s?.artifact_directory,s?.batches_finished,s?.status]);if(revision!==lastRevision){const data=await api('/evidence');facts=data.batches.flatMap(b=>b.facts.map(f=>({...f,source:b.source})));const prior=el('source-filter').value;el('source-filter').replaceChildren(new Option('All sources','all'));for(const url of new Set(facts.map(f=>f.source.source_url)))el('source-filter').append(new Option(url,url));el('source-filter').value=[...el('source-filter').options].some(o=>o.value===prior)?prior:'all';renderFacts();lastRevision=revision;}if(s?.status==='running'||['run_running','selection_running'].includes(state.status))timer=setTimeout(refresh,1200);}catch(error){showActivityIssue(true);el('status').textContent=error.message+' Retrying status…';timer=setTimeout(refresh,3000);}}
/** Submit explicit start or stop while preserving already saved decisions.
 * Args: suffix — selection or stop endpoint; button — action button to disable.
 * Returns: promise; refreshes observed state, showing any rejected action.
 */
async function action(suffix,button){button.disabled=true;try{await api(suffix,true);await refresh();}catch(e){showActivityIssue();el('status').textContent=e.message;}finally{button.disabled=false;}}
el('extraction-link').href=`/run/${sid}?stay=1`;
el('start').addEventListener('click',()=>action('/selection',el('start')));
el('stop').addEventListener('click',()=>action('/stop',el('stop')));
/** Start triage of saved selection, then show its own screen.
 * Args: none.
 * Returns: promise; navigates on success or reports a rejected request.
 */
async function startTriage(){el('start-triage').disabled=true;try{await api('/triage',true);location.href=`/triage/${sid}`;}catch(e){showActivityIssue();el('status').textContent=e.message;el('start-triage').disabled=false;}}
el('start-triage').addEventListener('click',startTriage);
for(const id of ['filter','source-filter','find'])el(id).addEventListener('input',renderFacts);
const resumeControls=node('section','');resumeControls.id='resume-controls';resumeControls.hidden=true;
const resumeLabel=node('label','Concurrent selection calls');resumeLabel.htmlFor='resume-concurrency';
const resumeLimit=node('input','');resumeLimit.id='resume-concurrency';resumeLimit.type='number';resumeLimit.min='1';resumeLimit.max='16';resumeLimit.step='1';resumeLimit.value='6';
const resumeButton=node('button','Resume unfinished selection →');resumeButton.id='resume-selection';
resumeControls.append(resumeLabel,resumeLimit,resumeButton,node('p','Reuses checked batches. Search and extraction are not repeated.'));
el('stop').after(resumeControls);
/** Resume a displayed stopped attempt only after an explicit action.
 * Args: none; reads the saved attempt token and user-selected limit.
 * Returns: promise; refreshes progress or shows errors without replaying upstream work.
 */
async function resumeSelection(){
 if(!resumeLimit.reportValidity())return;
 resumeButton.disabled=true;
 try{await api('/selection/resume',true,{previous_artifact:resumeArtifact,concurrency:Number(resumeLimit.value)});await refresh();}
 catch(error){showActivityIssue();el('status').textContent=error.message;}
 finally{resumeButton.disabled=false;}
}
resumeButton.addEventListener('click',resumeSelection);
refresh();
