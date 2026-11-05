/*
 * Present the finished research answer and let the user inspect its evidence.
 *
 * This file fills the report page with the entries, requested details, gaps and
 * supporting information prepared by the application. The user can filter the
 * entries, follow references back to saved facts, print the report or use the page's
 * spreadsheet download link. Internal labels in generated explanations are replaced
 * with readable field names; original source quotations are left alone.
 *
 * It also shows report-generation progress and permitted start, stop or retry
 * actions. Cards make long answers readable; an optional table supports comparison.
 * Filtering and switching views change only what is visible, not the saved report
 * or the information included in its spreadsheet export.
 */
'use strict';
const sid=location.pathname.split('/').pop(),el=id=>document.getElementById(id);
let timer,report=null,failedAttempt=null;
/** Create a literal-text node, not interpreted model/source markup.
 * Args: tag — DOM tag; text — visible text.
 * Returns: detached element.
 */
function node(tag,text){const n=document.createElement(tag);n.textContent=text;return n;}
/** Read state or submit an explicit report action.
 * Args: suffix — fixed API route; post — true for explicit actions; payload — action data.
 * Returns: decoded JSON or a readable HTTP/network error.
 */
async function api(suffix='',post=false,payload={}){const r=await fetch(`/api/setup/${sid}${suffix}`,post?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Request failed.');return data;}
/** Add fact links that expose the original evidence before scrolling to it.
 * Args: parent — destination; ids — validated short report fact IDs.
 * Returns: nothing; click behavior reads existing DOM only.
 */
function citations(parent,ids){for(const id of ids){const a=node('a',` [${id}]`);a.href='#source-'+id;a.addEventListener('click',()=>{el('source-details').open=true;});parent.append(a);}}
/** Replace internal requirement references in generated prose with readable names.
 * Args: text — generated narrative, not original source quotations.
 * Returns: literal display text using this report's field names; unknown IDs stay
 * unchanged rather than being guessed. Stored artifacts are never modified.
 */
function readableRequirements(text){const names=new Map((report?.requirements||[]).map(r=>[r.id,r.name]));return text.replace(/\bR\d+\b/g,id=>names.get(id)||id);}
/** Display readable cards by default, or a compact table when explicitly chosen.
 * Both views retain the same filtered entries and source links, so presentation
 * choices never hide missing qualifications or require report regeneration.
 * Args: none; reads the validated cached report, filters and view selector.
 * Returns: nothing; never changes qualification status or stored report data.
 */
function renderEntries(){
 if(!report)return;const filter=el('filter').value,query=el('find').value.toLowerCase();
 const rows=report.entries.filter(e=>{const complete=e.fields.every(f=>f.status==='supported');return(filter==='all'||(filter==='complete'?complete:!complete))&&`${e.label} ${e.fields.map(f=>f.value).join(' ')}`.toLowerCase().includes(query);});
 el('shown').textContent=`${rows.length} entries shown`;el('entries').replaceChildren();
 if(el('report-view').value==='table'){
  const columns=report.requirements.filter(r=>r.kind!=='entity_type');
  const table=node('table','');table.className='answer-items';
  const head=node('tr','');for(const label of ['Item','Evidence',...columns.map(r=>r.name)])head.append(node('th',label));table.append(head);
  for(const item of rows){
   const row=node('tr',''),name=node('td',item.label);citations(name,item.label_fact_ids);row.append(name);
   row.append(node('td',item.fields.every(f=>f.status==='supported')?'Complete':'Partial'));
   for(const requirement of columns){
    const field=item.fields.find(f=>f.requirement_id===requirement.id);
    const cell=node('td',field.value||'Not established');
    if(field.value&&field.status!=='supported')cell.append(node('small',' — '+field.status));
    citations(cell,field.fact_ids);row.append(cell);
   }
   table.append(row);
  }
  const wrap=node('div','');wrap.className='answer-table-scroll';wrap.append(table);el('entries').append(wrap);return;
 }

 for(const entry of rows){
  const card=node('article','');card.className='report-entry';
  const complete=entry.fields.every(field=>field.status==='supported');
  const status=node('span',complete?'Supported match':'Incomplete candidate');
  status.className='report-entry-status '+(complete?'supported':'incomplete');
  const heading=node('h2',entry.label);card.append(status,heading);
  const references=node('p','');references.className='report-entry-citations';
  citations(references,entry.label_fact_ids);card.append(references);
  for(const requirement of report.requirements){
   const field=entry.fields.find(item=>item.requirement_id===requirement.id);
   const row=node('div','');row.className='report-field';
   const content=node('div','');
   const value=node('p',readableRequirements(field.value)||'Not established');
   const qualification=node('span',field.status);qualification.className='report-field-status';
   content.append(value,qualification);citations(content,field.fact_ids);
   row.append(node('strong',requirement.name),content);card.append(row);
  }
  el('entries').append(card);
 }
}
/** Render only a server-validated report, including exclusions and source evidence.
 * Args: data — validated complete or explicitly partial report with original evidence mapping.
 * Returns: nothing; preserves readable report and CSV parity without model calls.
 */
function renderReport(data){
 report=data;el('report-content').hidden=false;
 el('summary').parentElement.hidden=data.presentation==='items';
 el('gaps').parentElement.hidden=data.presentation==='items'&&!data.gaps.length;el('csv').href=`/api/setup/${sid}/report.csv`;
 el('coverage').textContent=`Source collection: ${data.coverage.run}; selection: ${data.coverage.selection}; triage: ${data.coverage.triage}; investigation: ${data.coverage.investigation}. ${data.coverage.unreviewed_triage} triage claims and ${data.coverage.unreviewed_investigation} investigation claims remain unreviewed. Report coverage is limited to collected evidence.`;
 if(data.coverage.rounds)el('coverage').textContent=`${data.coverage.rounds.length} research rounds combined. `+data.coverage.rounds.map(r=>`Round ${r.round}: sources ${r.run}, selection ${r.selection}, triage ${r.triage}, investigation ${r.investigation}.`).join(' ')+` ${data.coverage.unreviewed_triage} triage claims and ${data.coverage.unreviewed_investigation} investigation claims remain unreviewed. Report coverage is limited to collected evidence.`;
 el('summary').replaceChildren();for(const part of data.summary){const p=node('p',readableRequirements(part.text));citations(p,part.fact_ids);el('summary').append(p);}
 if(!data.summary.length)el('summary').append(node('p','No supporting context was produced. See gaps and exclusions.'));
 el('gaps').replaceChildren();for(const gap of data.gaps)el('gaps').append(node('li',readableRequirements(gap)));
 el('excluded').replaceChildren();for(const x of data.exclusions){const p=node('p',readableRequirements(x.reason));citations(p,[x.fact_id]);el('excluded').append(p);}
 el('sources').replaceChildren();for(const f of data.facts){const card=node('article','');card.id='source-'+f.id;card.append(node('h3',`${f.id}: ${f.fact.statement}`));for(const e of f.evidence){const a=node('a',`${e.source_url}${e.pages.length?' · PDF page(s) '+e.pages.join(', '):''}`);a.href=e.source_url;a.target='_blank';a.rel='noopener noreferrer';card.append(a,node('blockquote',e.quote));}el('sources').append(card);}
 if(data.coverage.unreviewed_report||data.coverage.withheld_invalid_fields)el('coverage').textContent=`PARTIAL REPORT: ${data.coverage.unreviewed_report||0} input facts remain unreviewed; ${data.coverage.withheld_invalid_fields||0} invalid fields withheld. `+el('coverage').textContent;
 renderEntries();
}
/** Show measured batch completion and honest activity during open-ended merging.
 * Args: progress — saved report progress, or undefined before reporting begins.
 * Returns: nothing; updates a labelled native progress bar. Combining has no
 * fixed task total, so only running merges use an indeterminate bar. Failed or
 * stopped work retains its batch count without an ongoing activity animation.
 * Creates the region when absent so already-served page templates also work.
 */
function renderProgress(progress){
 if(!el('report-progress-region')){
  const region=node('section','');region.id='report-progress-region';
  const label=node('label','');label.id='report-progress-label';label.htmlFor='report-progress';
  const bar=node('progress','');bar.id='report-progress';
  region.append(label,bar);el('counts').before(region);
 }
 const region=el('report-progress-region'),bar=el('report-progress'),label=el('report-progress-label');
 region.hidden=!progress;if(!progress)return;
 const total=progress.batches_total||0,finished=progress.batches_finished||0;
 const completed=progress.status==='completed';
 const combining=progress.phase?.startsWith('Combining')||progress.phase==='Saving final report'||progress.phase==='Assembling answer items';
 let text;
 if(completed){bar.max=1;bar.value=1;text='Report complete';}
 else if(progress.status==='running'&&(combining||!total)){
  bar.removeAttribute('value');text=combining?`${progress.phase} · ${progress.merge_tasks_finished||0} combining tasks complete`:'Preparing evidence batches…';
 }else{
  bar.max=Math.max(1,total);bar.value=finished;
  text=`${finished}/${total} evidence batches complete`;
  if(progress.status!=='running')text+=` · Report ${progress.status}`;
 }
 label.textContent=text;bar.setAttribute('aria-label',text);
}
/** Poll generation and shared ledger without triggering model work on refresh.
 * Args: none.
 * Returns: promise; retries status reads only, retaining explicit errors.
 */
async function refresh(){clearTimeout(timer);try{const state=await api();if(followCurrentRound(state))return;renderStages(state);if(state.link_ranking&&!state.report&&!new URLSearchParams(location.search).has('stay')){location.replace(`/links/${sid}`);return;}const p=state.report;
 renderProgress(p);failedAttempt=p?.started_at;el('retry').hidden=!failedAttempt||!['failed','cancelled','interrupted','partial'].includes(p?.status);
 el('retry').textContent=p?.status==='partial'?'Retry unresolved report work':'Retry report';
 el('question').textContent=state.research_question;el('start').hidden=!!p||!['completed','partial','cancelled','interrupted'].includes(state.investigation?.status);el('stop').hidden=p?.status!=='running';el('stop').disabled=!!state.stop_requested;
 el('status').textContent=p?(p.error||(p.status==='running'?`${p.phase||'Preparing report'} · ${p.batches_finished||0}/${p.batches_total||0} evidence batches complete · ${p.merge_tasks_finished||0} merge tasks complete · ${p.reused_tasks||0} saved tasks reused`:`Report ${p.status}.`)):'Ready to prepare your report from Report Knowledge.';
 el('counts').replaceChildren();for(const [label,value] of [['Input facts',p?.facts_total??state.investigation?.report_knowledge??0],['Accounted for',p?.facts_accounted||0],['Supported matches',p?.complete_entries||0],['Incomplete candidates',p?.incomplete_entries||0]]){const d=node('div','');d.append(node('strong',value),node('span',label));el('counts').append(d);}
 if(['completed','partial'].includes(p?.status)&&!report){const data=await api('/report-data');if(data)renderReport(data);}
 if(p?.status==='running'||['investigation_running','link_ranking_running'].includes(state.status))timer=setTimeout(refresh,1200);
 }catch(e){showActivityIssue(true);el('status').textContent=e.message+' Retrying status…';timer=setTimeout(refresh,3000);}}
/** Start/stop generation only after an explicit user action.
 * Args: suffix — API action; button — clicked control.
 * Returns: promise; refreshes observed state and shows rejected actions.
 */
async function action(suffix,button){button.disabled=true;try{await api(suffix,true);await refresh();}catch(e){showActivityIssue();el('status').textContent=e.message;}finally{button.disabled=false;}}
el('back').href=`/investigation/${sid}?stay=1`;el('start').addEventListener('click',()=>action('/report',el('start')));el('stop').addEventListener('click',()=>action('/stop',el('stop')));el('print').addEventListener('click',()=>window.print());for(const id of ['filter','find','report-view'])el(id).addEventListener('input',renderEntries);refresh();

/** Retry a failed or partial report only after a deliberate click bound to that attempt.
 * Args: none.
 * Returns: promise; preserves the failed report and prevents duplicate replay.
 */
async function retryReport(){el('retry').disabled=true;try{await api('/report/retry',true,{previous_started_at:failedAttempt});report=null;el('report-content').hidden=true;await refresh();}catch(e){showActivityIssue();el('status').textContent=e.message;}finally{el('retry').disabled=false;}}
el('retry').addEventListener('click',retryReport);
