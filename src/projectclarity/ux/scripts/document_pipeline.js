/*
 * Show what is happening while sources are found, downloaded and read.
 *
 * After the question is defined, the user needs to know whether research is making
 * progress and which documents are usable. This file fills the document-processing
 * page with search results, download outcomes, extraction counts and recent activity.
 * It lets the user inspect saved text and facts from a particular source.
 *
 * It regularly asks the application for updated progress. Counts describe the work
 * actually recorded, including failures, rather than guessing how much useful
 * information has been found. The page observes and controls the work; it does not
 * perform the searches or extract the facts itself.
 */
'use strict';
const sid = location.pathname.split('/').pop();
const element = id => document.getElementById(id);
let timer, lastStage;
/** Create a literal-text element; untrusted source content never becomes markup.
 * Args: tag — DOM tag; text — plain content.
 * Returns: detached element.
 */
function textNode(tag, text) { const e=document.createElement(tag); e.textContent=text; return e; }
/** Read or update the current run without permitting caller-supplied research context.
 * Args: suffix — fixed API suffix; post — whether to POST.
 * Returns: decoded public state; throws a readable error on failure.
 */
async function api(suffix='', post=false) {
  const response=await fetch(`/api/setup/${sid}${suffix}`,post?{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}:{});
  const data=await response.json(); if(!response.ok) throw new Error(data.detail || 'Request failed.'); return data;
}
/** Render observed progress, preserving the source inspector while polling.
 * Args: state — saved public snapshot.
 * Returns: nothing; updates counters/table/events without initiating new work.
 */
function render(state) {
  renderStages(state);
  clearTimeout(timer);
  const run=state.run;
  if(!run) { element('run-message').textContent='This question has no started research run.';element('stop').hidden=true;return; }
  if(state.selection && !new URLSearchParams(location.search).has('stay')){location.replace(`/evidence/${sid}`);return;}
  element('selection-next').hidden=run.status==='running';
  element('select-evidence').hidden=!!state.selection||!['completed','partial','cancelled','interrupted'].includes(run.status);
  element('view-evidence').hidden=!state.selection;element('view-evidence').href=`/evidence/${sid}`;
  element('run-question').textContent=state.research_question;
  element('stop').hidden=run.status!=='running';element('stop').disabled=state.stop_requested;
  element('run-message').textContent=state.stop_requested && run.status==='running' ? 'Stopping after the current request finishes…' : run.error || (run.status==='running'?'Research is running. This page updates automatically.':`Run ${run.status}.`);
  element('prior-attempts').hidden=run.stage!=='searching';
  element('prior-attempts').textContent=`Search provider: ${run.provider || 'duckduckgo'}.`;
  const sources=run.sources;
  const counts=[['URLs found',sources.length],['Downloaded',sources.filter(s=>s.downloaded).length],['Text documents',sources.filter(s=>s.parsed).length],['Chunks finished',`${run.chunks_finished}/${run.chunks_total}`],['Facts extracted',run.facts]];
  element('counts').replaceChildren();for(const [label,value] of counts){const item=textNode('div','');item.append(textNode('strong',value),textNode('span',label));element('counts').append(item);}
  renderWork(run);
  element('queries').replaceChildren();for(const q of run.searches){const li=textNode('li','');li.append(textNode('strong',q.query),textNode('p',`${q.provider}: ${q.status} · ${q.results} results${q.primary_failure?' · Primary failed: '+q.primary_failure.error+' · Wikipedia fallback':''}${q.error?' — '+q.error:''}`));element('queries').append(li);}
  element('sources').replaceChildren();
  for(const source of sources){
    const row=textNode('tr','');const label=textNode('td','');const link=textNode('a',source.title||source.url);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';label.append(link,textNode('small',source.url));
    const status=textNode('td',source.status.replaceAll('_',' '));if(source.parser)status.append(textNode('small',`${source.parser}${source.pages?.length?' · '+source.pages.length+' PDF pages':''}`));for(const warning of source.warnings||[])status.append(textNode('small',warning));if(source.error)status.append(textNode('small',source.error));
    row.append(label,status,textNode('td',`${source.chunks.filter(c=>['checks_pass','checks_partial','failed','split'].includes(c.status)).length}/${source.chunks.length}`),textNode('td',source.facts));
    const action=textNode('td','');const button=textNode('button','Inspect');button.className='secondary';button.addEventListener('click',()=>inspectSource(source.id));action.append(button);row.append(action);element('sources').append(row);
  }
  element('events').replaceChildren();for(const event of [...run.events].reverse()){const li=textNode('li','');li.append(textNode('time',new Date(event.time).toLocaleTimeString()),textNode('span',event.message));element('events').append(li);}
  if(run.status==='running'||state.status==='run_running')timer=setTimeout(refresh,1200);
}
/** Show progress in explicit stage units and identify the document being consumed.
 * Args: run — saved acquisition/extraction state, including per-source chunks.
 * Returns: nothing; updates observed progress, current sizing and recovery counts.
 * Split parents count as processed attempts, not unresolved failures; dynamic
 * pending-work totals are labelled explicitly and are never estimated percentages.
 */
