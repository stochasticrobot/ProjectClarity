/*
 * Let the user look back at an earlier research step without running it again.
 *
 * The stage navigation links open this page when someone wants to review the
 * question, prepared searches or other saved progress. This file reads the requested
 * step's saved information, displays it and provides a way back to the current work.
 *
 * This view is for inspection, not editing. Keeping it read-only means the user can
 * understand how a result was reached without changing the question or accidentally
 * repeating searches and AI requests.
 */
'use strict';
/** Read a historical stage without redirecting, editing or replaying any work.
 * Args: none; uses validated server session and the requested stage query.
 * Returns: promise; renders saved details or an explicit error. No POST requests.
 */
async function reviewStage(){
 const sid=location.pathname.split('/').pop(),view=new URLSearchParams(location.search).get('stage')||'question';
 const container=document.getElementById('content');
 /** Append literal saved text; untrusted source markup is not executed.
  * Args: title — record heading; text — saved details.
  * Returns: nothing; appends one read-only article.
  */
 function entry(title,text){const a=document.createElement('article'),h=document.createElement('h2'),p=document.createElement('p');h.textContent=title;p.textContent=text;a.append(h,p);container.append(a);}
 try{
  const response=await fetch(`/api/setup/${sid}`);if(!response.ok)throw new Error('Saved research session unavailable.');const state=await response.json();
  renderStages(state,view);document.getElementById('title').textContent=view==='investigation'?'Investigate claims':view[0].toUpperCase()+view.slice(1);document.getElementById('question').textContent=state.research_question||state.original_question;
  document.getElementById('status').textContent='Read-only saved results. Viewing this page does not restart work.';
  document.getElementById('live').href=state.report?`/report/${sid}`:state.link_ranking?`/links/${sid}`:state.investigation?`/investigation/${sid}`:state.triage?`/triage/${sid}`:state.selection?`/evidence/${sid}`:state.run?`/run/${sid}`:'/';
  if(!state.run)document.getElementById('live').href='/';
  if(view==='question'){entry('Original question',state.original_question);for(const r of state.information_needed||[])entry(r.name,r.description);}
  else if(view==='clarification'){for(const pair of state.answers||[])entry(pair.question,pair.answer);if(!state.answers?.length)for(const q of state.questions||[])entry(q,'Awaiting your answer in the current stage.');}
  else if(view==='searches'){for(const q of state.run?.searches||state.searches||[])entry(q.query,`${q.purpose||''} ${q.provider||''} ${q.status||'Prepared'}${q.results!==undefined?' · '+q.results+' results':''}${q.primary_failure?' · Primary search failed: '+q.primary_failure.error+' · Wikipedia fallback':''}${q.error?' · '+q.error:''}`);}
  else if(view==='documents'||view==='extraction'){
   for(const s of state.run?.sources||[]){entry(s.title||s.url,`${s.url}\n${s.status} · ${s.parser||'Not parsed'}${s.pages?.length?' · '+s.pages.length+' PDF pages':''}\n${(s.warnings||[]).join(' ')} ${s.error||''}${view==='extraction'?' · '+s.facts+' facts · '+s.chunks.filter(c=>['checks_pass','checks_partial','failed'].includes(c.status)).length+'/'+s.chunks.length+' chunks processed':''}`);
    const a=document.createElement('a');a.textContent='Inspect source and extracted knowledge';a.href=`/run/${sid}?stay=1&document=${s.id}`;container.append(a);
   }
  }else if(view==='selection'||view==='triage'||view==='investigation'||view==='report'){
   const a=document.createElement('a');a.textContent='Open saved '+view+' details';a.href=`/${view==='selection'?'evidence':view}/${sid}?stay=1`;container.append(a);
  }
 }catch(error){showActivityIssue();document.getElementById('status').textContent=error.message;}
}
reviewStage();
