/*
 * Explain what happened when the application looked into a doubtful claim.
 *
 * For each claim needing further review, this file displays the original problem,
 * the source passages gathered to address it and the resulting decision. It updates
 * the investigation screen as claims are accepted, rejected or left unreviewed after
 * a failure, and shows which information is now available for the report.
 *
 * This lets the user inspect the evidence behind a decision instead of having to
 * trust a success counter. The investigation happens elsewhere in the application;
 * this file displays its saved work and handles the screen's user actions.
 */
'use strict';
const sid=location.pathname.split('/').pop(),el=id=>document.getElementById(id);
let timer,records=[],report=[],revision='',currentState;
/** Create safe literal text, never source-controlled markup.
 * Args: tag — DOM element; text — displayed string.
 * Returns: detached element.
 */
function node(tag,text){const n=document.createElement(tag);n.textContent=text;return n;}
/** Read saved state or submit an explicit user action.
 * Args: suffix — API suffix; post — true only for explicit actions; payload — action parameters.
 * Returns: decoded JSON or a readable HTTP/network error.
 */
async function api(suffix='',post=false,payload={}){const r=await fetch(`/api/setup/${sid}${suffix}`,post?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Request failed.');return data;}
/** Render observed counters without labeling failures as rejections.
 * Args: state — saved public session snapshot.
 * Returns: nothing; updates progress and current claim only.
 */
function render(state){
 currentState=state;
 renderStages(state);
 if(state.link_ranking&&!state.report&&!new URLSearchParams(location.search).has('stay')){location.replace(`/links/${sid}`);return;}
 if(state.report&&!new URLSearchParams(location.search).has('stay')){location.replace(`/report/${sid}`);return;}
 el('report-next').hidden=!state.investigation||state.investigation.status==='running';
 const retryable=['failed','cancelled','interrupted'].includes(state.report?.status)&&!!state.report?.started_at;
 const active=state.status.endsWith('_running')||['working','search_working'].includes(state.status);
 el('prepare-report').hidden=active||(!!state.report&&!retryable)||!['completed','partial','cancelled','interrupted'].includes(state.investigation?.status);
 el('prepare-report').textContent=['completed','partial'].includes(state.link_ranking?.status)?'Continue to report →':'Continue to link ranking →';
 el('continue-note').hidden=el('prepare-report').hidden;
 el('view-report').hidden=!state.report;el('view-report').href=`/report/${sid}`;
 const p=state.investigation,total=p?.claims_total??state.triage?.needs_verification??0,finished=p?.claims_finished||0;
 el('question').textContent=state.research_question;el('status').textContent=p?(p.error||(state.stop_requested&&p.status==='running'?'Stopping after the current request finishes…':`Investigation ${p.status}.`)):'Ready to investigate claims flagged during triage.';
 el('start').hidden=!!p||!['completed','partial','cancelled','interrupted'].includes(state.triage?.status);el('stop').hidden=p?.status!=='running';el('stop').disabled=!!state.stop_requested;
 el('upstream').textContent=`Source collection: ${state.run?.status}. Selection: ${state.selection?.status}. Triage: ${state.triage?.status}. ${p?.triage_unreviewed||0} claims unreviewed by triage are outside this investigation.`;
 el('counts').replaceChildren();for(const [label,value] of [['Flagged',total],['Accepted',p?.accepted||0],['Rejected',p?.rejected||0],['Failed',p?.failed||0],['Remaining',Math.max(0,total-finished)],['Report Knowledge',p?.report_knowledge??state.triage?.report_knowledge??0]]){const d=node('div','');d.append(node('strong',value),node('span',label));el('counts').append(d);}
 el('resume-investigation-controls').hidden=!!state.link_ranking||!!state.report||!p?.artifact_directory||!['cancelled','interrupted','failed','partial'].includes(p?.status);
 el('current').textContent=p?.active_claims?.length?p.active_claims.map(c=>`${c.status==='collecting'?'Collecting evidence':'Reviewing'}: ${c.statement}`).join('\n'):(p?.current_claim?`Reviewing: ${p.current_claim}`:'');el('progress').max=Math.max(1,total);el('progress').value=finished;el('progress-label').textContent=`${finished}/${total} claims processed (includes failed attempts)`;
 el('progress').setAttribute('aria-label',el('progress-label').textContent);
}
/** Render exact original claims, review conditions, evidence and outcomes.
 * Args: none; uses cached saved records and local UI filters.
 * Returns: nothing; does not alter records or initiate model calls.
 */
function renderRecords(){
 const filter=el('filter').value,query=el('find').value.toLowerCase();
 const rows=filter==='report'?report.map(r=>({record:r,status:'report',assessment:r.investigation})):records;
 const visible=rows.filter(r=>(['all','report'].includes(filter)||r.status===filter||(filter==='reviewing'&&r.status==='collecting'))&&`${r.record.fact.statement} ${r.record.triage_reason} ${r.assessment?.reason||''}`.toLowerCase().includes(query));
 el('shown').textContent=`${visible.length} claims shown`;el('records').replaceChildren();
 if(!visible.length)el('records').append(node('p','No claims in this view.'));
 for(const r of visible){
  const card=node('article','');card.className='evidence-card';const badge=node('span',r.status==='report'?'Report Knowledge':r.status);badge.className='badge '+(['accepted','report'].includes(r.status)?'selected':'unselected');
  card.append(badge,node('h2',r.record.fact.statement),node('p','Review issue: '+r.record.triage_reason),node('blockquote',r.record.fact.quote));
  if(r.assessment)card.append(node('strong',r.assessment.decision==='accept'?'Accepted':'Rejected'),node('p',r.assessment.reason));if(r.error)card.append(node('p',r.error));
  const packet=r.packet;if(packet){const info=packet.retrieval;card.append(node('p',`${info.passages_included} passages included from ${info.passages_searched} searched across ${info.documents_searched?.length||0} documents · ${info.characters_included}/${info.character_budget} evidence characters · ${info.omitted_passage_ids.length} omitted by budget · ${info.source_errors.length} source issues.`));
   const details=node('details','');details.append(node('summary','Evidence considered'));
   for(const p of packet.passages){const a=node('a',`${p.passage_id} · ${p.source_url}${p.pages?.length?' · PDF page(s) '+p.pages.join(', '):''}`);a.href=p.source_url;a.target='_blank';a.rel='noopener noreferrer';details.append(a,node('p',p.retrieval_reason),node('pre',p.text));}
   if(info.omitted_passage_ids.length)details.append(node('p','Omitted passages: '+info.omitted_passage_ids.join(', ')));
   for(const issue of info.source_errors)details.append(node('p','Source issue: '+issue.reason));card.append(details);
  }
  const citations=r.supporting_evidence||r.record.supporting_evidence||[];
  for(const c of citations){const link=node('a',`Supporting passage ${c.passage_id}`);link.href=c.source_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link,node('blockquote',c.quote));}
  el('records').append(card);
 }
}
/** Poll only saved results; refresh cannot replay any investigation.
 * Args: none.
 * Returns: promise; retries status reads on failure, never automatic POSTs.
 */
async function refresh(){clearTimeout(timer);try{const state=await api();if(followCurrentRound(state))return;render(state);const p=state.investigation,key=JSON.stringify([p?.status,p?.claims_finished,p?.current_claim,p?.model_calls,p?.events?.length,p?.active_claims]);if(key!==revision){const data=await api('/investigation-records');records=data.records;report=data.report_facts;renderRecords();revision=key;}if(p?.status==='running'||state.status==='triage_running')timer=setTimeout(refresh,1200);}catch(e){showActivityIssue(true);el('status').textContent=e.message+' Retrying status…';timer=setTimeout(refresh,3000);}}
/** Start or stop only after an explicit click.
 * Args: suffix — fixed API endpoint; button — clicked button.
 * Returns: promise; shows errors and restores the action button on rejection.
 */
async function action(suffix,button){button.disabled=true;try{await api(suffix,true);await refresh();}catch(e){showActivityIssue();el('status').textContent=e.message;}finally{button.disabled=false;}}
el('back').href=`/triage/${sid}?stay=1`;
el('start').addEventListener('click',()=>action('/investigation',el('start')));el('stop').addEventListener('click',()=>action('/stop',el('stop')));
for(const id of ['filter','find'])el(id).addEventListener('input',renderRecords);
const resumeControls=node('section','');resumeControls.id='resume-investigation-controls';resumeControls.hidden=true;
const resumeLabel=node('label','Concurrent investigation calls');resumeLabel.htmlFor='resume-investigation-limit';
const resumeLimit=node('input','');resumeLimit.id='resume-investigation-limit';resumeLimit.type='number';resumeLimit.min='1';resumeLimit.max='16';resumeLimit.step='1';resumeLimit.value='6';
const resumeButton=node('button','Resume claim investigation →');resumeButton.id='resume-investigation';
resumeControls.append(resumeLabel,resumeLimit,resumeButton,node('p','Reuses completed reviews; retries failed or unfinished claims. Earlier research stages are not repeated.'));
el('stop').after(resumeControls);
/** Resume a stopped claim-review attempt after an explicit click.
 * Args: none; reads displayed attempt token and bounded worker limit.
 * Returns: promise; refreshes saved progress or shows a rejection without replay.
 */
async function resumeInvestigation(){
 if(!resumeLimit.reportValidity())return;
 resumeButton.disabled=true;
 try{await api('/investigation/resume',true,{previous_artifact:currentState.investigation.artifact_directory,concurrency:Number(resumeLimit.value)});await refresh();}
 catch(error){showActivityIssue();el('status').textContent=error.message;}
 finally{resumeButton.disabled=false;}
}
resumeButton.addEventListener('click',resumeInvestigation);
refresh();

/** Continue downstream from saved investigation after an explicit click.
 * Args: none; uses the last displayed report attempt timestamp to prevent stale retries.
 * Returns: promise; opens link ranking (or an already ranked report). Failures
 * remain on this screen. Does not rerun investigation, search or extraction.
 */
async function continueFromInvestigation(){
 const button=el('prepare-report');button.disabled=true;
 try{
  const prior=currentState?.report;
  const state=await api(prior?'/report/retry':'/report',true,prior?{previous_started_at:prior.started_at}:{});
  location.assign(state.status==='link_ranking_running'||(!state.report&&state.link_ranking)?`/links/${sid}`:`/report/${sid}`);
 }catch(e){showActivityIssue();el('status').textContent=e.message;}
 finally{button.disabled=false;}
}
el('prepare-report').addEventListener('click',continueFromInvestigation);
