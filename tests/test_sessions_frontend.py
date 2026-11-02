"""Session management and automatic-request activity checks without a database."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import tests


class SessionsFrontendTests(unittest.TestCase):
    def javascript(self, source, harness):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Set NODE_BINARY for JavaScript handler tests")
        result = subprocess.run([nodePath, "-e", harness + source + "\ncheck().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def testSessionTableAndRevocation(self):
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        functions = "function accountSessionTime" + source.split("function accountSessionTime", 1)[1].split("let accountNotificationsRequestSequence", 1)[0]
        self.javascript(functions, r'''
const assert=require("node:assert/strict");
let confirmationAccepted=true;const confirmations=[];function confirm(message){confirmations.push(message);return confirmationAccepted;}
let browserSession={userId:9}, cleared, destination, nextError, accountSessionsRequestSequence=0;
const location={assign(path){destination=path;}};
const OfflineFinance={async clear(id){cleared=id;}};
async function loadAccountNotifications(){}
const rows=[];
function element(){return {textContent:"",disabled:false,isConnected:true,children:[],dataset:{},append(child){this.children.push(child);},addEventListener(name,callback){this[name]=callback;}};}
const controls={"#accountSessionRows":{replaceChildren(){rows.length=0;},insertRow(){const row={cells:[],insertCell(){const cell=element();this.cells.push(cell);return cell;}};rows.push(row);return row;}}};
const document={querySelector(key){return controls[key] ||= element();},createElement:element};
let requests=[],result={status:"ok",loggedOut:false};
async function api(path,options){requests.push({path,options});if(nextError)throw nextError;return path.endsWith("logout")?result:[{id:"safe-id",client:"<script>Client</script>",lastIp:"127.0.0.1",signedInAt:2000000000,lastActiveAt:2000000001,current:true},{id:"second",client:"Unknown client",lastIp:null,current:false}];}
async function check(){
 await loadAccountSessions();
 assert.equal(rows.length,2);assert.equal(rows[0].cells[0].textContent,"<script>Client</script> — This session");
 assert.equal(rows[1].cells[1].textContent,"Unknown");
 assert.equal(rows[0].className,"currentAccountRow");assert.equal(rows[1].className,undefined);
 const button=rows[1].cells[4].children[0];assert.equal(button.dataset.viewControl,"true");
 assert.equal(button.className,"destructiveButton");
 confirmationAccepted=false;const beforeCancel=requests.length;
 await logoutAccountSessions({id:"second"},button,{client:"Firefox"});
 assert.equal(requests.length,beforeCancel);assert.equal(button.disabled,false);
 assert.match(confirmations.at(-1),/Firefox/);
 confirmationAccepted=true;
 await logoutAccountSessions({id:"second"},button);
 assert.deepEqual(JSON.parse(requests[1].options.body),{id:"second"});
 assert.equal(destination,undefined);assert.equal(controls["#accountSessionsStatus"].textContent,"Session logged out.");
 await logoutAccountSessions({others:true},button);
 assert.match(confirmations.at(-1),/all your other sessions/);
 assert.deepEqual(JSON.parse(requests[3].options.body),{others:true});
 nextError=new Error("Revocation failed");await logoutAccountSessions({id:"safe-id"},button);
 assert.equal(controls["#accountSessionsError"].textContent,"Revocation failed");assert.equal(button.disabled,false);
 nextError=null;result.loggedOut=true;await logoutAccountSessions({id:"safe-id"},button);
 assert.equal(cleared,9);assert.equal(destination,"/sign-in");
 const pending=[];
 api=()=>new Promise(resolve=>pending.push(resolve));
 const older=loadAccountSessions(),newer=loadAccountSessions();
 pending[1]([]);await newer;
 pending[0]([{id:"revoked",client:"Old",current:false}]);await older;
 assert.equal(rows.length,0,"Late refresh must not reappear after newer state");
}
''')

    def testBackgroundOptionIsExplicitAndDoesNotLeak(self):
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        function = "async function api" + source.split("async function api", 1)[1].split("function ", 1)[0]
        self.javascript(function, r'''
const assert=require("node:assert/strict");
let confirmationAccepted=true;const confirmations=[];function confirm(message){confirmations.push(message);return confirmationAccepted;}
let browserSession={csrfToken:"csrf",ledgerId:"ledger"},offlineMode=false,offlineRecovery=false,lifecycleReady=false;
const location={href:"http://localhost/"};
const requests=[];
async function fetch(path,options){requests.push(options);return {ok:true,json:async()=>({})};}
async function check(){
 await api("/api/calendar",{background:true});
 await api("/api/account/name",{method:"PUT",body:"{}"});
 assert.equal(requests[0].headers["X-Background-Request"],"true");
 assert.equal(requests[1].headers["X-Background-Request"],undefined);
 assert.equal(requests[0].background,undefined);
 assert.equal(requests[0].headers["X-CSRF-Token"],"csrf");
}
''')
