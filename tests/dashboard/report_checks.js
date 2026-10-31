/** Check the production dashboard renderers against a Python-supplied saved-run view.
 * The calling unittest sends JSON on stdin. Node's VM evaluates the real rendering
 * functions with minimal browser stubs and assertions inspect their output HTML.
 * Missing field values, unsafe markup and lost unassigned evidence fail the check.
 * This does not open a browser or visually inspect layout and interaction.
 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
// Execute the production renderer on the Python test's shared-inventory projection.
// Input: JSON dashboard view on stdin. Result: throws on missing values, unsafe
// markup, duplicate report rows or lost unassigned evidence; starts no UI/network.
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const nodes=new Map();
const context=vm.createContext({data, console, DiscoveryTracker:class{},
  document:{querySelector:selector=>{if(!nodes.has(selector))nodes.set(selector,{focus(){}});return nodes.get(selector);}}, window:{}, location:{hash:''}});
const source=fs.readFileSync('projectclarity/dashboard/static/app.js','utf8');
vm.runInContext(source.replace(/init\(\);\s*$/,''),context);
const html=vm.runInContext('reportPanel(data)',context);
assert.match(html,/Alpha; Beta/);
assert.match(html,/Unassigned evidence \(1 observations, not report entries\)/);
assert.match(html,/&lt;unassigned&gt;/);
assert.doesNotMatch(html,/<unassigned>/);
assert.match(html,/data-claim="C3"/);
assert.equal((html.match(/data-target=/g)||[]).length,1);
assert.equal((html.match(/<tbody><tr>/g)||[]).length,1);

// The other consumers must not silently fall back to a scalar representative.
const targets=vm.runInContext('targetsPanel(data)',context);
assert.match(targets,/Alpha \/ Beta/);
assert.match(targets,/0 incomplete records · 1 unassigned observations not counted/);
vm.runInContext("state.targetQuery='Beta'",context);
assert.match(vm.runInContext('targetsPanel(data)',context),/data-target=/);
vm.runInContext("state.data=data;showDrawer('target',data.targets[0].rows[0].id)",context);
assert.match(nodes.get('#drawer-content').innerHTML,/Alpha; Beta/);
assert.match(nodes.get('#drawer-content').innerHTML,/data-claim="C1"/);
assert.match(nodes.get('#drawer-content').innerHTML,/data-claim="C2"/);
assert.match(vm.runInContext('resultCard(data)',context),/0 incomplete records · 1 unassigned observations/);

// Compact population counts preserve estimates and never borrow another goal's scope.
vm.runInContext(`
  const compactFixture={targets:[],scope_summary:[{goal_ids:['G1'],lower:8,upper:200}],claims:[]};
  const compactGoal={goal_id:'G1',identified:11,measurement:{target:null}};
`,context);
assert.match(vm.runInContext('compactGoalProgress(compactFixture,compactGoal)',context),/11 \/ ~8–200/);
vm.runInContext('compactFixture.scope_summary[0].lower=300;compactFixture.scope_summary[0].upper=300',context);
assert.match(vm.runInContext('compactGoalProgress(compactFixture,compactGoal)',context),/11 \/ ~300/);
vm.runInContext("compactFixture.scope_summary[0].goal_ids=['G2']",context);
assert.match(vm.runInContext('compactGoalProgress(compactFixture,compactGoal)',context),/11 \/ \?/);
vm.runInContext('compactGoal.identified=0;compactGoal.measurement.target=0',context);
assert.match(vm.runInContext('compactGoalProgress(compactFixture,compactGoal)',context),/0 \/ 0/);
// Findings stay first even when the completion ledger contains blockers.
vm.runInContext("data.result_progress.mapping_ledger={rows:[{blocking:true,status:'pending',goal_id:'G1',field_names:[],rationale:'Repair pending',candidate_id:'C1'}]}",context);
const blockedReport=vm.runInContext('reportPanel(data)',context);
assert.ok(blockedReport.indexOf('<table>')<blockedReport.indexOf('Unresolved claim-to-report mappings'));

// Inclusion uncertainty stays distinct from supported field values, with escaped reasons.
vm.runInContext(`
  const qualifiedRecord=data.targets[0].rows[0].records[0];
  qualifiedRecord.field_support_status='supported';
  qualifiedRecord.qualification_status='unknown';
  qualifiedRecord.qualification_requirements=[{description:'Current affiliation',status:'unknown',assessments:[{candidate_id:'C1',rationale:'No <dated> evidence.'}]}];
`,context);
const qualificationHTML=vm.runInContext('reportPanel(data)',context);
assert.match(qualificationHTML,/Inclusion checks: unknown/);
assert.match(qualificationHTML,/Field support: supported/);
assert.match(qualificationHTML,/Current affiliation/);
assert.match(qualificationHTML,/No &lt;dated&gt; evidence/);
assert.doesNotMatch(qualificationHTML,/<dated>/);
vm.runInContext("state.data=data;showDrawer('target',data.targets[0].rows[0].id)",context);
assert.match(nodes.get('#drawer-content').innerHTML,/Inclusion checks: unknown/);

// A flat qualification count must not conceal growing factual support, nor may
// historical field-only counts be relabelled as eligibility approvals.
vm.runInContext("const supportFixture={field_supported:5,supported:0,qualification_policy:'record-scope-and-currentness-v2'}",context);
const supportStats=vm.runInContext('targetSupportStats(supportFixture)',context);
assert.equal(supportStats[0][0],'Field-supported targets');
assert.equal(supportStats[0][1],'5');
assert.equal(supportStats[1][0],'Qualified targets');
assert.equal(supportStats[1][1],'0');
vm.runInContext("supportFixture.qualification_policy='legacy_field_support'",context);
assert.equal(vm.runInContext('targetSupportStats(supportFixture)[1][1]',context),'—');

// The field-support legend must use the same blue as its plotted series.
vm.runInContext("data.history=[];data.metrics=data.metrics||{}",context);
const targetChartHTML=vm.runInContext('targetChart(data)',context);
assert.match(targetChartHTML,/<span class="field-support">Field-supported targets<\/span>/);
assert.match(fs.readFileSync('projectclarity/dashboard/static/style.css','utf8'),/\.legend \.field-support:before\{background:#3568ac\}/);