function renderWork(run){
  const stage=run.stage,sources=run.sources,bar=element('extraction-progress');
  if(stage!==lastStage){element('search-summary').open=stage==='searching';lastStage=stage;}
  element('current-document').hidden=true;
  let done=0,total=0,label='',description='',current='';
  if(stage==='searching'){
    done=run.searches.filter(q=>['completed','failed'].includes(q.status)).length;total=2;
    element('work-title').textContent='Searching for sources';label=`${done}/${total} searches finished`;
    const active=run.searches.find(q=>['searching','waiting'].includes(q.status));
    current=active?`${active.status==='waiting'?'Cooling down before search':'Searching'}: ${active.query}`:'Preparing searches…';
    description='Finished includes successful searches and explicit failures. Search results are leads, not downloaded evidence.';
  }else if(stage==='downloading'){
    done=sources.filter(s=>!['queued','checking_robots','parsing'].includes(s.status)).length;total=sources.length;
    element('work-title').textContent='Downloading documents';label=`${done}/${total} URLs processed`;
    const source=sources.find(s=>['checking_robots','parsing'].includes(s.status));current=source?`${source.status==='checking_robots'?'Checking access':'Parsing'}: ${source.title||source.url}`:'Preparing the document pool…';
    description='Processed URLs include downloaded, blocked, and failed sources. Only successfully parsed documents go to extraction.';
  }else{
    done=run.chunks_finished;total=run.chunks_total;
    element('work-title').textContent=stage==='finished'?'Knowledge extraction summary':'Extracting knowledge';
    label=`${done}/${total} chunks processed across all documents`;
    const inflight=sources.flatMap(s=>s.chunks).filter(c=>c.status==='repairing').reduce((n,c)=>n+(c.accepted||0),0);
    description=`${run.facts+inflight} facts accepted · ${run.facts_awaiting_repair||0} awaiting repair · ${run.facts_unresolved||0} unresolved · ${run.facts_withdrawn||0} withdrawn. Chunks: ${run.chunks_passed} complete · ${run.chunks_partial||0} partial · ${done-run.chunks_passed-(run.chunks_partial||0)-(run.chunks_split||0)} failed · ${run.chunks_split||0} split for recovery · ${Math.max(0,total-done)} remaining. ${run.recovery_calls||0} recovery calls (including child repairs). Pending chunk totals can change as sizes adapt.`;
    const source=sources.find(s=>s.status==='extracting');
    current=source?`Reading: ${source.title||source.url}${source.extraction_sizing?` · Chunk length ${source.extraction_sizing.chunk_characters.toLocaleString()} characters`:""}`:`${run.facts} facts extracted from ${sources.filter(s=>s.parsed).length} parsed documents.`;
    if(source){const chunks=source.chunks,finished=chunks.filter(c=>['checks_pass','checks_partial','failed','split'].includes(c.status)).length,active=chunks.find(c=>['extracting','repairing'].includes(c.status));
      element('current-document').hidden=false;element('document-link').textContent=source.url;element('document-link').href=source.url;
      element('document-progress-label').textContent=`This document: ${finished}/${chunks.length} chunks processed${active?` · reading chunk ${active.index+1}`:''}`;
      element('document-progress').max=Math.max(1,chunks.length);element('document-progress').value=finished;
      element('document-facts').textContent=`${source.facts} facts extracted so far from this document. Use Inspect below to read saved facts and supporting quotes.`;
    }
  }
  bar.max=Math.max(1,total);bar.value=done;bar.setAttribute('aria-label',label);
  element('overall-progress-label').textContent=label;element('current-work').textContent=current;element('progress-description').textContent=description;
  const successes=run.searches.filter(q=>q.status==='completed').length,failures=run.searches.filter(q=>q.status==='failed').length;
  element('search-summary-label').textContent=`Search results · ${successes} successful${failures?`, ${failures} failed`:''}`;
}
/** Refresh progress after loading or during active work; never replay requests.
 * Args: none.
 * Returns: promise; transient network failures remain visible and retry status only.
 */
