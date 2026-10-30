/** Render and control the local ProjectClarity dashboard in the browser.
 * The server supplies snapshots: JSON views of saved runs, claims, report entries,
 * source evidence and usage. This file turns those snapshots into panels, charts
 * and tables, preserves navigation/filter state, and refreshes active research.
 * Form handlers send explicit start, clarification and stop requests to the local
 * API. Merely viewing a run does not start or resume its research. Rendering does
 * not extract facts, resolve identities or judge whether evidence is sufficient.
 * index.html loads this script after the notification tracker in discoveries.js.
 */
'use strict';
/** Find a dashboard element so rendering and event handlers share one lookup.
 * Args:
 *   selector: CSS selector matched against the current document.
 * Returns:
 *   The first matching element, or null. Invalid selector syntax throws; no DOM
 *   content is changed.
 */
const $ = (selector) => document.querySelector(selector);
const state = {csrf: '', run: null, data: null, tab: 'overview', axis: 'time', query: '', type: '',
  support: '', page: 0, targetGoal: '', targetQuery: '', targetStatus: '', targetPage: 0, claimQuery: '', claimStatus: '', claimPage: 0, drawer: null, pendingJob: null, busy: false};

// Storage may be disabled by browser privacy settings; notifications still work in memory.
let notificationStorage;try{notificationStorage=window.sessionStorage;}catch{}
const discoveries=new DiscoveryTracker(notificationStorage);
let treasureTimer;
let refreshTimer, refreshQueued=false, refreshFailures=0;

/** Escape a display value before inserting it as text in generated HTML.
 * Args:
 *   value: Any value to display; null and undefined become an empty string.
 * Returns:
 *   Text with HTML-significant characters escaped. This does not validate URLs
 *   or modify the document; link destinations have a separate check.
 */
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
/** Format a count for display while keeping missing measurements distinct from zero.
 * Args:
 *   n: Numeric or number-convertible count; null/undefined means not assessed.
 * Returns:
 *   Locale-formatted number text, or "Not assessed" for a missing count.
 *   Other invalid numeric values use JavaScript's NaN display; state is unchanged.
 */
function num(n) { return n == null ? 'Not assessed' : Number(n).toLocaleString(); }
/** Format a saved event time in the browser's local timezone.
 * Args:
 *   value: Timestamp string accepted by Date, or a number of Unix seconds.
 * Returns:
 *   Locale-formatted hour/minute text. Invalid dates retain the browser's invalid-
 *   date display; this function does not infer a missing timestamp or save state.
 */
function clock(value) { return new Date(typeof value === 'number' ? value * 1000 : value).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); }
/** Render a status label as a small dashboard badge.
 * Args:
 *   value: Status string, such as needs_review; an empty value displays unknown.
 * Returns:
 *   Escaped span markup with underscores replaced by spaces in the visible text.
 *   No status is assessed or changed, and the DOM is not updated here.
 */
function badge(value) { return `<span class="pill ${esc(value)}">${esc((value || 'unknown').replaceAll('_',' '))}</span>`; }
/** Render an external source link without allowing executable URL schemes.
 *
 * Args:
 *   url: Source URL or local-source label; only absolute HTTP(S) URLs become links.
 *   text: Optional visible label; an empty label falls back to url.
 * Returns:
 *   Escaped link markup opening a new tab, or escaped non-link text for local,
 *   missing or invalid destinations. Does not navigate or fetch the source.
 */
function link(url, text) { try { const u = new URL(url); if (!['http:','https:'].includes(u.protocol)) throw Error(); return `<a class="source-link" href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">${esc(text || url)} ↗</a>`; } catch { return `<span class="source-link">${esc(text || url || 'Local source')}</span>`; } }
/** Read or mutate the local dashboard API from browser event handlers.
 *
 * Args:
 *   path: Same-origin application endpoint, such as /api/runs.
 *   payload: Optional JSON body. Omitted means GET; any supplied value means POST
 *       with the server-issued CSRF token from state.csrf.
 * Returns:
 *   Promise resolving to decoded JSON. HTTP errors reject with the response
 *   error text; network or JSON-decoding errors also reject. Sends one request.
 */
async function api(path, payload) {
  const options = payload === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Clarity-CSRF':state.csrf},body:JSON.stringify(payload)};
  const response = await fetch(path, options); const body = await response.json();
  if (!response.ok) throw Error(body.error || 'Request failed.'); return body;
}
/** Show a dashboard request error without preventing the user from navigating.
 * Args:
 *   message: Text for the error banner; an empty value clears/hides it.
 * Returns:
 *   Nothing. Updates the existing error element; it does not retry the request.
 */
function notice(message='') { $('#notice').textContent=message; $('#notice').hidden=!message; }
/** Switch the displayed investigation without resuming or modifying research.
 *
 * Args:
 *   id: Saved run identifier chosen from navigation or application launch state.
 * Returns:
 *   Nothing. Clears filters, paging, notifications and the evidence drawer;
 *   updates the URL fragment and starts an asynchronous snapshot refresh.
 */
function selectRun(id) { $('#treasure').hidden=true;clearTimeout(treasureTimer);state.run=id; state.targetGoal='';state.targetQuery='';state.targetStatus='';state.targetPage=0;state.page=0; state.claimPage=0; state.drawer=null; $('#drawer').hidden=true; $('#drawer-shade').hidden=true; location.hash=id; refresh(); }
/** Draw saved count history on the selected elapsed-time or model-call axis.
 *
 * Args:
 *   samples: Ordered samples with timestamp in Unix seconds and values data.
 *   series: Array of {value, color}; value(sample) returns a numeric count or null,
 *       and color is an application-owned SVG stroke color.
 *   label: Accessible chart title and empty-state description.
 * Returns:
 *   SVG markup, or an empty-history message. Uses state.axis, falling back to time
 *   when call counts are unavailable. Null values break lines within retained
 *   samples; samples missing every series are omitted. No DOM or data mutation.
 */
function chart(samples, series, label) {
  const rows=samples.filter(r=>series.some(s=>s.value(r)!=null));
  if (!rows.length) return `<div class="chart-empty">No recorded ${esc(label.toLowerCase())} history yet.</div>`;
  const useCalls=state.axis==='calls' && rows.every(r=>r.values.tokens?.calls!=null);
  const xval=r=>useCalls?r.values.tokens.calls:r.timestamp;
  const first=xval(rows[0]), last=xval(rows.at(-1));
  const max=Math.max(1,...rows.flatMap(r=>series.map(s=>s.value(r)??0)));
  const x=r=>first===last?338:48+((xval(r)-first)/Math.max(1,last-first))*580;
  const y=v=>178-(v/max)*145;
  let svg=`<svg class="chart" viewBox="0 0 650 215" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>`;
  for(let i=0,n=Math.min(3,max);i<=n;i++) { const val=max*i/n, yy=y(val); svg+=`<line class="grid" x1="48" x2="628" y1="${yy}" y2="${yy}"/><text x="40" y="${yy+4}" text-anchor="end">${esc(Math.round(val).toLocaleString())}</text>`; }
  if(first===last)svg+='<text x="338" y="202" text-anchor="middle">Current count</text>';
  else for(let i=0;i<=4;i++) {const val=first+(last-first)*i/4;svg+=`<text x="${48+580*i/4}" y="202" text-anchor="middle">${esc(useCalls?Math.round(val)+' calls':last-first<10?(val-first).toFixed(1)+'s':last-first<120?Math.round(val-first)+'s':Math.round((val-first)/60)+'m')}</text>`;}
  for(const s of series) { let path='',connected=false; for(const r of rows) {const v=s.value(r); if(v==null){connected=false;continue;} path+=`${connected?' L':' M'}${x(r).toFixed(1)},${y(v).toFixed(1)}`;connected=true;} svg+=`<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2.5" stroke-linejoin="round"/>`; const end=rows.at(-1),v=s.value(end); if(v!=null)svg+=`<circle cx="${x(end)}" cy="${y(v)}" r="3" fill="${s.color}"/>`; }
  return svg+'</svg>';
}
/** Display known provider token totals separately from the model-call budget.
 *
 * Args:
 *   data: Snapshot whose tokens field contains totals, completeness flags, active
 *       calls, request limit and per-task usage.
 * Returns:
 *   Usage-card markup. Incomplete accounting is labelled as a subtotal, never
 *   zero or an exact bill; cache tokens are not added again. No state is changed.
 */
