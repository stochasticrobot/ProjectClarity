/*
 * Show which relevant claims need a closer look before they can enter the report.
 *
 * This file fills the claim-review page with each claim, its review decision and the
 * reason for that decision. It distinguishes claims accepted for the report, claims
 * needing more investigation and claims that could not yet be checked. This helps
 * the user see missing information rather than just a growing count of facts.
 *
 * It follows saved progress and provides the page's permitted actions, including an
 * explicit resume button for stopped triage. Here, triage means deciding what needs
 * further review. Opening the page does not itself repeat the review.
 */
'use strict';
const sid=location.pathname.split('/').pop(),el=id=>document.getElementById(id);
let timer,revision='',records=[],resumeToken=null;
/** Create a literal-text element; source material is never interpreted as HTML.
 * Args: tag — element name; text — displayed text.
 * Returns: detached DOM element.
 */
function node(tag,text){const n=document.createElement(tag);n.textContent=text;return n;}
/** Read or submit an action to this saved session.
 * Args: suffix — fixed endpoint suffix; post — explicit action; payload — action data.
 * Returns: decoded JSON or an error; GET never starts model work.
 */
async function api(suffix='',post=false,payload={}){const r=await fetch(`/api/setup/${sid}${suffix}`,post?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Request failed.');return data;}
/** Render routing counts without counting pending/failed facts as approved.
 * Args: state — saved public progress snapshot.
 * Returns: nothing; changes only the visible view.
 */
function render(state){
  renderStages(state);
 resumeToken=state.triage?.artifact_directory;
 el('resume').hidden=!['triage_interrupted','triage_cancelled'].includes(state.status)||!!state.investigation||!!state.report;
 el('resume-note').hidden=el('resume').hidden;
 if(state.investigation&&!new URLSearchParams(location.search).has('stay')){location.replace(`/investigation/${sid}`);return;}
 el('investigation-next').hidden=!state.triage||state.triage.status==='running'||!el('resume').hidden;
 el('investigate').hidden=!!state.investigation||!['completed','partial','cancelled','interrupted'].includes(state.triage?.status);
 el('view-investigation').hidden=!state.investigation;el('view-investigation').href=`/investigation/${sid}`;
 const t=state.triage,total=t?.facts_total??state.selection?.facts_selected??0,done=t?.facts_reviewed||0;
 el('question').textContent=state.research_question;el('stop').hidden=t?.status!=='running';el('stop').disabled=!!state.stop_requested;
 el('start').hidden=!!t||!['completed','partial','cancelled','interrupted'].includes(state.selection?.status);
 el('status').textContent=t?(t.error||(state.stop_requested&&t.status==='running'?'Stopping after the current request finishes…':t.status==='running'?'Checking selected claims…':`Triage ${t.status}.`)):'Ready to check the selected facts.';
 const limited=state.run?.status!=='completed'||state.selection?.status!=='completed';el('upstream').hidden=!limited;
 el('upstream').textContent=`Source collection: ${state.run?.status}. Evidence selection: ${state.selection?.status}. Triage covers only saved selected facts; earlier omissions are not repaired here.`;
 el('counts').replaceChildren();for(const [label,value] of [['Selected facts',total],['Needs verification',t?.needs_verification||0],['Report Knowledge',t?.report_knowledge||0],['Unreviewed',total-done]]){const item=node('div','');item.append(node('strong',value),node('span',label));el('counts').append(item);}
 el('progress').max=Math.max(1,total);el('progress').value=done;el('progress').setAttribute('aria-label',`${done}/${total} selected facts checked`);
 el('progress-label').textContent=`${done}/${total} selected facts checked`;
 const active=t?.batches?.find(b=>b.status==='reviewing');el('current').textContent=active?`Checking source ${active.source.document_id}, chunk ${active.source.chunk_index+1}: ${active.facts} facts.`:t?`${t.batches_finished||0}/${t.batches_total||0} batches finished. Failed or stopped batches remain unreviewed.`:'';
 el('batches').replaceChildren();for(const b of t?.batches||[]){el('batches').append(node('li',`Source ${b.source.document_id}, chunk ${b.source.chunk_index+1}: ${b.status}${b.error?' — '+b.error:''}`));}
}
/** Display original facts, quotes and exact triage reasons in the chosen bucket.
 * Args: none; uses cached saved records and local filter values.
 * Returns: nothing; filtering never changes stored decisions.
 */
function renderRecords(){
 const bucket=el('bucket').value,query=el('find').value.toLowerCase();
 const visible=records.filter(r=>(bucket==='all'||r.triage_bucket===bucket)&&`${r.fact.statement} ${r.fact.quote} ${r.triage_reason}`.toLowerCase().includes(query));
 el('shown').textContent=`${visible.length} facts shown`;el('records').replaceChildren();
 if(!visible.length)el('records').append(node('p','No checked facts in this view yet.'));
 for(const r of visible){const card=node('article','');card.className='evidence-card';const badge=node('span',r.triage_bucket==='needs_verification'?'Needs verification':'Report Knowledge');badge.className='badge '+(r.triage_bucket==='needs_verification'?'unselected':'selected');card.append(badge,node('p',r.fact.statement));const reason=node('div','');reason.className='selection-reason';reason.append(node('strong',r.triage_bucket==='needs_verification'?'What needs checking':'Why it is ready'),node('p',r.triage_reason));card.append(reason,node('blockquote',r.fact.quote));const link=node('a',`Source · chunk ${r.source.chunk_index+1}${r.source.pages?.length?' · PDF page(s) '+ r.source.pages.join(', '):''}`);link.href=r.source.source_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);el('records').append(card);}
}
/** Poll observed state and load newly saved records without repeating calls.
 * Args: none.
 * Returns: promise; active work polls, read failures retry visibly.
 */
async function refresh(){clearTimeout(timer);try{const state=await api();if(followCurrentRound(state))return;render(state);const t=state.triage,key=JSON.stringify([t?.artifact_directory,t?.batches_finished,t?.status]);if(key!==revision){const data=await api('/triage-records');records=data.facts;renderRecords();revision=key;}if(t?.status==='running'||state.status==='selection_running')timer=setTimeout(refresh,1200);}catch(e){showActivityIssue(true);el('status').textContent=e.message+' Retrying status…';timer=setTimeout(refresh,3000);}}
/** Submit a start or cooperative stop and refresh the actual result.
 * Args: suffix — fixed action endpoint; button — button temporarily disabled.
 * Returns: promise; shows errors without assuming the action succeeded.
 */
async function action(suffix,button){button.disabled=true;try{await api(suffix,true);await refresh();}catch(e){showActivityIssue();el('status').textContent=e.message;}finally{button.disabled=false;}}
el('back').href=`/evidence/${sid}?stay=1`;
el('start').addEventListener('click',()=>action('/triage',el('start')));
el('stop').addEventListener('click',()=>action('/stop',el('stop')));
for(const id of ['bucket','find'])el(id).addEventListener('input',renderRecords);
refresh();

el('investigate').addEventListener('click',()=>action('/investigation',el('investigate')));

/** Resume only the stopped attempt shown on screen, never from polling.
 * Args: none.
 * Returns: promise; duplicate clicks reuse the server's idempotent action.
 */
async function resumeResearch(){el('resume').disabled=true;try{await api('/resume',true,{artifact_directory:resumeToken});await refresh();}catch(e){showActivityIssue();el('status').textContent=e.message;}finally{el('resume').disabled=false;}}
el('resume').addEventListener('click',resumeResearch);
