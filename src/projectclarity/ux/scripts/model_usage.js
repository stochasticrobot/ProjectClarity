/*
 * Show how many AI requests the research has made and how much text usage is known.
 *
 * Each page has a small, expandable Usage panel so accounting is available without
 * overshadowing the research. This file reads the saved request records and displays
 * counts, reported usage and individual attempts, including failures. The text units
 * reported by the AI service are called tokens and are relevant to its charges.
 *
 * Unknown usage is shown as unknown rather than as zero. Opening or refreshing this
 * panel only reads existing records; it does not ask the AI to do more work or enforce
 * a spending limit.
 */
'use strict';
let usageSession=null,usageUpdated=0,usageLoading=false;
/** Keep a collapsed usage audit panel on every screen, outside the main workflow.
 * Args: state — current snapshot, or empty state before a question exists.
 * Returns: promise; reads metadata only and preserves the panel's open state.
 * Network errors are shown without interrupting the main screen or starting calls.
 */
async function renderUsage(state={}){
 let panel=document.getElementById('usage-panel');
 if(!panel){panel=document.createElement('details');panel.id='usage-panel';panel.className='usage-panel';const summary=document.createElement('summary');summary.id='usage-summary';panel.append(summary);const body=document.createElement('div');body.id='usage-body';panel.append(body);document.querySelector('main').append(panel);}
 const sid=state.session_id;
 if(!sid){document.getElementById('usage-summary').textContent='Usage · no model calls yet';document.getElementById('usage-body').textContent='Call and token accounting will appear here after you start a question.';usageSession=null;return;}
 if(usageLoading||(usageSession===sid&&Date.now()-usageUpdated<1000))return;
 usageLoading=true;usageSession=sid;
 try{
  const response=await fetch(`/api/setup/${sid}/usage`);if(!response.ok)throw new Error('Usage ledger unavailable.');const data=await response.json();if(usageSession!==sid)return;
  const t=data.totals;document.getElementById('usage-summary').textContent=`Usage · ${t.attempted} model calls · ${t.known_tokens.toLocaleString()} known tokens${t.unknown_usage?' · '+t.unknown_usage+' unknown usage':''}`;
  const body=document.getElementById('usage-body');body.replaceChildren();
  /** Append safe readable audit text without rendering receipt content as markup.
   * Args: parent — target element; tag — DOM tag; text — plain audit text.
   * Returns: appended element for optional further composition.
   */
  function add(parent,tag,text){const n=document.createElement(tag);n.textContent=text;parent.append(n);return n;}
  add(body,'p',data.scope+' Monetary cost estimates are not enabled. Run-wide request and token allowances are enforced separately. Cache tokens are counted separately from uncached input.');
  if(state.model_budget){const b=state.model_budget;add(body,'p',`Run budget: ${b.calls_used} / ${b.call_limit} calls · ${b.known_tokens.toLocaleString()} known + ${b.estimated_tokens.toLocaleString()} estimated tokens / ${b.token_limit.toLocaleString()} · ${b.reserved_calls} calls in flight (${b.reserved_tokens.toLocaleString()} reserved tokens).`);}
  for(const [stage,count] of Object.entries(data.stages))add(body,'p',`${stage}: ${count.attempted} calls · input ${count.input_tokens} · output ${count.output_tokens} (including ${count.thinking_tokens} reported thinking) · cache read ${count.cache_read_input_tokens} · cache write ${count.cache_creation_input_tokens} · ${count.unknown_usage} unknown usage · ${count.rejected} recorded validation rejections · ${count.validation_unknown} validation outcomes unknown`);
  const details=add(body,'details','');add(details,'summary','Individual calls');
  for(const call of data.calls){const row=add(details,'p',`${call.stage} · ${call.model} · ${call.outcome} · validation: ${call.validation} · ${call.started_at||'historical time unavailable'} · ${call.duration_seconds??'?'}s`);add(row,'small',`Call ${call.call_id} · input ${call.usage?.input_tokens??'?'} · output ${call.usage?.output_tokens??'?'} (thinking ${call.usage?.output_tokens_details?.thinking_tokens??'unreported'}, included in output) · cache read ${call.usage?.cache_read_input_tokens??'?'} · cache write ${call.usage?.cache_creation_input_tokens??'?'} · provider request ${call.provider_request_id||'unavailable'} · receipt ${call.receipt}`);}
  const a=add(body,'a','View ledger JSON');a.href=`/api/setup/${sid}/usage`;a.target='_blank';a.rel='noopener';usageUpdated=Date.now();
 }catch(error){document.getElementById('usage-summary').textContent='Usage · temporarily unavailable';document.getElementById('usage-body').textContent=error.message;}
 finally{usageLoading=false;}
}