function usageCard(data) {
  const t=data.tokens, prefix=t.complete?'':'≥ ';
  return `<section class="card"><div class="card-heading"><h2>Token usage</h2>${badge(t.complete?'reported':'partial_usage')}</div><div class="usage-grid"><div><span>INPUT</span><strong>${prefix}${num(t.input)}</strong></div><div><span>OUTPUT</span><strong>${prefix}${num(t.output)}</strong></div><div><span>TOTAL</span><strong>${prefix}${num(t.total)}</strong></div></div><p class="fine">${t.complete?'Provider-reported usage.':'Known subtotal only; unreported or in-flight usage is not zero.'} Cache tokens are not added twice.</p><div class="usage-row"><strong>Model requests</strong><span>${num(t.calls)} / ${num(t.limit)}</span></div><progress aria-label="Model requests used" value="${t.calls}" max="${Math.max(1,t.limit)}"></progress><p class="fine">${num(t.active_calls)} in flight · ${num(t.missing_calls)} calls without per-call usage</p><details><summary>Usage by task${t.breakdown_complete?'':' · partial'}</summary>${Object.entries(t.by_task).map(([name,v])=>`<div class="usage-row"><span>${esc(TASKS[name]||name.replaceAll('_',' '))}</span><span>${num(v.total)} tokens · ${v.calls} calls</span></div>`).join('')}</details></section>`;
}
const TASKS={design_answer:'Answer design',extract_knowledge:'Reading / extraction',review_claims:'Claim review',plan_research:'Planning',study_coverage:'Scope study',assess_research_gaps:'Coverage review',select_sources:'Search selection',select_followup_sources:'Link selection',accumulate_knowledge:'Broad knowledge'};
/** Show detailed counts against an established target, not a scope estimate.
 *
 * Args:
 *   m: Goal measurement containing counts, optional target, percentages and target
 *       basis. Missing/evidence_checks measurements have no population fraction.
 *   unit: Human-readable counting unit, such as researchers or documents.
 * Returns:
 *   Escaped count/status markup. Percentages come from the checked measurement;
 *   exceeded targets are flagged, not silently treated as successful completion.
 */
function goalProgress(m,unit) {
  if(!m||m.kind==='evidence_checks')return '<p class="fine">Progress is assessed through evidence checks, not a population percentage.</p>';
  const target=m.target==null?'':` / ${num(m.target)}`;
  const percent=v=>v==null?'':` · ${Number(v).toFixed(1)}%`;
  return `<div class="scope-number">${num(m.identified)}${target}${percent(m.found_percent)}</div><p>${esc(unit)} identified</p><p><strong>${num(m.supported)}${target} source-supported${percent(m.supported_percent)}</strong></p>${m.target>0&&!m.target_exceeded?`<progress aria-label="Source-supported results toward target" value="${m.supported}" max="${m.target}"></progress>`:''}<p>${num(m.pending)} pending · ${num(m.disputed)} disputed · ${num(m.identity_pending||0)} observations awaiting identity resolution</p><p class="fine">Target: ${esc(m.target_basis.replaceAll('_',' '))}${m.target==null?' — no exact completeness percentage':''}.</p>${m.target_exceeded?'<p class="fine">More results than the target: reconcile scope, identities or the target before completion.</p>':''}`;
}
/** Show one goal count alongside its estimated scope without claiming completion.
 *
 * Args:
 *   data: Dashboard snapshot with scope_summary, claims and action_context.
 *   goal: Selected target inventory with identified count and optional measurement;
 *       null uses claims attached to the first action-context goal instead.
 * Returns:
 *   Count/range markup. Report counts include unfinished entries. Matching scope
 *   estimates take precedence over an exact-target fallback; unknown totals show ?.
 *   Claim-only work has no inferred population denominator. Does not mutate data.
 */
function compactGoalProgress(data,goal) {
  const id=goal?.goal_id||data.action_context?.goals?.[0]?.id;
  const estimate=(data.scope_summary||[]).find(e=>e.goal_id===id||(e.goal_ids||[]).includes(id));
  const finite=n=>typeof n==='number'&&Number.isFinite(n)&&n>=0;
  const lower=estimate?.lower??estimate?.range?.lower, upper=estimate?.upper??estimate?.range?.upper;
  let denominator='?';
  if(goal&&finite(lower)&&finite(upper))denominator=lower===upper?`~${num(lower)}`:`~${num(lower)}–${num(upper)}`;
  else if(goal&&finite(lower))denominator=`≥~${num(lower)}`;
  else if(goal&&finite(upper))denominator=`≤~${num(upper)}`;
  else if(goal&&finite(goal.measurement?.target))denominator=num(goal.measurement.target);
  const count=goal?.identified??(data.claims||[]).filter(c=>c.goals.includes(id)).length;
  return `<div class="scope-number">${num(count)} / ${denominator}</div><div class="fine">${goal?'Report entries / estimated scope':'Claims / unknown total'}</div>`;
}
/** Keep completion diagnostics available outside the compact Goal Progress panel.
 *
 * Args:
 *   data: Dashboard snapshot with optional completion checks and blocker messages.
 * Returns:
 *   Escaped details-card markup, or an empty string before completion data exists.
 *   Reads the current stopping-details element to preserve its expanded state.
 *   Does not change completion decisions, saved evidence or DOM contents itself.
 */
function completionDetails(data) {
  const c=data.completion;if(!c)return '';
  const open=$('#stopping-details')?.open;
  return `<section class="card"><h2>Completion details</h2><p>${c.passed} / ${c.total} checks satisfied · ${c.fresh?'Current assessment':'Awaiting a current assessment'}</p>${batchReviewStatus(c)}<details id="stopping-details" ${open?'open':''}><summary>What remains before stopping?</summary>${c.blockers.map(x=>`<p>${esc(x)}</p>`).join('')||'<p>All evidence gates passed.</p>'}</details></section>`;
}
/** Summarize all goal inventories without combining incompatible counting units.
 *
 * Args:
 *   data: Snapshot with result_progress.goals and their measurements/inventories.
 * Returns:
 *   Summary-card markup retaining each goal, its counts and incomplete/unassigned
 *   observations. Unshaped goals show supported-claim counts, not invented rows.
 */
function resultCard(data) {
  const goals=data.result_progress?.goals||[];
  return `<section class="card"><h2>Current answer progress</h2>${goals.length?goals.map(g=>`<div class="goal"><strong>${esc(g.goal_id)}</strong><p>${esc(g.question)}</p>${goalProgress(g.measurement,g.results?.unit)}${g.results?`<small class="muted">${g.results.incomplete} incomplete ${g.results.count_basis==='answer-schema-v1'?`records · ${g.results.unassigned_count||0} unassigned observations`:'observations'}</small>`:`<span class="pill">${g.supported_facts} source-supported claims</span>`}</div>`).join(''):'<p class="muted">The plan and first findings will appear here.</p>'}</section>`;
}
/** Choose one goal inventory so counts never mix people, documents or other units.
 *
 * Args:
 *   data: Snapshot with an optional targets array in display order.
 * Returns:
 *   The inventory matching state.targetGoal, otherwise the first inventory,
 *   or null when none exists. Does not update the selected goal or input.
 */
function targetGoal(data) {return (data.targets||[]).find(g=>g.goal_id===state.targetGoal)||(data.targets||[])[0]||null;}
/** Build the goal dropdown used by target charts and target tables.
 *
 * Args:
 *   data: Snapshot whose targets contain goal_id and counting unit.
 * Returns:
 *   Escaped select markup with the current/fallback goal selected. Does not
 *   install event handlers or change state; init handles selection events.
 */
