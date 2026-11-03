/*
 * Show research activity, its place in the sequence and links to earlier steps.
 *
 * The shared activity pill describes ongoing work, warnings and failures.
 * Every screen uses the same row of stage names so the user can see what has happened,
 * what is happening now and what comes next. This file highlights the actual current
 * step, omits clarification when none was needed and lets the user review steps that
 * have already been reached.
 *
 * Looking back at an earlier screen must not make the research appear to have moved
 * backwards. The highlight therefore follows the application's saved progress, not
 * just the page being viewed. The separate model_usage.js file supplies the Usage panel.
 */
'use strict';
/** Identify the live workflow stage from saved state, not the viewed URL.
 * Args: state — durable public session snapshot.
 * Returns: stage key; never starts work or changes persisted state.
 */
function currentStage(state){
  if(state.report)return 'report';
  if(state.link_ranking)return 'link_ranking';
  if(state.investigation)return 'investigation';
  if(state.triage)return 'triage';
  if(state.selection)return 'selection';
  if(state.run)return ({searching:'searches',downloading:'documents',extracting:'extraction',finished:'extraction'})[state.run.stage]||'searches';
  if(state.searches?.length||state.status==='search_working')return 'searches';
  return state.status==='clarification'?'clarification':'question';
}
/** Render accessible workflow navigation with read-only links to reached stages.
 * Args: state — saved state or empty initial state; viewed — optional review stage.
 * Returns: nothing; highlights live stage, labels viewed stage, disables future
 * stages and omits clarification when no clarification was requested.
 */
function renderStages(state={},viewed=null){
 followCurrentRound(state,viewed);
 renderActivity(state,viewed);
 renderUsage(state);
 const nav=document.getElementById('stages');if(!nav)return;
 const stages=[['question','Question'],...(state.questions?.length||state.answers?.length?[['clarification','Clarification']]:[]),['searches','Searches'],['documents','Documents'],['extraction','Extraction'],['selection','Selection'],['triage','Triage'],['investigation','Investigate claims'],['link_ranking','Rank links'],['report','Report']];
 const active=currentStage(state),position=stages.findIndex(s=>s[0]===active);
 nav.replaceChildren();
 stages.forEach(([key,label],i)=>{
  if(i){const arrow=document.createElement('span');arrow.textContent='→';arrow.setAttribute('aria-hidden','true');nav.append(arrow);}
  const reached=i<=position&&!!state.session_id&&(key!=='link_ranking'||!!state.link_ranking);
  const item=document.createElement(reached?'a':'span');item.textContent=label;
  if(reached)item.href=key==='link_ranking'?`/links/${state.session_id}?stay=1`:`/stage/${state.session_id}?stage=${key}`;else item.setAttribute('aria-disabled','true');
  if(key===active){item.classList.add('active');item.setAttribute('aria-current','step');item.title='Current workflow stage';}
  if(key===viewed)item.classList.add('viewed');nav.append(item);
 });
}


/*
 * Give every research screen the same clear sign of activity or trouble.
 *
 * A green pill gently pulses while the displayed step is working. Recoverable
 * interruptions turn it yellow; a failed step turns it red. Finished, stopped and
 * waiting steps never pulse. Text explains the state so colour is not the only
 * signal. This file reads saved progress; it never starts or retries research.
 */

/** Describe the displayed step without confusing another stage's work with it.
 * Args: state — saved session; page — URL path; viewed — optional saved-review stage.
 * Returns: label, tone and busy flag. Rejected claims are research outcomes, not
 * application failures. Per-record errors and partial evidence stay in detail
 * panels and never change this progress signal. A saved view is never live.
 */
