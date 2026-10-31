/** Exercise production graph UI logic with real headless Cytoscape and inert DOM controls.
 * Canvas clicking is separately checked in a headless browser against saved runs.
 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const cytoscape=require('../../projectclarity/dashboard/static/vendor/cytoscape.min.js');
const graphData=JSON.parse(fs.readFileSync(0,'utf8')),controls=new Map();
/** Supply a stable control for production filter and inspector updates.
 * Args: id: Fixed DOM control identifier.
 * Returns: Mutable inert control; it cannot execute inserted HTML.
 */
function control(id){if(!controls.has(id))controls.set(id,{value:'',innerHTML:'',textContent:'',hidden:false});return controls.get(id);}
const context=vm.createContext({URLSearchParams,location:{search:'?run=test'},console,
 document:{getElementById:control},fetch:async()=>({ok:true,json:async()=>graphData}),
 cytoscape:options=>cytoscape({...JSON.parse(JSON.stringify(options)),headless:true,styleEnabled:true,container:undefined})});
const code=fs.readFileSync('projectclarity/dashboard/static/graph.js','utf8').replace(/startExplorer\(\);\s*$/,'');
vm.runInContext(code,context);
/** Verify preserved discoveries, safe rendering and reversible view filtering.
 * Args: None; uses the real Python graph supplied on stdin.
 * Returns: Promise; failed assertions fail the process without external network calls.
 */
async function check(){
 await vm.runInContext('loadGraph()',context);
 assert.equal(control('graph-warning').textContent,'');
 for(const [node,expected] of [
   [{kind:'person'},'person'],[{kind:'current House of Commons member',fields:[{name:'member_name',value:'Someone'}]},'person'],
   [{kind:'researcher'},'person'],[{kind:'organization'},'institution'],[{kind:'research_lab'},'institution'],
   [{kind:'electoral_district'},'place'],[{kind:'city'},'place'],[{kind:'statement'},'statement'],
   [{kind:'research_topic'},'other'],[{kind:'unknown',label:'University of Somewhere'},'other']]){
   context.visualNode=node;assert.equal(vm.runInContext('nodeCategory(visualNode)',context),expected);
 }
 assert.match(control('node-legend').innerHTML,/Institution \/ organization/);
 assert.match(control('node-legend').innerHTML,/Place \/ district/);
 assert(vm.runInContext("cy.nodes().every(n=>n.data('icon').startsWith('data:image/svg+xml')&&n.style('background-color'))",context));
 assert.equal(vm.runInContext('cy.nodes().length',context),graphData.nodes.length);
 assert.match(control('node-list').innerHTML,/&lt;incidental topic&gt;/);
 control('search').value='Alice';vm.runInContext('applyFilters()',context);
 assert(vm.runInContext("cy.nodes(':visible').length",context)<graphData.nodes.length);
 const alice=graphData.nodes.find(n=>n.label==='Alice'&&n.origin==='extraction');
 context.nodeId=alice.id;vm.runInContext('selectNode(nodeId)',context);
 assert.match(control('inspector').innerHTML,/Alice/);
 assert.match(control('inspector').innerHTML,/WORKS_FOR/);
 vm.runInContext(`globalThis.layouts=[];cy.on('layoutstart',e=>layouts.push({
   name:e.layout.options.name,ids:e.layout.options.eles.map(item=>item.id())}));`,context);
 control('focus-node').onclick();
 assert.equal(vm.runInContext('layouts.length',context),1);
 const visibleIds=JSON.parse(vm.runInContext("JSON.stringify(cy.elements(':visible').map(item=>item.id()).sort())",context));
 const layoutIds=JSON.parse(vm.runInContext('JSON.stringify(layouts[0].ids.sort())',context));
 assert.deepEqual(layoutIds,visibleIds);
 assert(layoutIds.length<graphData.nodes.length+graphData.edges.length);
 assert.equal(vm.runInContext('layouts[0].name',context),'concentric');
 assert.equal(vm.runInContext("cy.edges().filter(e=>e.style('label')!=='').length",context),0);
 assert.equal(vm.runInContext("cy.nodes().filter(n=>n.style('label')!=='').length",context),1);
 control('all-labels').checked=true;vm.runInContext('applyLabels()',context);
 assert(vm.runInContext("cy.nodes().every(n=>n.style('label')===n.data('label'))",context));
 control('all-labels').checked=false;vm.runInContext('applyLabels()',context);
 control('layout-mode').value='hierarchical';vm.runInContext('arrange()',context);
 assert.equal(vm.runInContext('layouts[1].name',context),'breadthfirst');
 assert.equal(control('search').value,'');
 assert(vm.runInContext("cy.nodes(':visible').length",context)>=2);

 const beforeExpansion=vm.runInContext("JSON.stringify(cy.nodes(':visible').map(n=>n.id()).sort())",context);
 const neighbor=vm.runInContext("cy.nodes(':visible').filter(n=>n.id()!==selected)[0].id()",context);
 context.neighborId=neighbor;
 vm.runInContext("selectNode(neighborId);cy.zoom(1.7);cy.pan({x:17,y:23});",context);
 const beforeViewport=vm.runInContext('JSON.stringify({zoom:cy.zoom(),pan:cy.pan()})',context);
 vm.runInContext("exploreNode(neighborId,'expand')",context);
 assert(vm.runInContext("cy.nodes(':visible').length",context)>JSON.parse(beforeExpansion).length);
 vm.runInContext('backView()',context);
 assert.equal(vm.runInContext("JSON.stringify(cy.nodes(':visible').map(n=>n.id()).sort())",context),beforeExpansion);
 assert.equal(vm.runInContext('JSON.stringify({zoom:cy.zoom(),pan:cy.pan()})',context),beforeViewport);
 vm.runInContext("exploreNode(neighborId,'expand');exploreNode(neighborId,'collapse')",context);
 assert.equal(vm.runInContext("JSON.stringify(cy.nodes(':visible').map(n=>n.id()).sort())",context),beforeExpansion);
 control('relationship').value=JSON.stringify(['relationship','WORKS_FOR']);vm.runInContext('applyFilters()',context);
 assert(vm.runInContext("cy.edges(':visible').length>0&&cy.edges(':visible').every(e=>e.data('label')==='WORKS_FOR')",context));
 control('relationship').value='';vm.runInContext('applyFilters()',context);
 const association=graphData.edges.find(e=>e.category==='record_association');context.edgeId=association.id;
 vm.runInContext('selectEdge(edgeId)',context);
 assert.match(control('inspector').innerHTML,/not a separately extracted relationship/);
 assert.equal(vm.runInContext("cy.edges().filter(e=>e.style('label')!=='').length",context),1);
 assert(!vm.runInContext("sourceLink('javascript:alert(1)')",context).includes('<a'));
 vm.runInContext('cy.destroy()',context);
}
check().catch(error=>{console.error(error);process.exit(1);});