function targetSelector(data) {const g=targetGoal(data);return `<select id="target-goal" aria-label="Target research goal">${(data.targets||[]).map(x=>`<option value="${esc(x.goal_id)}" ${x.goal_id===g?.goal_id?'selected':''}>${esc(x.goal_id+' · '+x.unit)}</option>`).join('')}</select>`;}
/** Display requested result entries with their evidence status and field variants.
 *
 * Args:
 *   data: Snapshot containing target inventories and rows; raw entity mentions
 *       are not substituted for report entries.
 * Returns:
 *   Filtered, paginated table markup or an empty-state message. Reads target
 *   query/status/goal from browser state and clamps state.targetPage to available
 *   rows. Missing-identity observations stay outside identified-entry counts.
 */
function targetsPanel(data) {
  const g=targetGoal(data);if(!g)return '<section class="card"><h2>Research targets</h2><p>No item-list goal has been defined. This question may call for an explanation rather than a list.</p></section>';
  const rows=g.rows.filter(r=>JSON.stringify(r.records.map(x=>x.display_values||x.values)).toLowerCase().includes(state.targetQuery.toLowerCase())&&(!state.targetStatus||r.status===state.targetStatus));
  state.targetPage=Math.min(state.targetPage,Math.max(0,Math.ceil(rows.length/50)-1));
  return `<section class="card"><div class="card-heading"><h2>Research targets</h2>${targetSelector(data)}</div><p>${esc(g.question)}</p><p class="fine">Only items proposed for this goal. ${g.count_basis==='answer-schema-v1'?'Observations combine only when their complete declared identity agrees. Fields retain their evidence status.':'Repeated observations and explicit person-name formatting variants merge only when all required result fields match.'} Uncertain aliases are not guessed. Source support is not a separate confirmation of eligibility. Open an item to inspect qualifications and field variants.</p><div class="filterbar"><input id="target-query" aria-label="Search research targets" placeholder="Search target names and fields…" value="${esc(state.targetQuery)}"><select id="target-status" aria-label="Filter target support"><option value="">All candidate targets</option>${['supported','pending','disputed','identity_pending','incomplete'].map(x=>`<option value="${x}" ${state.targetStatus===x?'selected':''}>${x==='supported'?'Source-supported':x==='pending'?'Pending review / evidence':x==='identity_pending'?'Unresolved identity':x==='incomplete'?'Missing required fields':'Disputed'}</option>`).join('')}</select></div><div class="table-wrap"><table><thead><tr><th>TARGET</th>${g.fields.filter(f=>f!==g.name_field).map(f=>`<th>${esc(f.replaceAll('_',' '))}</th>`).join('')}<th>EVIDENCE</th></tr></thead><tbody>${rows.slice(state.targetPage*50,(state.targetPage+1)*50).map(r=>`<tr><td><button class="entity-name" data-target="${r.id}">${esc(r.label)}</button>${r.variants>1?`<div class="fine">${r.variants} field variants · inspect</div>`:''}${r.qualifications.length?'<div class="fine">Qualifications recorded</div>':''}</td>${g.fields.filter(f=>f!==g.name_field).map(f=>`<td>${[...new Set(r.records.flatMap(x=>x.display_values?.[f]||[x.values[f]]))].map(esc).join(' / ')}</td>`).join('')}<td>${badge(r.status)}</td></tr>`).join('')}</tbody></table></div>${!rows.length?'<p>No matching targets yet.</p>':''}<div class="pagination"><span>${rows.length} target groups · ${g.count_basis==='answer-schema-v1'?`${g.incomplete} incomplete records · ${g.unassigned_observations?.length||0} unassigned observations not counted`:`${g.incomplete} incomplete observations not counted`}</span><div><button data-target-page="-1" ${state.targetPage===0?'disabled':''}>Previous</button> <button data-target-page="1" ${(state.targetPage+1)*50>=rows.length?'disabled':''}>Next</button></div></div></section>`;
}
/** Plot one goal using only history compatible with its current counting policy.
 *
 * Args:
 *   data: Snapshot with targets, history samples and current metrics.
 * Returns:
 *   Chart and goal-selector markup. Uses state.targetGoal; historical samples
 *   with another count_basis are omitted. If no compatible history exists, shows
 *   a current-only point rather than inventing a historical curve.
 */
function targetChart(data) {
  const g=targetGoal(data);if(!g)return '<p class="muted">No list of targets has been defined for this question.</p>';
  const history=data.history.filter(r=>r.values.targets?.[g.goal_id]?.count_basis===g.count_basis);
  const current={timestamp:Date.now()/1000,values:data.metrics};
  const samples=history.length?history:[current];
  return `<div class="target-selector">${targetSelector(data)}</div><div class="legend"><span>Candidate targets identified</span><span class="amber">Source-supported targets</span></div>${chart(samples,[{value:r=>r.values.targets?.[g.goal_id]?.identified,color:'#12796c'},{value:r=>r.values.targets?.[g.goal_id]?.supported,color:'#c28b37'}],'Target discovery and support')}<p class="fine">${esc(g.unit)}. Counts use the shared goal-shaped result inventory, not claim mentions. Qualifications and unresolved identities remain in the Targets view. Counts can decrease when evidence is disputed or identities become uncertain.${history.length?' Historical points use the evidence status recorded at the time.':' No target history was recorded by this worker; showing the current count only. New runs record the curve.'}</p><button data-tab="targets">Explore these targets →</button>`;
}
/** Compose the activity, target-history, claim-preview and usage overview.
 *
 * Args:
 *   data: Current snapshot with history, activity, claims, usage and goal results.
 * Returns:
 *   Overview HTML. The latest-five claim preview is deliberately limited, but
 *   the report/claim views retain their own full inventories and paging. This
 *   renderer does not itself fetch data or mark any research complete.
 */
function overview(data) {
  const h=data.history;
  return `<div class="columns"><div class="stack"><section class="card"><div class="card-heading"><h2>Research gain</h2><select id="axis" aria-label="Chart horizontal axis"><option value="time" ${state.axis==='time'?'selected':''}>Elapsed time</option><option value="calls" ${state.axis==='calls'?'selected':''}>Model calls</option></select></div>${targetChart(data)}</section><section class="card"><h2>Tokens over the investigation</h2><div class="legend"><span>Reported input</span><span class="blue">Reported output</span></div>${chart(h,[{value:r=>r.values.tokens?.input,color:'#12796c'},{value:r=>r.values.tokens?.output,color:'#557aae'}],'Token usage')}<p class="fine">Reported subtotals; failed calls may leave usage unknown.</p></section>${resultCard(data)}<section class="card"><h2>Latest candidate claims</h2><p class="fine">Recent discoveries, with their current review status.</p>${data.claims.slice(-5).reverse().map(c=>`<article class="claim-card">${badge(c.status)}<p>${esc(c.statement)}</p><button data-claim="${esc(c.id)}">Inspect evidence →</button></article>`).join('')||'<p class="muted">Waiting for the first candidates…</p>'}</section></div><div class="stack">${usageCard(data)}<section class="card"><div class="card-heading"><h2>Live activity</h2><span class="pill">Recorded actions</span></div><div class="activity">${data.activity.map(e=>`<div class="activity-item"><span class="activity-icon">●</span><div><div class="activity-time">${clock(e.timestamp)}</div><div class="activity-title">${esc(e.title)}</div><div class="activity-why">${esc(e.why)}</div>${e.source?`<span class="activity-source">${link(e.source,e.source_id+' · '+e.source)}</span>`:''}</div></div>`).join('')||'<p class="muted">Waiting for the first action…</p>'}</div></section></div></div>`;
}
/** Describe one provisional scope estimate without computing a completion rate.
 *
 * Args:
 *   e: Estimate with direct or nested lower/upper bounds, unit, basis, method and
 *       optional confidence, assumptions and limitations.
 * Returns:
 *   Escaped estimate markup. A missing lower bound displays Unknown; an absent
 *   upper bound is labelled unassessed rather than inferred. No counts change.
 */