async function refresh(){clearTimeout(timer);try{render(await api());}catch(error){showActivityIssue(true);element('run-message').textContent=error.message+' Retrying status…';timer=setTimeout(refresh,3000);}}
/** Show saved source text, accepted assertions and unresolved/withdrawn proposals.
 * Args: id — source identifier supplied by the run snapshot.
 * Returns: promise; fills the read-only inspector with no model calls, including
 * failed source spans that remain unresolved after minimum-size recovery.
 */
async function inspectSource(id){
  try{const data=await api(`/knowledge/${id}`);element('inspector').hidden=false;element('inspect-title').textContent=data.source.title||data.source.url;element('inspect-status').textContent=[data.source.error||data.source.status,...(data.source.warnings||[])].join(' · '); element('document-text').textContent=data.document_text||'No parsed document text available.';element('facts').replaceChildren();
    for(const chunk of data.knowledge)for(const fact of chunk.facts){const card=textNode('article','');card.append(textNode('p',fact.statement),textNode('blockquote',fact.quote),textNode('small',`Chunk ${chunk.chunk_index+1}${chunk.pages?.length?' · PDF page(s) '+chunk.pages.join(', '):''} · ${fact.predicate}`));element('facts').append(card);}
    for(const chunk of data.knowledge){
      for(const issue of chunk.unresolved||[]){const card=textNode('article','');card.append(textNode('strong',`Unresolved · chunk ${chunk.chunk_index+1}`),textNode('p',issue.fact?.statement||JSON.stringify(issue.fact)),textNode('small',issue.reason),textNode('p',issue.repair_error?JSON.stringify(issue.repair_error):'Not accepted into knowledge.'));element('facts').append(card);}
      for(const item of chunk.withdrawn||[]){const card=textNode('article','');card.append(textNode('strong',`Withdrawn · chunk ${chunk.chunk_index+1} · ${item.id}`),textNode('p',item.reason));element('facts').append(card);}
    }
    for(const chunk of data.source.chunks||[]){
      if(chunk.status==='failed'){const card=textNode('article','');card.append(textNode('strong',`Unresolved source portion · chunk ${chunk.index+1}`),textNode('p',chunk.error||'Extraction failed.'),textNode('small',`Source characters ${chunk.start}–${chunk.end}; no accepted facts from this portion.`));element('facts').append(card);}
    }
    if(!data.knowledge.length)element('facts').append(textNode('p','No accepted knowledge records yet.'));
    element('inspector').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){element('run-message').textContent=error.message;}
}
/** Request a cooperative stop while preserving existing receipts and knowledge.
 * Args: none.
 * Returns: promise; refreshes the observed state without assuming immediate stop.
 */
async function stopRun(){element('stop').disabled=true;try{render(await api('/stop',true));}catch(error){element('run-message').textContent=error.message;}}
element('stop').addEventListener('click',stopRun);
/** Start selection from saved knowledge; navigation never repeats upstream work.
 * Args: none.
 * Returns: promise; opens the separate screen or shows an explicit start error.
 */
async function startSelection(){element('select-evidence').disabled=true;try{await api('/selection',true);location.href=`/evidence/${sid}`;}catch(error){showActivityIssue();element('run-message').textContent=error.message;element('select-evidence').disabled=false;}}
element('select-evidence').addEventListener('click',startSelection);
element('close-inspector').addEventListener('click',()=>{element('inspector').hidden=true;});
element('new-research').addEventListener('click',()=>localStorage.removeItem('clarity.setup'));
refresh();
const requestedDocument=Number(new URLSearchParams(location.search).get('document'));if(requestedDocument)inspectSource(requestedDocument);