function activityPresentation(state={},page=location.pathname,viewed=null){
 const names={link_ranking:'Link ranking',run:'Research',selection:'Evidence selection',triage:'Claim review',investigation:'Claim investigation',report:'Report'};
 const working={link_ranking:'Ranking links',selection:'Selecting relevant information',triage:'Checking claims',investigation:'Investigating claims',report:'Preparing report'};
 const segment=page.split('/')[1];
 const key=({links:'link_ranking',run:'run',evidence:'selection',triage:'triage',investigation:'investigation',report:'report'})[segment];
 if(viewed){
  const stored=({link_ranking:state.link_ranking,searches:state.run,documents:state.run,extraction:state.run,selection:state.selection,triage:state.triage,investigation:state.investigation,report:state.report})[viewed];
  const problem=stored?.status==='failed'?'error':['interrupted','cancelled'].includes(stored?.status)?'warning':'idle';
  return {label:'Saved '+viewed.replaceAll('_',' ')+(problem==='error'?' — failed':problem==='warning'?' — incomplete':''),tone:problem,busy:false};
 }
 if(!key){
  const status=state.status;
  if(['failed','search_failed','launch_failed'].includes(status))return {label:'Research setup failed',tone:'error',busy:false};
  if(status==='working'||status==='search_working')return {label:status==='working'?'Defining your research':'Preparing searches',tone:'working',busy:true};
  if(status==='clarification')return {label:'Waiting for your clarification',tone:'idle',busy:false};
  if(status==='queries_ready'||status==='ready')return {label:status==='ready'?'Definition ready':'Searches ready',tone:'success',busy:false};
  return {label:'Start with a question',tone:'idle',busy:false};
 }
 const progress=state[key],name=names[key];
 if(!progress)return {label:name+' — ready',tone:'idle',busy:false};
 const status=progress.status;
 if(status==='failed')return {label:name+' failed',tone:'error',busy:false};
 if(['cancelled','interrupted'].includes(status))return {label:name+(status==='cancelled'?' stopped':' interrupted'),tone:'warning',busy:false};
 if(status==='running'&&state.stop_requested)return {label:'Stopping '+name.toLowerCase()+'…',tone:'warning',busy:true};
 const busy=status==='running';
 const label=busy?(key==='run'?({searching:'Searching',downloading:'Downloading',extracting:'Extracting'})[progress.stage]||'Processing documents':working[key]):name+(status==='completed'?' complete':status==='partial'?' finished':' — '+(status||'ready'));
 return {label,tone:busy?'working':['completed','partial'].includes(status)?'success':'idle',busy};
}

/** Update the shared pill without announcing identical polling results repeatedly.
 * Args: presentation — readable label, semantic tone and whether work is active.
 * Returns: nothing; updates plain text, colour and animation. Missing pills are
 * ignored so a failed screen cannot cause a second rendering exception.
 */
function showActivity(presentation){
 const pill=document.getElementById('activity-status');if(!pill)return;
 if(pill.textContent!==presentation.label)pill.textContent=presentation.label;
 pill.dataset.tone=presentation.tone;
 pill.classList.toggle('is-active',presentation.busy);
}

/** Refresh the activity indicator from the same snapshot as stage navigation.
 * Args: state — saved session; viewed — optional read-only stage being inspected.
 * Returns: nothing; does not fetch, save or advance the research.
 */
function renderActivity(state={},viewed=null){
 const presentation=activityPresentation(state,location.pathname,viewed);
 if(state.run&&state.research_rounds>1)presentation.label=`Round ${state.current_round||1} of ${state.research_rounds} · ${presentation.label}`;
 showActivity(presentation);
}

/** Stop showing healthy activity when an action or progress request fails.
 * Args: retrying — true for a failed status fetch that the page will retry.
 * Returns: nothing; yellow means the live state is unknown, not that work stopped.
 * A rejected action is red; existing detailed error text remains on the page.
 */
function showActivityIssue(retrying=false){
 showActivity({label:retrying?'Status unavailable — reconnecting':'Unable to complete request',tone:retrying?'warning':'error',busy:false});
}

/** Follow the current stage after a round boundary without replaying any work.
 * Args: state — latest saved snapshot; viewed — explicit saved-stage view.
 * Returns: true when navigation was requested, otherwise false/undefined.
 * Only live workflow pages navigate. Saved views and ?stay
 * remain read-only. Missing old stage buckets must not strand an open page.
 */
function followCurrentRound(state,viewed=null){
 if((state.current_round||1)<2||viewed||new URLSearchParams(location.search).has('stay'))return;
 const page=location.pathname.split('/')[1];
 if(!['run','evidence','triage','investigation','links','report'].includes(page))return;
 const target=state.report?'report':state.link_ranking?'links':state.investigation?'investigation':state.triage?'triage':state.selection?'evidence':'run';
 if(page!==target){location.replace(`/${target}/${state.session_id}`);return true;}
 return false;
}