function estimate(e) {
  const lo=e.lower??e.range?.lower, hi=e.upper??e.range?.upper;
  const range=lo==null?'Unknown':lo===hi?num(lo):`${num(lo)}–${num(hi)}`;
  return `<div class="estimate"><strong>${range}</strong><p>${esc(e.unit||e.scale||'Scope estimate')}</p><div class="inline-list">${badge(e.basis||'provisional')}${e.confidence?badge(e.confidence+' confidence'):''}</div><small>${esc(e.method||e.rationale||'')}</small>${(e.limitations||[]).length?`<details><summary>Assumptions and limitations</summary><ul class="small-list">${[...(e.assumptions||[]),...(e.limitations||[])].map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>`:''}</div>`;
}
/** Present the plan, clarification form and completion rules in Plan & scope.
 *
 * Args:
 *   data: Snapshot with optional plan, pending_review, estimates, criteria and
 *       completion state. All form text comes from the saved run.
 * Returns:
 *   HTML for the available plan or a planning placeholder. Shows stale coverage
 *   explicitly and keeps initial estimates separate from stopping requirements.
 *   Submitting clarification is handled elsewhere; rendering starts no work.
 */
function planPanel(data) {
  const p=data.plan;if(!p)return answerSchemaCard(data)+'<section class="card"><h2>Preparing the plan</h2><p>The research question has been submitted. The plan will appear when the planner returns.</p></section>';
  const checks=data.criteria||[], study=data.estimates.study||{}, profile=study.readiness||{};
  const review=data.pending_review;
  const schema=answerSchemaCard(data);
  const questions=review?.questions||[];
  const clarification=review?`<section class="card full-width"><h2>Your clarification is needed</h2><p>Answer these questions to continue the same run and budget.</p><form id="review-form" data-review-id="${esc(review.review_id)}">${questions.map(q=>`<label>${esc(q.question||q.text)}<textarea data-answer="${esc(q.question_id||q.id)}" required></textarea></label>`).join('')}<button class="primary" type="submit">Submit answers and continue</button> <button type="button" id="reject-review">Reject this plan</button><p id="review-error" class="error-text"></p></form></section>`:'';
  return `<div class="stack">${schema}${completionDetails(data)}<div class="columns">${clarification}<div class="stack"><section class="card"><h2>Research plan</h2><p>${esc(p.objective)}</p>${p.goals.map(g=>`<div class="goal"><strong>${esc(g.goal_id)} · ${esc(g.question)}</strong><ul class="small-list">${g.evidence_required.map(e=>`<li>${esc(e)}</li>`).join('')}</ul></div>`).join('')}<h3>Approach</h3><ol class="small-list">${p.strategy.map(s=>`<li>${esc(s.action)}</li>`).join('')}</ol><details><summary>Scope boundaries and assumptions</summary><h3>Included</h3><ul class="small-list">${p.scope_in.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><h3>Excluded</h3><ul class="small-list">${p.scope_out.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><ul class="small-list">${p.assumptions.map(x=>`<li>${esc(typeof x==='string'?x:JSON.stringify(x))}</li>`).join('')}</ul></details></section><section class="card"><h2>Stopping criteria</h2><p class="fine">Automatic stopping: ${data.automatic_stopping?'enabled':'off'}. Assessment: ${esc(data.coverage_freshness.status)}. Resource limits are separate.</p>${p.completion_criteria.map(c=>{const status=checks.find(x=>x.criterion_id===c.criterion_id);return `<div class="goal">${badge(status?.status||'not_assessed')} <strong>${esc(c.criterion_id)}</strong><p>${esc(c.description)}</p><p class="fine">${esc(c.measurement_method)}</p>${status?.gaps?.length?`<details><summary>Recorded gaps${data.coverage_freshness.status==='stale'?' · historical':''}</summary><ul class="small-list">${status.gaps.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>`:''}</div>`;}).join('')}${data.readiness_error?`<p class="error-text">Additional readiness checks were not fully assessed: ${esc(data.readiness_error)}.</p>`:''}<details><summary>Scope-study readiness checks</summary>${(profile.coverage_checks||[]).map(c=>`<div class="goal"><strong>${esc(c.check_id)}</strong><p>${esc(c.description)}</p><p class="fine">${esc(c.assessment_method)}</p></div>`).join('')||'<p>No additional checks recorded.</p>'}</details></section></div><div class="stack"><section class="card"><h2>Initial scope estimates</h2><p class="fine">Retained for reporting. These are not stopping targets or completion percentages.</p>${data.estimates.initial.map(estimate).join('')||'<p class="muted">No numeric estimate was proposed.</p>'}</section><section class="card"><h2>Early scope study</h2>${badge(study.status||'not_started')}<p>${esc(study.summary||'Study findings will appear here when available.')}</p>${(study.estimates||[]).map(estimate).join('')}<details><summary>Limitations</summary><ul class="small-list">${(study.limitations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details></section>${(data.estimates.current||[]).length?`<section class="card"><h2>Latest estimate revisions</h2>${data.estimates.current.map(estimate).join('')}</section>`:''}</div></div></div>`;
}
/** Show the accepted report specification before or during evidence collection.
 *
 * Args:
 *   data: Snapshot with an optional answer_schema containing sections and checks.
 * Returns:
 *   Escaped specification-card markup, or an empty string for an undesigned run.
 *   Field meanings, identity and multiplicity are shown as requirements, not as
 *   evidence that any report entry already meets them.
 */
function answerSchemaCard(data) {
  const s=data.answer_schema;if(!s)return '';
  return `<section class="card"><h2>Answer specification · ${esc(s.title)}</h2><p>${esc(s.scope)}</p>${s.as_of?`<p>As of ${esc(s.as_of)}</p>`:''}${s.sections.map(x=>`<div class="goal"><strong>${esc(x.title)}</strong> · ${esc(x.format)}<p>${esc(x.question)}</p>${x.fields.length?`<p>One entry: ${esc(x.unit)} · Identity: ${x.identity_fields.map(esc).join(', ')}</p><ul>${x.fields.map(f=>`<li><strong>${esc(f.name)}</strong>: ${esc(f.meaning)} <span class="fine">${f.multiplicity==='multiple'?'Multiple supported values':'Single resolved value'} · ${esc(f.absence_meaning)}</span></li>`).join('')}</ul>`:''}<ul>${x.evidence_requirements.map(e=>`<li>${esc(e)}</li>`).join('')}</ul></div>`).join('')}${s.interpretation_notes.length?`<p class="fine">Interpretations: ${s.interpretation_notes.map(esc).join(' · ')}</p>`:''}${(s.completion_checks||[]).length?`<h3>Required completion checks</h3>${s.completion_checks.map(c=>`<div class="goal"><strong>${esc(c.criterion_id)} · ${esc(c.description)}</strong><p>${esc(c.measurement_method)}</p><p class="fine">${c.result_goal_id?'Report entries · '+esc(c.result_goal_id):'Supporting coverage check'} · ${esc(c.unit)}</p><ul>${c.evidence_required.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`).join('')}`:''}</section>`;
}
/** Distinguish evidence-batch review progress from whole-answer completion.
 *
 * Args:
 *   completion: Completion projection with optional batch_progress state.
 * Returns:
 *   Batch-status markup when a manifest exists, otherwise an empty string.
 *   Completed batch reviews alone are not labelled a complete research answer.
 */
function batchReviewStatus(completion) {
  const p=completion?.batch_progress;
  if(!p||p.mode!=='incremental')return '';
  return `<p><strong>Evidence review: ${esc(p.completed)} / ${esc(p.total)} batches</strong></p><p class="fine">${esc(p.candidates_reviewed)} / ${esc(p.candidate_total)} evidence records reviewed · ${esc(p.pending)} pending · ${esc(p.failed)} failed or uncertain. ${completion.ready?'The whole-answer assessment passed.':'Batch completion alone does not establish answer completeness.'}</p>`;
}
/** Display the derived claim-to-report gap list without filling fields from prose.
 *
 * Args:
 *   data: Snapshot with result_progress.mapping_ledger.rows and blocking flags.
 * Returns:
 *   Escaped card with evidence buttons and collapsible gap details, or an empty
 *   string when no blocking rows exist. Displays backend decisions as recorded;
 *   it does not validate their semantic correctness or schedule repair calls.
 */
function mappingGaps(data) {
  const gaps=(data.result_progress?.mapping_ledger?.rows||[]).filter(r=>r.blocking);
  if(!gaps.length)return '';
  return `<section class="card"><h2>Unresolved claim-to-report mappings (${gaps.length})</h2><p>These supported contributions are not yet accounted for in report fields. Completion is blocked.</p><details><summary>Inspect mapping gaps</summary>${gaps.map(r=>`<p>${badge(r.status)} ${esc(r.goal_id)} · ${esc(r.field_names.join(', ')||'Mapping not assessed')} — ${esc(r.rationale)} <button data-claim="${esc(r.candidate_id)}">Evidence</button></p>`).join('')}</details></section>`;
}
/** Place requested report entries before underlying mapping diagnostics.
 *
 * Args:
 *   data: Snapshot with optional answer_schema, targets and source-checked claims.
 * Returns:
 *   Escaped report HTML preserving every field status and unassigned observation.
 *   Narrative sections list supported claims without synthesizing conclusions.
 *   Legacy runs without a schema get a placeholder and any mapping diagnostics.
 *   No identities, fields, completeness judgments or model calls are invented.
 */
function reportPanel(data) {
  const schema=data.answer_schema;if(!schema)return mappingGaps(data)+'<section class="card"><h2>Report</h2><p>This run has no designed answer schema. Its findings remain in Targets and Claims & evidence.</p></section>';
  return `<div class="stack"><section class="card"><h2>${esc(schema.title)}</h2><p>${esc(schema.scope)}</p><p class="fine">Working report · Source support does not independently establish currentness or completeness.</p></section>${schema.sections.map(s=>{
    if(s.format==='narrative')return `<section class="card"><h2>${esc(s.title)}</h2><p class="fine">Supported findings for this section; completeness requires its evidence checks.</p>${data.claims.filter(c=>c.goals.includes(s.goal_id)&&c.status==='supported'&&c.roles.includes('answer_evidence')).map(c=>`<p>${esc(c.statement)} <button data-claim="${esc(c.id)}">Evidence</button></p>`).join('')||'<p>No supported findings yet.</p>'}</section>`;
    const g=data.targets.find(g=>g.goal_id===s.goal_id);
    return `<section class="card"><h2>${esc(s.title)}</h2>${g?compactGoalProgress(data,g):'<p>Waiting for the first entries.</p>'}<div class="table-wrap"><table><thead><tr>${s.fields.map(f=>`<th>${esc(f.name.replaceAll('_',' '))}</th>`).join('')}<th>Status</th><th>Evidence</th></tr></thead><tbody>${(g?.rows||[]).map(r=>{const record=r.records[0];return `<tr>${s.fields.map(f=>`<td>${esc((record.display_values?.[f.name]||[record.values[f.name]??'Unknown']).join('; '))}${record.field_status?.[f.name]?.status!=='supported'?`<div class="fine">${esc(record.field_status?.[f.name]?.status||'Not assessed')}</div>`:''}</td>`).join('')}<td>${badge(r.status)}</td><td><button data-target="${esc(r.id)}" data-goal="${esc(s.goal_id)}">Inspect</button></td></tr>`;}).join('')}</tbody></table></div>${g?.unassigned_observations?.length?`<details><summary>Unassigned evidence (${g.unassigned_observations.length} observations, not report entries)</summary>${g.unassigned_observations.map(o=>`<p>${Object.entries(o.values).map(([k,v])=>esc(k)+': '+esc(v)).join('; ')} ${o.candidate_ids.map(id=>`<button data-claim="${esc(id)}">Evidence</button>`).join(' ')}</p>`).join('')}</details>`:''}</section>`;
  }).join('')}${mappingGaps(data)}</div>`;
}
/** Browse extracted entity labels without treating every label as an answer item.
 *
 * Args:
 *   data: Snapshot containing entity groups, their source mentions and claim links.
 * Returns:
 *   Filtered, paginated entity-table markup. Reads query/type/support filters
 *   from state and clamps state.page to valid pages. Matching labels are not
 *   independently resolved real-world identities; no input evidence is modified.
 */
function entityPanel(data) {
  const rows=data.entities.filter(e=>(e.name+' '+e.type).toLowerCase().includes(state.query.toLowerCase())&&(!state.type||e.type===state.type)&&(!state.support||(state.support==='supported'?e.supported_claims>0:e.supported_claims===0)));
  state.page=Math.min(state.page,Math.max(0,Math.ceil(rows.length/50)-1));
  return `<section class="card"><div class="card-heading"><h2>Entity explorer</h2><span class="pill">${rows.length} label groups</span></div><p class="fine">${esc(data.entity_basis)} Click a name to inspect its claims, qualifications and evidence.</p>${data.broad_capture_enabled?'<p class="fine">This view covers answer-extraction entities; the separate broad-capture store is not merged into these counts.</p>':''}<div class="filterbar"><input id="entity-query" aria-label="Search entities" placeholder="Search names and types…" value="${esc(state.query)}"><select id="entity-type" aria-label="Filter entity type"><option value="">All types</option>${[...new Set(data.entities.map(e=>e.type))].sort().map(t=>`<option ${state.type===t?'selected':''}>${esc(t)}</option>`).join('')}</select><select id="entity-support" aria-label="Filter claim support"><option value="">All evidence statuses</option><option value="supported" ${state.support==='supported'?'selected':''}>Has supported claims</option><option value="pending" ${state.support==='pending'?'selected':''}>No supported claims yet</option></select></div><div class="table-wrap"><table><thead><tr><th>NAME</th><th>TYPE</th><th>CLAIM SUPPORT</th><th>GOAL MATCH</th></tr></thead><tbody>${rows.slice(state.page*50,(state.page+1)*50).map(e=>`<tr><td><button class="entity-name" data-entity="${e.id}">${esc(e.name)}</button><div class="fine">${new Set(e.mentions.map(m=>m.source_id)).size} sources · ${e.claim_ids.length} claims</div></td><td>${esc(e.type)}</td><td>${e.supported_claims?badge('supported')+' '+e.supported_claims:badge(e.pending_claims?'pending':e.claim_ids.length?'not_supported':'unreviewed')}</td><td>${badge('not_assessed')}</td></tr>`).join('')}</tbody></table></div>${!rows.length?'<p class="muted">No entities match these filters.</p>':''}<div class="pagination"><span>${rows.length?state.page*50+1:0}–${Math.min(rows.length,(state.page+1)*50)} of ${rows.length}</span><div><button data-page="-1" ${state.page===0?'disabled':''}>Previous</button> <button data-page="1" ${(state.page+1)*50>=rows.length?'disabled':''}>Next</button></div></div></section>`;
}
/** Browse candidate statements and review outcomes independently of report rows.
 *
 * Args:
 *   data: Snapshot containing claims with statements, statuses and evidence links.
 * Returns:
 *   Filtered claim-table markup with paging controls. Uses state.claimQuery and
 *   state.claimStatus and clamps state.claimPage. Rejected or incomplete claims
 *   remain inspectable instead of silently becoming supported report entries.
 */
function claimsPanel(data) {
  const rows=data.claims.filter(c=>c.statement.toLowerCase().includes(state.claimQuery.toLowerCase())&&(!state.claimStatus||c.status===state.claimStatus));
  state.claimPage=Math.min(state.claimPage,Math.max(0,Math.ceil(rows.length/30)-1));
  return `<section class="card"><h2>Claims & evidence</h2><p class="fine">Source support is not goal qualification or independent truth verification. Rejected and uncertain claims remain inspectable.</p><div class="filterbar"><input id="claim-query" aria-label="Search claims" placeholder="Search statements…" value="${esc(state.claimQuery)}"><select id="claim-status" aria-label="Filter claim status"><option value="">All statuses</option>${[...new Set(data.claims.map(c=>c.status))].sort().map(s=>`<option value="${s}" ${state.claimStatus===s?'selected':''}>${esc(s.replaceAll('_',' '))}</option>`).join('')}</select></div>${rows.slice(state.claimPage*30,(state.claimPage+1)*30).map(c=>`<article class="claim-card">${badge(c.status)} <span class="fine">${esc(c.id)} · ${esc(c.goals.join(', '))}</span><p class="statement">${esc(c.statement)}</p><button data-claim="${esc(c.id)}">Inspect evidence →</button></article>`).join('')||'<p class="muted">No claims match these filters.</p>'}<div class="pagination"><span>${rows.length} claims</span><div><button data-claim-page="-1" ${state.claimPage===0?'disabled':''}>Previous</button> <button data-claim-page="1" ${(state.claimPage+1)*30>=rows.length?'disabled':''}>Next</button></div></div></section>`;
}
/** Separate source acquisition, passage reading and search-provider outcomes.
 *
 * Args:
 *   data: Snapshot with source statuses, passage counts and search attempt receipts.
 * Returns:
 *   Escaped source/search tables. Downloaded pages are not labelled fully read,
 *   and supported-claim counts are not source-truth scores. Rendering performs
 *   no external search, retrieval or model request.
 */
function sourcesPanel(data) {
  return `<div class="stack"><section class="card"><h2>Sources · ${data.sources.length}</h2><p class="fine">Retrieving a page does not mean its full contents were read. Supported-claim counts measure contributions, not source truthfulness.</p><div class="table-wrap"><table><thead><tr><th>SOURCE</th><th>ACQUISITION</th><th>READING</th><th>SUPPORTED CLAIMS</th></tr></thead><tbody>${data.sources.map(s=>`<tr><td><strong>${esc(s.id)}</strong> ${link(s.location)}<div class="fine">${esc(s.origin)} · depth ${s.depth}</div></td><td>${badge(s.status)}${s.error?`<div class="fine">${esc(s.error)}</div>`:''}</td><td>${s.passages.completed||0} completed<br><span class="fine">${s.passages.not_attempted||0} queued · ${s.passages.failed||0} failed</span></td><td>${s.supported_claims}</td></tr>`).join('')}</tbody></table></div></section><section class="card"><h2>Searches · ${data.searches.length}</h2>${data.searches.map(q=>`<div class="goal"><div class="query">${esc(q.query)}</div><div class="inline-list">${badge(q.status)}<span class="tag">${esc(q.provider||'Pending provider')}</span><span class="tag">${esc(q.purpose||'Research exploration')}</span></div><p class="fine">${esc(q.reason||'')}</p><details><summary>Engine attempts and fallbacks</summary><ul class="small-list">${(q.attempts||[]).map(a=>`<li>${esc(a.provider)} — ${esc(a.status)}${a.error_code?' · '+esc(a.error_code):''}</li>`).join('')}</ul></details></div>`).join('')||'<p class="muted">No searches recorded.</p>'}</section></div>`;
}
/** Update current-work details and the compact goal-count sidebar.
 *
 * Args:
 *   data: Snapshot with action_context, work queue and optional target inventory.
 * Returns:
 *   Nothing. Replaces action-details and research-context HTML while preserving
 *   the scheduling-details expansion. Missing action_context leaves existing
 *   contents alone. Completion diagnostics remain in the Plan & scope renderer.
 */
function renderContext(data) {
  const c=data.action_context;if(!c)return;
  const selected=c.selected, expanded=$('#decision-details')?.open;
  const concurrent=(c.in_flight||[]).length>1?`<ul class="small-list">${c.in_flight.map(a=>`<li>${esc(a.source_id)} · passage ${esc(a.passage_id)} · ${link(a.location)}</li>`).join('')}</ul>`:'';
  const source=c.source?`<div class="current-document"><strong>${esc(c.source.id)} · ${link(c.source.location)}</strong>${c.passage_id?`<span class="tag">Passage ${esc(c.passage_id)}</span>`:''}${c.source_passages_total?`<span class="fine">${c.source_passages_total} inventoried passages in this source</span>`:''}</div>`:'';
  $('#action-details').innerHTML=`${c.historical?'<div class="fine">Last recorded work · this worker is no longer executing an action.</div>':''}${concurrent||source}${c.query?`<p class="query">Search: ${esc(c.query)}</p>`:''}<p class="action-summary">${esc(c.plain_reason||c.description)}</p><details id="decision-details" ${expanded?'open':''}><summary>Work queue & technical details · ${c.queue.reduce((n,r)=>n+r.count,0)} unfinished tasks</summary><div class="decision-body"><h3>Recorded selection</h3><p>${esc(selected.title)}${selected.source_id?' · '+esc(selected.source_id):''}${selected.passage_id?' · passage '+esc(selected.passage_id):''} ${badge(selected.status||'not_recorded')}</p>${selected.location?link(selected.location):''}<h3>Scheduling-policy context</h3><ul class="small-list">${c.policy_context.map(x=>`<li>${esc(x)}</li>`).join('')||'<li>No additional policy context recorded for this stage.</li>'}</ul><p class="fine">${esc(c.selection_note)}</p><h3>Saved work queue (including the selected action)</h3><div class="queue-table table-wrap"><table><thead><tr><th>WORK / SOURCE</th><th>UNITS</th><th>STATE</th></tr></thead><tbody>${c.queue.map(r=>`<tr><td>${esc(r.title)}${r.source_id?' · '+esc(r.source_id):''}${r.location?'<br>'+link(r.location):''}${r.reason?`<div class="fine">${esc(r.reason.replaceAll('_',' '))}</div>`:''}</td><td>${r.count}</td><td>${badge(r.status)}</td></tr>`).join('')||'<tr><td colspan="3">No queued work recorded.</td></tr>'}</tbody></table></div></div></details>`;
  const g=targetGoal(data);
  $('#research-context').innerHTML=`<section class="context-card"><div class="eyebrow">${c.studying?'RESEARCHING SCOPE':'CURRENT GOAL'}</div><p>${esc(g?.question||c.goals[0]?.question||'Planning the investigation…')}</p>${(data.targets||[]).length>1?'<button data-tab="targets">Choose another research goal →</button>':''}</section><section class="context-card"><div class="eyebrow">GOAL PROGRESS${g?' · '+esc(g.goal_id):''}</div>${compactGoalProgress(data,g)}<button class="context-link" data-tab="plan">Plan & scope details →</button></section>`;
}

/** Notify this browser about newly supported results without replaying old runs.
 *
 * Args:
 *   data: Current source-checked snapshot passed to the tab-local DiscoveryTracker.
 * Returns:
 *   Nothing. Updates the gold notification only for newly observed support,
 *   links the first new supported item and resets a fourteen-second hide timer.
 *   No sound, model request or saved research mutation occurs.
 */
function announceDiscoveries(data) {
  if(discoveries.run!==data.id){$('#treasure').hidden=true;clearTimeout(treasureTimer);}
  const fresh=discoveries.observe(data);if(!fresh.length)return;
  const first=fresh[0];
  $('#treasure-content').innerHTML=`<strong>Treasure found · ${fresh.length} ${first.result_id?(fresh.length===1?'report entry':'report entries')+' completed':(fresh.length===1?'claim':'claims')+' supported'}</strong><p>${esc(first.statement)}</p><span class="fine">Source-supported evidence${fresh.length>1?' · '+(fresh.length-1)+' more in Claims & evidence':''}</span><br><button ${first.result_id?'data-target="'+esc(first.result_id)+'" data-goal="'+esc(first.goal_id)+'"':'data-claim="'+esc(first.id)+'"'}>Inspect evidence →</button>`;
  $('#treasure').hidden=false;clearTimeout(treasureTimer);
  treasureTimer=setTimeout(()=>{$('#treasure').hidden=true;},14000);
}

/** Refresh dashboard panels while preserving active input and evidence reading.
 *
 * Args:
 *   force: Optional boolean, default false. Allows the selected panel to redraw
 *       while typing; an unchanged pending clarification form is still preserved.
 * Returns:
 *   Nothing. Reads state.data and updates DOM text, controls, charts and selected
 *   panel. Restores expanded details and activity scroll after a redraw. Does
 *   nothing before a snapshot exists; it does not refresh the evidence drawer.
 */
function render(force=false) {
  const d=state.data;if(!d)return;
  $('#workspace').hidden=false;$('#empty').hidden=true;$('#short-id').textContent=d.id.slice(0,8);$('#question').textContent=d.question;
  $('#run-meta').textContent=`${new Date(d.created_at).toLocaleString()} · ${Math.floor(d.status.elapsed_seconds/60)}m ${Math.floor(d.status.elapsed_seconds%60)}s recorded`;
  renderContext(d);
  $('#now').textContent=d.now;$('#why').textContent=d.why;$('#state').textContent=d.status.status.replaceAll('_',' ');
  $('#now-dot').classList.toggle('finished',!d.status.worker_running);$('#stop').hidden=!d.status.worker_running;$('#stop').disabled=!!d.status.cancel_requested;
  $('#stop').textContent=d.status.cancel_requested?'Stopping…':'Stop research';$('#report').href=`/api/runs/${d.id}/report`;
  const targets=targetGoal(d);
  $('#stats').innerHTML=[['Results identified',targets?num(targets.identified):'—',targets?`${targets.goal_id} · ${targets.unit}`:'Evidence-based goal; no population count'],['Source-supported targets',targets?num(targets.supported):'—','Complete answer records with source support'],['Pending targets',targets?num(targets.pending):'—','Awaiting review or stronger evidence'],['Supported claims',num(d.metrics.supported_claims),'All research evidence, not a target count']].map(([l,v,n])=>`<div class="stat"><div class="stat-label">${l}</div><div class="stat-value">${v}</div><div class="stat-note">${n}</div></div>`).join('');
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('selected',b.dataset.tab===state.tab));
  const typing=$('#panel').contains(document.activeElement)&&document.activeElement.matches('input,textarea,select');
  const reviewForm=$('#review-form');
  const preserveAnswers=state.tab==='plan'&&reviewForm&&d.pending_review&&reviewForm.dataset.reviewId===d.pending_review.review_id;
  if((force||!typing)&&!preserveAnswers) {
    const expanded=new Set([...$('#panel').querySelectorAll('details[open]')].map(e=>e.querySelector('summary')?.textContent));
    const scroll=$('#panel .activity')?.scrollTop||0;
    $('#panel').innerHTML=({overview,report:reportPanel,plan:planPanel,targets:targetsPanel,entities:entityPanel,claims:claimsPanel,sources:sourcesPanel}[state.tab])(d);
    $('#panel').querySelectorAll('details').forEach(e=>{if(expanded.has(e.querySelector('summary')?.textContent))e.open=true;});
    if($('#panel .activity'))$('#panel .activity').scrollTop=scroll;
  }
  // Keep evidence reading position stable while the background snapshot updates.
}
/** Open source-linked evidence already present in the selected snapshot.
 *
 * Args:
 *   kind: target, entity or claim; other values follow the claim lookup branch.
 *   id: Identifier in the selected snapshot corresponding to kind.
 *   focus: Optional boolean, default true; focuses the close button after opening.
 * Returns:
 *   Nothing. Records state.drawer and renders its evidence with escaped text.
 *   A missing ID returns without opening new content. No request or new review
 *   is made; closing the drawer is handled by the installed UI events.
 */
