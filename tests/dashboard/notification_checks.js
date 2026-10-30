/** Check support-notification transitions using the production tracker in Node.js.
 * Handwritten snapshots and a Map-backed storage substitute exercise first load,
 * repeated findings, run switches, unavailable storage and report-entry support.
 * Assertions throw on incorrect notifications. No browser, server or model runs.
 */
'use strict';
const assert=require('node:assert/strict');
const {DiscoveryTracker}=require('../../projectclarity/dashboard/static/discoveries.js');
const memory=new Map();
const storage={getItem:key=>memory.get(key),setItem:(key,value)=>memory.set(key,value)};
/** Construct the minimal claim state used in notification-transition assertions.
 * Args:
 *   id: Stable fixture claim identifier.
 *   status: Saved review status to simulate, such as pending or supported.
 *   intact: Whether the simulated source-integrity check passed; defaults to true.
 * Returns:
 *   A fresh fixture claim. The helper does not actually validate its evidence.
 */
function claim(id,status,intact=true){return {id,status,evidence_intact:intact};}
/** Feed one claim-only snapshot to the real tracker and extract notification IDs.
 * Args:
 *   tracker: Production DiscoveryTracker instance holding notification history.
 *   claims: Fixture claim objects representing the latest saved view.
 *   run: Run identifier for switch tests; defaults to run1.
 * Returns:
 *   IDs newly announced by the tracker. Updates the tracker's notification state
 *   through its normal observe method; it does not save research findings.
 */
function observe(tracker,claims,run='run1'){return tracker.observe({id:run,claims}).map(c=>c.id);}
const tracker=new DiscoveryTracker(storage);
assert.deepEqual(observe(tracker,[claim('old','supported'),claim('new','pending')]),[]);
assert.deepEqual(observe(tracker,[claim('old','supported'),claim('new','supported')]),['new']);
assert.deepEqual(observe(tracker,[claim('old','supported'),claim('new','supported')]),[]);
assert.deepEqual(observe(tracker,[claim('new','contradicted'),claim('bad','supported',false)]),[]);
assert.deepEqual(observe(tracker,[claim('new','supported'),claim('bad','supported',false)]),[]);
// A new support judgment counts even if the total stays unchanged after a retraction.
assert.deepEqual(observe(tracker,[claim('new','contradicted'),claim('replacement','supported')]),['replacement']);
assert.deepEqual(observe(tracker,[claim('historical','supported')],'run2'),[]);
assert.deepEqual(observe(tracker,[claim('old','supported'),claim('new','supported')],'run1'),[]);
const reloaded=new DiscoveryTracker(storage);
assert.deepEqual(observe(reloaded,[claim('old','supported'),claim('new','supported')]),[]);
assert.deepEqual(observe(reloaded,[claim('later','supported'),claim('bad','needs_review')]),['later']);
const unavailable=new DiscoveryTracker({getItem(){throw Error('Disabled');},setItem(){throw Error('Full');}});
assert.deepEqual(observe(unavailable,[]),[]);
assert.deepEqual(observe(unavailable,[claim('safe','supported')]),['safe']);
console.log('Notification transition checks passed.');

// A designed report celebrates a completed entry, not another supported claim.
const reportTracker=new DiscoveryTracker();
const report={id:'report-run',answer_schema:{sections:[{goal_id:'G1'}]},claims:[],targets:[{goal_id:'G1',rows:[{id:'R1',label:'One requested item',status:'incomplete'}]}]};
assert.deepEqual(reportTracker.observe(report),[]);
report.claims=[claim('unrelated','supported')];
assert.deepEqual(reportTracker.observe(report),[]);
report.targets[0].rows[0].status='supported';
assert.equal(reportTracker.observe(report)[0].result_id,'R1');
report.claims.push(claim('more_evidence','supported'));
assert.deepEqual(reportTracker.observe(report),[]);
console.log('Report-entry notification checks passed.');
