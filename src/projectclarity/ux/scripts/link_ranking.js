/* Display ranking scores and provenance without fetching destination pages.
 * Polling is read-only. Only explicit buttons stop work or request a new attempt.
 */
'use strict';
const sid=location.pathname.split('/').pop(),el=id=>document.getElementById(id);
let timer,candidateRows=[],visibleCandidates=50;
/** Create literal text to keep untrusted source titles out of HTML markup.
 * Args: tag — element name; text — display text.
 * Returns: detached DOM element.
 */
function node(tag,text){const result=document.createElement(tag);result.textContent=text;return result;}
/** Read saved state or send an explicitly clicked action.
 * Args: suffix — fixed API suffix; post — whether the user requested an action.
 * Returns: decoded JSON or a readable request error; never follows source links.
 */
async function api(suffix='',post=false){const response=await fetch(`/api/setup/${sid}${suffix}`,post?{method:'POST'}:{});const data=await response.json();if(!response.ok)throw new Error(data.detail||'Request failed');return data;}
/** Show source attribution and both scores for each candidate.
 * Args: id — target container; rows — saved candidate or selected records.
 * Returns: nothing; replaces the container with text-safe cards, source associations
 * and HTTP(S) links. Excluded/unassessed records never appear to have title ratings.
 */
function cards(id,rows){
 el(id).replaceChildren();
 if(!rows.length)el(id).append(node('p','No links to show.'));
 for(const row of rows){
  const card=node('article','');card.className='evidence-card';
  const link=node('a',row.link_title||row.url);
  if(/^https?:\/\//i.test(row.url)){link.href=row.url;link.target='_blank';link.rel='noopener noreferrer';}
  card.append(link,node('p',`Source: ${row.source_title} — ${row.source_url}`));
  const ranked=row.status==='ranked';
  card.append(node('p',`Source ${row.document_score.toFixed(2)} + title ${ranked?row.title_score:'unranked'}${ranked?' = '+(row.document_score+row.title_score).toFixed(2):''} · ${row.rating||row.status.replaceAll('_',' ')}`),node('p',row.reason||'Title not ranked.'));
  if(row.source_associations?.length>1){const details=node('details','');details.append(node('summary',`Found in ${row.source_associations.length} source/title occurrences`));for(const source of row.source_associations)details.append(node('p',`${source.source_title} — ${source.source_url} · ${source.link_title||'Untitled'} · source score ${source.document_score.toFixed(2)}`));card.append(details);}
  el(id).append(card);
 }
}
/** Render a bounded slice so thousands of unassessed links do not flood the page.
 * Args: none; reads cached records, the filter and the requested display limit.
 * Returns: nothing; shows at most the requested number (initially 50), with an
 * explicit count and a show-more control. No ranking or network work is started.
 */
function renderCandidates(){
 const filter=el('candidate-filter').value;
 const rows=candidateRows.filter(row=>filter==='all'||row.status===filter);
 cards('candidates',rows.slice(0,visibleCandidates));
 el('candidate-count').textContent=`Showing ${Math.min(visibleCandidates,rows.length)} of ${rows.length} matching destinations`;
 el('more-candidates').hidden=rows.length<=visibleCandidates;
}
/** Follow reporting automatically unless this is an explicitly saved-stage view.
 * Args: none.
 * Returns: promise; navigates when reporting exists, retains ?stay views and
 * stopped/failed ranking, retries failed status reads, and never posts new work.
 */
async function refresh(){
 clearTimeout(timer);
 try{
  const state=await api();
  if(followCurrentRound(state))return;
  if(!state.link_ranking){renderStages(state);timer=setTimeout(refresh,1200);return;}
  if(state.report&&!new URLSearchParams(location.search).has('stay')){location.replace(`/report/${sid}`);return;}
  const data=await api('/link-ranking-data'),p=state.link_ranking;
  const heading=el('selected-heading')||el('selected').previousElementSibling;
  heading.textContent=p?.status==='completed'?'Selected links':p?.status==='running'?'Provisional top links — ranking in progress':'Top links from available ratings — ranking incomplete';
  renderStages(state);el('question').textContent=state.research_question;
  el('status').textContent=p?.error||(p?.status==='waiting'?'Waiting for your decision. No links have been ranked.':`Link ranking ${p?.status||'not started'}.`);
  el('counts').textContent=p?.unique_destinations!==undefined?`${p.discovered_occurrences} discovered link occurrences · ${p.unique_destinations} unique destinations · ${p.duplicates_combined} duplicates combined · ${p.excluded} excluded · ${p.shortlisted} shortlisted · ${p.not_assessed} not assessed (shortlist limits) · ${p.candidates_ranked||0} ranked · ${data.selected.length} selected`:`${p?.candidates_ranked||0}/${p?.candidates_total||0} links ranked · ${data.selected.length} selected`;
  el('progress').max=Math.max(1,p?.batches_total||0);el('progress').value=p?.batches_finished||0;
  el('stop').hidden=p?.status!=='running';el('stop').disabled=!!state.stop_requested;
  el('retry').hidden=!['failed','cancelled','interrupted'].includes(p?.status)||!!state.report;
  el('report').hidden=!state.report;el('report').href=`/report/${sid}`;
  el('issues').replaceChildren(...data.issues.map(issue=>node('p',issue)));
  cards('selected',data.selected);candidateRows=data.candidates;renderCandidates();
  if(state.status?.endsWith('_running')||state.status==='link_ranking_waiting')timer=setTimeout(refresh,1200);
 }catch(error){showActivityIssue(true);el('status').textContent=error.message;timer=setTimeout(refresh,3000);}
}
/** Dispatch a clicked control, then refresh without duplicate submissions.
 * Args: suffix — stop or report action; button — initiating control.
 * Returns: promise; rejected requests are visible and restore the control.
 */
async function action(suffix,button){button.disabled=true;try{await api(suffix,true);await refresh();}catch(error){showActivityIssue();el('status').textContent=error.message;}finally{button.disabled=false;}}
el('candidate-filter').addEventListener('change',()=>{visibleCandidates=50;renderCandidates();});
el('more-candidates').addEventListener('click',()=>{visibleCandidates+=50;renderCandidates();});
el('stop').addEventListener('click',()=>action('/stop',el('stop')));
el('retry').addEventListener('click',()=>action('/report',el('retry')));
refresh();