function showDrawer(kind,id,focus=true) {
  const d=state.data;state.drawer={kind,id};let html='';
  if(kind==='target') {
    const g=(d.targets||[]).find(g=>g.rows.some(r=>r.id===id));const r=g?.rows.find(r=>r.id===id);if(!r)return;
    html=`<div class="eyebrow">RESEARCH TARGET · ${esc(g.goal_id)}</div><h2>${esc(r.label)}</h2>${badge(r.status)}<p>${esc(g.unit)}</p><p class="fine">One goal-shaped result. Formatting variants and repeated observations retain their evidence. Ambiguous identities are not guessed; incomplete observations are not joined to fill gaps.</p>${r.records.map(record=>`<article class="claim-card">${badge(record.status)}${Object.entries(record.values).map(([k,v])=>`<p><strong>${esc(k.replaceAll('_',' '))}</strong><br>${esc((record.display_values?.[k]||[v]).join('; '))}${record.field_status?.[k]?`<br><span class="fine">${esc(record.field_status[k].status)}</span>`:''}</p>`).join('')}<ul class="small-list">${record.qualifications.map(q=>`<li>${esc(q)}</li>`).join('')}</ul>${record.candidate_ids.map(cid=>`<button data-claim="${esc(cid)}">Inspect evidence · ${esc(cid)}</button>`).join(' ')}</article>`).join('')}`;
  } else if(kind==='entity') {
    const e=d.entities.find(x=>x.id===id);if(!e)return;
    html=`<div class="eyebrow">ENTITY DETAILS</div><h2>${esc(e.name)}</h2>${badge(e.type)}<p class="fine">Exact label group; ${e.mentions.length} recorded mentions. Matching labels can still refer to different identities.</p><h3>Research-goal verification</h3><p>${esc(d.verification_note)}</p><div class="inline-list">${e.goals.map(g=>`<span class="tag">${esc(g)}</span>`).join('')}</div><h3>Claims and evidence</h3>${e.claim_ids.map(cid=>{const c=d.claims.find(c=>c.id===cid);return c?`<article class="claim-card">${badge(c.status)}<p>${esc(c.statement)}</p><button data-claim="${esc(cid)}">Inspect evidence →</button></article>`:'';}).join('')||'<p class="muted">No linked claim was emitted for this named entity.</p>'}<h3>Relationships</h3>${e.relationships.map(r=>`<p class="fine">${esc(d.entities.find(x=>x.id===r.subject)?.name)} → ${esc(r.predicate)} → ${esc(d.entities.find(x=>x.id===r.object)?.name)} <button data-claim="${esc(r.claim_id)}">Evidence</button></p>`).join('')||'<p class="muted">No structured relationships recorded.</p>'}`;
  } else {
    const c=d.claims.find(x=>x.id===id);if(!c)return;
    html=`<div class="eyebrow">CLAIM EVIDENCE · ${esc(c.id)}</div><h2>${esc(c.statement)}</h2>${badge(c.status)}<p>${esc(c.reason||'No semantic review has been recorded yet.')}</p><p class="fine">Evidence integrity: ${c.evidence_intact?'saved quotations checked':'not established / invalid'}. Claim support alone does not establish goal eligibility.</p><h3>Qualifications</h3><ul class="small-list">${c.qualifications.map(q=>`<li>${esc(q)}</li>`).join('')||'<li>None recorded; this is not a guarantee of certainty.</li>'}</ul><h3>Source evidence</h3>${c.evidence.map(e=>`<blockquote>${esc(e.quote)}</blockquote>${link(d.sources.find(s=>s.id===e.source_id)?.location)}<p class="fine">${esc(e.source_id)} · passage ${esc(e.passage_id)} · characters ${e.start}–${e.end}</p>`).join('')||'<p class="muted">No validated source span is available.</p>'}<h3>Structured result fields</h3>${c.results.map(r=>`<div class="claim-card"><strong>${esc(r.goal_id)}</strong>${r.fields.map(f=>`<p><span class="fine">${esc(f.name)}</span><br>${esc(f.value)}</p>`).join('')}</div>`).join('')||'<p class="muted">No structured item record.</p>'}`;
  }
  $('#drawer-content').innerHTML=html;$('#drawer').hidden=false;$('#drawer-shade').hidden=false;if(focus)$('#close-drawer').focus();
}
/** Poll active work while avoiding hidden-tab and idle-run background requests.
 *
 * Args:
 *   None. Reads pending-launch, worker-running, visibility and request-failure state.
 * Returns:
 *   Nothing. Replaces the refresh timer: queued work is immediate, active work
 *   waits 2.5 seconds, and failures back off to at most 60 seconds. Hidden tabs
 *   receive no timer. This function schedules but does not send the request.
 */
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if(document.hidden)return;
  const active=state.pendingJob||state.data?.status.worker_running;
  if(refreshQueued||refreshFailures||active) {
    const delay=refreshQueued?0:refreshFailures?Math.min(60000,5000*2**Math.min(refreshFailures-1,4)):2500;
    refreshTimer=setTimeout(refresh,delay);
  }
}
/** Fetch run navigation and the selected snapshot without overlapping refreshes.
 *
 * Args:
 *   None. Reads and updates browser state, including pending launches and run selection.
 * Returns:
 *   Promise resolving after rendering or handled failure. Busy calls queue a
 *   follow-up. A stale response cannot replace a newly selected run. Failures
 *   retain the last snapshot, show an error and schedule bounded retry backoff;
 *   normal polling pauses on idle or hidden tabs. Does not start research.
 */
