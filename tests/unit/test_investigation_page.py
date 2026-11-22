"""Exercise investigation-page transitions without research, browsers or providers."""
import shutil
import subprocess
from pathlib import Path
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node required for browser-code checks')
class InvestigationPageTests(unittest.TestCase):
    """Keep partial-result publication from stranding the investigation page."""

    def test_terminal_progress_keeps_polling_until_owner_finishes(self):
        """Run actual page JavaScript across the terminal-progress transition.

        Args: None.
        Returns: None; asserts continued polling, hidden premature Resume, visible
            Continue after transition, and saved concurrency on explicit Resume.
            DOM/network are fixtures; no real action is sent to the application.
        """
        script=Path(__file__).resolve().parents[2]/'src/projectclarity/ux/scripts/claim_investigation.js'
        program=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();
function node(){return {hidden:false,disabled:false,value:'all',textContent:'',
 append(){},after(){},replaceChildren(){},setAttribute(){},addEventListener(){},
 querySelectorAll(){return []},classList:{add(){},remove(){}}};}
function get(id){if(!elements.has(id))elements.set(id,node());return elements.get(id);}
let state={session_id:'fixture',status:'investigation_running',research_question:'Question',
 investigation_concurrency:4,triage:{status:'completed'},run:{status:'partial'},selection:{status:'completed'},
 investigation:{status:'partial',artifact_directory:'saved-attempt',claims_total:5,claims_finished:5,failed:2}};
let timer=null,posted=null;
const sandbox={document:{getElementById:get,createElement:node},
 location:{pathname:'/investigation/fixture',search:'?stay=1',replace(){},assign(){}},
 URLSearchParams,renderStages(){},showActivityIssue(){},followCurrentRound(){return false;},
 clearTimeout(){timer=null;},setTimeout(callback){timer=callback;return 1;},
 fetch:async(path,options)=>{if(options?.method==='POST')posted=JSON.parse(options.body);
 return {ok:true,json:async()=>path.endsWith('/investigation-records')?{records:[],report_facts:[]}:state};}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
setImmediate(async()=>{try{
 assert.equal(typeof timer,'function','must poll beyond terminal progress');
 assert.equal(get('prepare-report').hidden,true);
 // The controls are built dynamically, so observe their visibility via render state.
 assert.equal(get('resume-investigation-controls').hidden,true);
 state={...state,status:'investigation_partial'};
 await sandbox.refresh();
 assert.equal(get('prepare-report').hidden,false);
 assert.equal(get('resume-investigation-controls').hidden,false);
 assert.equal(timer,null);
 await sandbox.resumeInvestigation();
 assert.equal(posted.concurrency,4);
 assert.equal(posted.previous_artifact,'saved-attempt');
 assert(!elements.has('resume-investigation-limit'));
}catch(error){console.error(error);process.exitCode=1;}});
"""
        result=subprocess.run(['node','-e',program,str(script)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
