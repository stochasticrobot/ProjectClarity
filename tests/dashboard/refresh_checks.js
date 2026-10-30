/** Check dashboard refresh timing and launch tracking without a live browser.
 * Node's VM executes app.js with controlled fetch responses, timers and DOM stubs.
 * Assertions cover idle/hidden-tab pauses, request overlap and retry recovery.
 * Rendering is replaced, so this is not a visual or complete browser test.
 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

/** Exercise production refresh orchestration with deterministic HTTP and timer boundaries.
 * Args: none.
 * Returns: promise; throws on idle polling, lost launch tracking, overlap or recovery errors.
 * Rendering is stubbed; no server, research worker or model request is started.
 */
async function check() {
  const timers=new Map(), nodes=new Map(), events={};let timerId=0,calls=0,fail=false;
  let running=false,jobs=[],hold=null;
  const context=vm.createContext({console,Date,URL,DiscoveryTracker:class{},
    location:{hash:''},window:{addEventListener:(name,fn)=>{events[name]=fn;}},
    document:{hidden:false,addEventListener:(name,fn)=>{events[name]=fn;},
      querySelector:selector=>{if(!nodes.has(selector))nodes.set(selector,{elements:{model:{}},addEventListener(){}});return nodes.get(selector);}},
    setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},
    clearTimeout:id=>timers.delete(id),
    fetch:async url=>{calls++;if(hold)await hold;if(fail)throw Error('Offline');
      const body=url==='/api/bootstrap'?{csrf:'test',model:'test'}:url==='/api/jobs'?jobs:
        url==='/api/runs'?[{id:'r1',question:'Test',created_at:0,status:'finished'}]:
        {id:url.split('/').at(-1),status:{worker_running:running}};
      return {ok:true,json:async()=>body};}
  });
  const source=fs.readFileSync(path.join(__dirname,'../../projectclarity/dashboard/static/app.js'),'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/,''),context);
  vm.runInContext('render=()=>{};announceDiscoveries=()=>{};',context);
  await vm.runInContext('init()',context);
  assert.equal(calls,4);assert.equal(timers.size,0,'Finished run must not poll');
  assert.match(nodes.get('#connection').textContent,/Idle/);
  running=true;
  await vm.runInContext('refresh()',context);
  assert.equal([...timers.values()][0].ms,2500);
  running=false;
  await vm.runInContext('refresh()',context);
  assert.equal(timers.size,0,'Final snapshot must cancel polling');
  jobs=[{id:'j1',run_id:'r1',exit_code:null}];
  vm.runInContext("state.pendingJob='j1'",context);
  await vm.runInContext('refresh()',context);
  assert.equal(vm.runInContext('state.pendingJob',context),'j1','Early run directory must not end launch tracking');
  running=true;
  await vm.runInContext('refresh()',context);
  assert.equal(vm.runInContext('state.pendingJob',context),null);
  context.document.hidden=true;events.visibilitychange();
  assert.equal(timers.size,0,'Hidden tabs must pause');
  await vm.runInContext('refresh()',context);
  assert.equal(timers.size,0,'An in-flight response must not restart hidden polling');
  context.document.hidden=false;await events.focus();
  // Event callbacks intentionally do not return the asynchronous refresh promise.
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal([...timers.values()][0].ms,2500);
  fail=true;
  await vm.runInContext('refresh()',context);
  assert.equal([...timers.values()][0].ms,5000);
  await vm.runInContext('refresh()',context);
  assert.equal([...timers.values()][0].ms,10000);
  fail=false;running=false;
  await vm.runInContext('refresh()',context);
  assert.equal(timers.size,0,'Recovery to idle must stop retrying');
  let release;hold=new Promise(resolve=>{release=resolve;});
  const first=vm.runInContext('refresh()',context),before=calls;
  await vm.runInContext('refresh()',context);
  assert.equal(calls,before,'Concurrent refresh must not duplicate requests');
  vm.runInContext("state.run='r2'",context);hold=null;release();await first;
  assert.equal(vm.runInContext('state.data.id',context),'r2');
  assert.equal([...timers.values()][0].ms,0,'An explicit request during refresh must not be lost');
  await vm.runInContext('refresh()',context);
  assert.equal(timers.size,0);
  jobs=[{id:'j2',run_id:null,exit_code:1,error:'Launch failed'}];
  vm.runInContext("state.pendingJob='j2'",context);
  await vm.runInContext('refresh()',context);
  assert.equal(timers.size,0,'Failed launch must not poll forever');
  console.log('Dashboard refresh checks passed: idle, active, launch, hidden, recovery and coalescing.');
}
check().catch(error=>{console.error(error);process.exitCode=1;});