async function refresh() {
  if(state.busy){refreshQueued=true;return;}
  clearTimeout(refreshTimer);refreshQueued=false;state.busy=true;
  try {
    const [runs,jobs]=await Promise.all([api('/api/runs'),api('/api/jobs')]);
    const job=jobs.find(j=>j.id===state.pendingJob);
    if(job?.run_id){state.run=job.run_id;location.hash=state.run;notice('');}
    else if(job?.error){notice(job.error);state.pendingJob=null;}
    if(!state.run&&runs.length)state.run=runs[0].id;
    $('#runs').innerHTML=runs.map(r=>`<button class="run-link ${r.id===state.run?'active':''}" data-run="${esc(r.id)}"><span>${esc(r.question)}</span><small>${new Date(r.created_at).toLocaleDateString()} · ${esc(r.status.replaceAll('_',' '))}</small></button>`).join('')||'<p class="muted">Your investigations will appear here.</p>';
    if(state.run){const selected=state.run;const view=await api('/api/runs/'+encodeURIComponent(selected));if(selected===state.run){announceDiscoveries(view);state.data=view;render();}}else{$('#empty').hidden=false;}
    // Keep a launch pending until its worker is observable or its process exits.
    // A run directory may appear before the worker acquires its monitoring lock.
    if(job?.run_id&&(state.data?.id===job.run_id&&state.data.status.worker_running||job.exit_code!=null))state.pendingJob=null;
    refreshFailures=0;
    $('#connection').textContent=state.pendingJob||state.data?.status.worker_running?'● Live · '+clock(Date.now()/1000):'● Idle · auto-refresh paused';
  } catch(e){refreshFailures++;$('#connection').textContent='Connection lost · retrying with backoff';notice(e.message);}
  finally{state.busy=false;scheduleRefresh();}
}
/** Read one browser-selected document into the dashboard upload payload.
 *
 * Args:
 *   file: Browser File selected by the user; size must not exceed 5 MiB.
 * Returns:
 *   Promise resolving to {name, content}, where content is base64 without a
 *   data-URL prefix. Oversized files and read errors reject. Reads locally only;
 *   the caller sends the payload later if research is submitted.
 */
function upload(file) {return new Promise((resolve,reject)=>{if(file.size>5*1024*1024)return reject(Error('Each document must be at most 5 MiB.'));const reader=new FileReader();reader.onload=()=>resolve({name:file.name,content:reader.result.split(',')[1]});reader.onerror=()=>reject(Error('Unable to read document.'));reader.readAsDataURL(file);});}
/** Submit a new research job using the visible form and selected documents.
 *
 * Args:
 *   event: Browser form-submit event; its default navigation is prevented.
 * Returns:
 *   Promise resolving after launch or a displayed form error. Converts minutes
 *   to seconds, reads uploads, POSTs the job and tracks its receipt until a run
 *   appears. Disables then restores the launch button. A successful submission
 *   authorizes the backend worker to spend the chosen research budget.
 */
async function startResearch(event) {
  event.preventDefault();$('#launch').disabled=true;$('#form-error').textContent='';
  try {const form=$('#new-form'),f=new FormData(form);const payload={question:f.get('question'),urls:f.get('urls'),model:f.get('model'),max_requests:Number(f.get('max_requests')),model_concurrency:Number(f.get('model_concurrency')),max_seconds:Number(f.get('minutes'))*60,explore_depth:Number(f.get('explore_depth')),files:await Promise.all([...$('#uploads').files].map(upload))};for(const k of ['search','follow_links','coverage_study','estimate_fallback','automatic_stopping','design_answer'])payload[k]=form.elements[k].checked;
    const job=await api('/api/jobs',payload);state.pendingJob=job.id;$('#new-dialog').close();notice('Starting your investigation…');await refresh();
  } catch(e){$('#form-error').textContent=e.message;}finally{$('#launch').disabled=false;}
}
/** Initialize this dashboard page and bind its navigation and research controls.
 *
 * Args:
 *   None. Uses bundled DOM elements, URL fragment and the same-origin bootstrap API.
 * Returns:
 *   Promise resolving after the first refresh. Saves the CSRF token in browser
 *   state, registers form/click/filter/visibility events and loads the selected
 *   run. A failed bootstrap displays its error and stops initialization. Research
 *   starts only from explicit form actions, not merely opening the page.
 */
async function init() {
  try {const boot=await api('/api/bootstrap');state.csrf=boot.csrf;$('#new-form').elements.model.value=boot.model;}catch(e){notice(e.message);return;}
  state.run=/^[a-zA-Z0-9_-]+$/.test(location.hash.slice(1))?location.hash.slice(1):null;
  document.addEventListener('click',async event=>{
    const b=event.target.closest('button');if(!b)return;
    if(b.id==='refresh')refresh();
    if(b.classList.contains('new-research')){$('#new-dialog').showModal();$('#new-form').elements.question.focus();}
    if(b.dataset.run)selectRun(b.dataset.run);
    if(b.dataset.tab){state.tab=b.dataset.tab;render(true);}
    if(b.dataset.entity)showDrawer('entity',b.dataset.entity);
    if(b.dataset.target){if(b.dataset.goal)state.targetGoal=b.dataset.goal;showDrawer('target',b.dataset.target);}
    if(b.dataset.targetPage){state.targetPage+=Number(b.dataset.targetPage);render(true);}
    if(b.dataset.claim)showDrawer('claim',b.dataset.claim);
    if(b.dataset.page){state.page+=Number(b.dataset.page);render(true);}
    if(b.dataset.claimPage){state.claimPage+=Number(b.dataset.claimPage);render(true);}
    if(b.id==='close-treasure'){$('#treasure').hidden=true;clearTimeout(treasureTimer);}
    if(b.id==='stop'){b.disabled=true;try{const r=await api(`/api/runs/${state.run}/stop`,{});notice(r.message);await refresh();}catch(e){notice(e.message);b.disabled=false;}}
    if(b.id==='reject-review'){try{await api(`/api/runs/${state.run}/review`,{review_id:state.data.pending_review.review_id,reject:true});notice('Plan rejection submitted.');await refresh();}catch(e){notice(e.message);}}
  });
  document.addEventListener('input',event=>{
    const e=event.target;const map={'entity-query':'query','claim-query':'claimQuery','target-query':'targetQuery'};
    if(map[e.id]){const start=e.selectionStart;state[map[e.id]]=e.value;state.page=state.claimPage=state.targetPage=0;render(true);const input=$('#'+e.id);input.focus();input.setSelectionRange(start,start);}
  });
  document.addEventListener('change',event=>{const map={'entity-type':'type','entity-support':'support','claim-status':'claimStatus','axis':'axis','target-goal':'targetGoal','target-status':'targetStatus'};if(map[event.target.id]){state[map[event.target.id]]=event.target.value;state.page=state.claimPage=state.targetPage=0;render(true);}});
  document.addEventListener('submit',async event=>{if(event.target.id!=='review-form')return;event.preventDefault();const answers={};event.target.querySelectorAll('[data-answer]').forEach(e=>answers[e.dataset.answer]=e.value);try{const job=await api(`/api/runs/${state.run}/review`,{review_id:state.data.pending_review.review_id,answers});state.pendingJob=job.id;notice('Clarification submitted; continuing research.');await refresh();}catch(e){$('#review-error').textContent=e.message;}});
  $('#new-form').addEventListener('submit',startResearch);$('#close-new').onclick=()=>$('#new-dialog').close();
  const close=()=>{state.drawer=null;$('#drawer').hidden=true;$('#drawer-shade').hidden=true;};$('#close-drawer').onclick=close;$('#drawer-shade').onclick=close;document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(refreshTimer);}else refresh();});
  window.addEventListener('focus',()=>{if(!document.hidden)refresh();});
  window.addEventListener('online',()=>{if(!document.hidden)refresh();});
  await refresh();
}
init();
