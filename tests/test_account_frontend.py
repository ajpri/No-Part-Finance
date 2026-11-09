"""Browser account forms, sessions, notification devices, and password recovery."""

import tests  # Enable the production-database guard before application imports.
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from flask import Flask, render_template_string

class AccountFrontendTests(unittest.TestCase):
    def runJavascript(self, harness):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js unavailable; set NODE_BINARY for frontend checks")
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        accountFunctions = "function renderMyAccount" + source.split("function renderMyAccount", 1)[1].split("function useSameTabNavigation", 1)[0]
        completed = subprocess.run([nodePath, "-e", harness + "\n" + accountFunctions + "\ncheck().catch(error => { console.error(error); process.exitCode = 1; });"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def testSettingsDefaultFragmentsRolesAndNoLedger(self):
        self.runJavascript(r'''
const assert = require("node:assert/strict");
let browserSession = {role: "owner", ledgerId: "test"};
let location = {hash: ""}, auditRequestSequence = 0, auditLoads = 0;
function loadAuditLog() { auditLoads++; }
const elements = new Map();
const document = {querySelector(selector) { if (!elements.has(selector)) elements.set(selector, {hidden: false, attributes: {}, setAttribute(key,value) {this.attributes[key]=value;}, removeAttribute(key) {delete this.attributes[key];}}); return elements.get(selector); }};
async function check() {
  showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  assert.equal(document.querySelector("#settingsAccounts").hidden, true);
  location.hash = "#accounts"; showSettingsSection();
  assert.equal(document.querySelector("#settingsAccounts").hidden, false);
  location.hash = "#audit-log"; showSettingsSection(); assert.equal(auditLoads, 1);
  location.hash = ""; showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  browserSession.role = "auditor"; location.hash = "#integrations"; showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  browserSession.ledgerId = null; location.hash = "#accounts"; showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  assert.equal(document.querySelector("#settingsAccounts").hidden, true);
}
''')

    def testAccountFormsPreserveErrorsAndResetPasswordOnSuccess(self):
        self.runJavascript(r'''
const assert = require("node:assert/strict");
let browserSession = {csrfToken: "old"};
const elements = new Map();
const document = {querySelector(selector) { if (!elements.has(selector)) elements.set(selector, {value: "", textContent: "", hidden: false, open: true, close(){this.open=false;}, replaceChildren(){}}); return elements.get(selector); }};
const account = {username: "<script>name</script>", firstName: "Test", lastName: "Person", allowAccountSelfService: true, csrfToken: "retained"};
let nextResult = account, nextError = null, submitted;
async function api(path, options) {if(["/api/account/sessions", "/api/account/notifications"].includes(path)) return []; submitted = {path, payload: JSON.parse(options.body)}; if(nextError) throw nextError; return nextResult;}
function showFormError(form,error) {form.error.textContent = error.message;}
function makeForm() { return {error: {textContent:""}, button: {disabled:false}, resetCount:0, querySelector(selector) {return selector === ".formError" ? this.error : this.button;}, reset(){this.resetCount++;}}; }
async function check() {
  renderMyAccount(account);
  assert.equal(document.querySelector("#accountUsername").textContent, "<script>name</script>");
  assert.equal(document.querySelector("#accountEditing").hidden, false);
  assert.equal(document.querySelector("#changeAccountNameButton").hidden, false);
  renderMyAccount({...account, allowAccountSelfService:false});
  assert.equal(document.querySelector("#accountEditing").hidden, true);
  assert.equal(document.querySelector("#changeAccountNameButton").hidden, true);
  const form = makeForm(), event = {preventDefault(){},currentTarget:form};
  document.querySelector("#accountFirstName").value = "Edited";
  nextError = new Error("Name rejected"); await saveAccountName(event);
  assert.equal(document.querySelector("#accountFirstName").value, "Edited");
  assert.equal(form.error.textContent,"Name rejected"); assert.equal(form.button.disabled,false);
  assert.equal(document.querySelector("#accountNameDialog").open, true);
  nextError = null; await saveAccountName(event);
  assert.equal(submitted.path,"/api/account/name");
  assert.deepEqual(submitted.payload,{firstName:"Edited",lastName:"Person"});
  assert.equal(browserSession.csrfToken,"retained");
  assert.equal(document.querySelector("#accountNameDialog").open, false);
  document.querySelector("#accountCurrentPassword").value = "  old Unicode α  ";
  document.querySelector("#accountNewPassword").value = "  next Unicode β  ";
  document.querySelector("#accountPasswordConfirmation").value = "  next Unicode β  ";
  nextError = new Error("Current password rejected"); await changeAccountPassword(event);
  assert.equal(form.resetCount,0); assert.equal(form.error.textContent,"Current password rejected");
  assert.equal(document.querySelector("#accountPasswordDialog").open, true);
  document.querySelector("#accountFirstName").value = "Unsaved name";
  nextError = null; await changeAccountPassword(event);
  assert.equal(document.querySelector("#accountFirstName").value, "Unsaved name");
  assert.equal(form.resetCount,1); assert.equal(form.button.disabled,false);
  assert.equal(document.querySelector("#accountPasswordDialog").open, false);
  assert.deepEqual(submitted.payload,{currentPassword:"  old Unicode α  ",newPassword:"  next Unicode β  ",confirmation:"  next Unicode β  "});
}
''')

    def testAccountDialogOpeningPrefillsNamesAndClearsPasswordDrafts(self):
        self.runJavascript(r'''
const assert = require("node:assert/strict");
let browserSession = {allowAccountSelfService:true, firstName:"Current", lastName:"Name"};
const elements = new Map();
const document = {querySelector(selector) {if (!elements.has(selector)) elements.set(selector, {value:"draft", textContent:"old", open:false, showModal(){this.open=true;}, error:{textContent:"old"}, querySelector(){return this.error;}, reset(){this.resetCount=(this.resetCount||0)+1;}}); return elements.get(selector);}};
function openDialog(dialog,form){form.querySelector(".formError").textContent="";dialog.showModal();}
async function check(){
  openAccountNameDialog();
  assert.equal(document.querySelector("#accountNameDialog").open,true);
  assert.equal(document.querySelector("#accountFirstName").value,"Current");
  assert.equal(document.querySelector("#accountLastName").value,"Name");
  assert.equal(document.querySelector("#accountNameForm").error.textContent,"");
  document.querySelector("#accountFirstName").value="Cancelled draft";
  openAccountNameDialog();
  assert.equal(document.querySelector("#accountFirstName").value,"Current");
  openAccountPasswordDialog();
  assert.equal(document.querySelector("#accountPasswordForm").resetCount,1);
  assert.equal(document.querySelector("#accountPasswordDialog").open,true);
  browserSession.allowAccountSelfService=false;
  openAccountPasswordDialog();openAccountNameDialog();
  assert.equal(document.querySelector("#accountPasswordForm").resetCount,1);
  assert.equal(document.querySelector("#accountNameForm").resetCount,2);
}
''')

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

class NotificationFrontendTests(unittest.TestCase):
    def testNotificationDevicesRemovalAndStaleRefresh(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Set NODE_BINARY for browser-handler tests")
        source = Path("app.js").read_text()
        functions = "let accountNotificationsRequestSequence" + source.split("let accountNotificationsRequestSequence", 1)[1].split("async function loadMyAccount", 1)[0]
        harness = r'''
const assert=require("node:assert/strict");
let confirmationAccepted=true;const confirmations=[];function confirm(message){confirmations.push(message);return confirmationAccepted;}
let browserSession={ledgerId:null},pushRefreshes=0,nextError;
const timers=new Map();let timerId=0;
function setTimeout(callback,delay){timers.set(++timerId,{callback,delay});return timerId;}
function clearTimeout(id){timers.delete(id);}
async function refreshPushSettings(){pushRefreshes++;}
const rows=[];
function element(){return {textContent:"",disabled:false,isConnected:true,children:[],dataset:{},append(...children){this.children.push(...children);},reset(){this.resetCount=(this.resetCount||0)+1;},close(){this.closed=true;},addEventListener(name,callback){this[name]=callback;}};}
const controls={"#accountNotificationRows":{replaceChildren(){rows.length=0;},insertRow(){const row={cells:[],insertCell(){const cell=element();this.cells.push(cell);return cell;}};rows.push(row);return row;}}};
const document={querySelector(key){return controls[key] ||= element();},createElement:element};
let requests=[],enrollments=[{id:"safe-id",client:"<script>Client</script>",ledgerName:"<script>Ledger</script>",label:"<script>Tablet</script>",current:true,sessionStatus:"signed-in"}];
async function api(path,options){requests.push({path,options});if(nextError)throw nextError;return path.endsWith("disable")?{status:"ok"}:enrollments;}
async function check(){
 await loadAccountNotifications();
 assert.equal(rows.length,1);assert.equal(rows[0].cells[0].textContent,"<script>Tablet</script> — This device");
 assert.equal(rows[0].cells[1].textContent,"<script>Ledger</script>");
 assert.equal(rows[0].cells[2].textContent,"Signed in");
 assert.equal(rows[0].className,"currentAccountRow");
 assert.equal(rows[0].cells[0].children[0].textContent,"<script>Client</script>");
 const button=rows[0].cells[3].children[0];assert.equal(button.dataset.viewControl,"true");
 assert.equal(button.className,"destructiveButton");
 confirmationAccepted=false;const beforeCancel=requests.length;
 await disableAccountNotifications({id:"safe-id"},button,{client:"Safari",ledgerName:"Household"});
 assert.equal(requests.length,beforeCancel);assert.equal(button.disabled,false);
 assert.match(confirmations.at(-1),/Safari.*Household/);
 confirmationAccepted=true;
 enrollments=[];await disableAccountNotifications({id:"safe-id"},button);
 assert.deepEqual(JSON.parse(requests[1].options.body),{id:"safe-id"});
 assert.equal(pushRefreshes,0);assert.equal(controls["#disableAllAccountNotifications"].disabled,true);
 browserSession.ledgerId="ledger";await disableAccountNotifications({all:true},controls["#disableAllAccountNotifications"]);
 assert.match(confirmations.at(-1),/all your devices across all ledgers/);
 assert.deepEqual(JSON.parse(requests[3].options.body),{all:true});assert.equal(pushRefreshes,1);
 assert.equal(controls["#disableAllAccountNotifications"].disabled,true);
 assert.equal(controls["#accountNotificationsStatus"].textContent,"All notifications disabled.");
 nextError=new Error("Removal failed");await disableAccountNotifications({id:"safe-id"},button);
 assert.equal(controls["#accountNotificationsError"].textContent,"Removal failed");assert.equal(button.disabled,false);
 nextError=null;
 const form=controls["#notificationLabelForm"]=element();form.error=element();form.button=element();form.querySelector=(selector)=>selector===".formError"?form.error:form.button;
 openDialog=(dialog,selected)=>{selected.error.textContent="";dialog.closed=false;};
 showFormError=(selected,error)=>{selected.error.textContent=error.message;};
 openNotificationLabelDialog({id:7,label:"My tablet"});
 assert.equal(controls["#notificationDeviceLabel"].value,"My tablet");
 nextError=new Error("Label rejected");await saveNotificationLabel({preventDefault(){},currentTarget:form});
 assert.equal(form.error.textContent,"Label rejected");assert.equal(controls["#notificationLabelDialog"].closed,false);
 assert.equal(controls["#notificationDeviceLabel"].value,"My tablet");
 nextError=null;await saveNotificationLabel({preventDefault(){},currentTarget:form});
 assert.equal(controls["#notificationLabelDialog"].closed,true);assert.equal(form.button.disabled,false);
 assert.equal(controls["#accountNotificationsStatus"].textContent,"Device label saved.");
 const firstTimer=notificationLabelStatusTimer;assert.equal(timers.get(firstTimer).delay,5000);
 await saveNotificationLabel({preventDefault(){},currentTarget:form});
 assert.equal(timers.has(firstTimer),false);assert.equal(timers.size,1);
 timers.get(notificationLabelStatusTimer).callback();assert.equal(controls["#accountNotificationsStatus"].textContent,"");
 await saveNotificationLabel({preventDefault(){},currentTarget:form});
 controls["#accountNotificationsStatus"].textContent="Notifications disabled.";
 controls["#accountNotificationsError"].textContent="Keep this error";
 timers.get(notificationLabelStatusTimer).callback();
 assert.equal(controls["#accountNotificationsStatus"].textContent,"Notifications disabled.");
 assert.equal(controls["#accountNotificationsError"].textContent,"Keep this error");
 enrollments=[{client:"Old browser",sessionStatus:"no-active-session"},{client:"Legacy browser",sessionStatus:"unknown"},{client:"Invalid status",sessionStatus:"<script>"},{client:"Current browser",sessionStatus:"no-active-session",current:true}];
 await loadAccountNotifications();
 assert.equal(rows[0].className,undefined);assert.equal(rows[1].className,undefined);assert.equal(rows[2].className,undefined);assert.equal(rows[3].className,"currentAccountRow");
 assert.equal(rows[0].cells[2].textContent,"No active session");assert.equal(rows[1].cells[2].textContent,"Unknown");assert.equal(rows[2].cells[2].textContent,"Unknown");
 const pending=[];api=()=>new Promise(resolve=>pending.push(resolve));
 const old=loadAccountNotifications(),latest=loadAccountNotifications();
 pending[1]([]);await latest;pending[0]([{id:"removed",client:"Old",ledgerName:"Old"}]);await old;
 assert.equal(rows.length,0);
}
'''
        result = subprocess.run([nodePath, "-e", harness + functions + "\ncheck().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def testCurrentLedgerEnrollmentAndTableRefresh(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Set NODE_BINARY for browser-handler tests")
        source = Path("app.js").read_text()
        functions = "async function refreshPushSettings" + source.split("async function refreshPushSettings", 1)[1].split('if ("serviceWorker" in navigator) navigator.serviceWorker.addEventListener', 1)[0]
        harness = r'''
const assert=require("node:assert/strict");
const browserSession={role:"member",ledgerId:"one",ledgers:[{id:"one",label:"<Household>"}]};
const OfflineFinance={policy:{pushNotificationsEnabled:true}};
const controls={};const document={querySelector(key){return controls[key] ||= {hidden:false,disabled:false,textContent:"",addEventListener(event,callback){this[event]=callback;}};}};
let enrolled=false,tableRefreshes=0,permissionRequests=0,notificationPermissionCheckPromise=null;
async function reconcileNotificationPermission(){}
const subscription={endpoint:"https://push.example/device",toJSON(){return {keys:{}};}};
const registration={pushManager:{async getSubscription(){return subscription;}}};
const window={isSecureContext:true,PushManager:{},Notification:{}};
const navigator={serviceWorker:{}};
const Notification={permission:"granted",async requestPermission(){permissionRequests++;return "granted";}};
async function applicationWorker(){return registration;}
async function loadAccountNotifications(){tableRefreshes++;}
async function api(path,options){assert.equal(path,"/api/push");if(options?.method==="POST")enrolled=true;return {endpoints:enrolled?[subscription.endpoint]:[],publicKey:"YWJj"};}
async function check(){
 await refreshPushSettings();
 assert.equal(controls["#pushLedgerName"].textContent,"<Household>");
 assert.equal(controls["#enablePushButton"].hidden,false);
 assert.equal(permissionRequests,0);
 let finishCleanup;notificationPermissionCheckPromise=new Promise(resolve=>finishCleanup=resolve);
 const enabling=controls["#enablePushButton"].click();
 await Promise.resolve();await Promise.resolve();assert.equal(enrolled,false);
 finishCleanup();await enabling;notificationPermissionCheckPromise=null;
 assert.equal(permissionRequests,1);assert.equal(tableRefreshes,1);
 assert.equal(controls["#enablePushButton"].hidden,true);
 assert.equal(controls["#pushPermissionStatus"].textContent,"Enabled for this ledger.");
 assert.equal(controls["#disablePushButton"],undefined);
 enrolled=false;await refreshPushSettings();assert.equal(controls["#enablePushButton"].hidden,false);
 OfflineFinance.policy.pushNotificationsEnabled=false;await refreshPushSettings();assert.equal(controls["#enablePushButton"].hidden,true);
}
'''
        result = subprocess.run([nodePath, "-e", harness + functions + "\ncheck().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def testPermissionRevocationCleanupGuardsRetryAndCoalescing(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Set NODE_BINARY for browser-handler tests")
        source = Path("app.js").read_text()
        functions = "let notificationPermissionCheckPromise" + source.split("let notificationPermissionCheckPromise", 1)[1].split("async function refreshPushSettings", 1)[0]
        harness = r'''
const assert=require("node:assert/strict");
let browserSession={userId:9,role:"auditor",ledgerId:null},offlineMode=false,currentPath="/settings";
const navigator={onLine:true,serviceWorker:{}};const window={isSecureContext:true,Notification:{},PushManager:{}};
const Notification={permission:"granted",requestPermission(){throw new Error("Must not prompt");}};
const controls={};const document={visibilityState:"visible",querySelector(key){return controls[key] ||= {textContent:""};}};
let requests=[],tableRefreshes=0,statusRefreshes=0,nextError,release;
async function api(path,options){requests.push({path,options});if(nextError)throw nextError;if(release)await release;return {status:"ok"};}
async function loadAccountNotifications(){tableRefreshes++;}
async function refreshPushSettings(){statusRefreshes++;}
async function check(){
 await reconcileNotificationPermission();assert.equal(requests.length,0);
 Notification.permission="denied";await reconcileNotificationPermission();
 assert.equal(requests.length,1);assert.equal(requests[0].path,"/api/account/notifications/disable");
 assert.deepEqual(JSON.parse(requests[0].options.body),{currentDevice:true});assert.equal(requests[0].options.background,true);
 assert.equal(tableRefreshes,1);assert.equal(statusRefreshes,1);
 Notification.permission="default";await reconcileNotificationPermission();assert.equal(requests.length,2);
 for(const state of ["offline","disconnected","hidden","insecure","unsupported","no-session","unknown"]){
   offlineMode=state==="offline";navigator.onLine=state!=="disconnected";document.visibilityState=state==="hidden"?"hidden":"visible";
   window.isSecureContext=state!=="insecure";if(state==="unsupported")delete window.Notification;else window.Notification={};
   browserSession=state==="no-session"?null:{userId:9};Notification.permission=state==="unknown"?undefined:"denied";
   await reconcileNotificationPermission();assert.equal(requests.length,2,state);
 }
 Notification.permission="denied";nextError=new Error("Offline request");await reconcileNotificationPermission();
 assert.match(controls["#accountNotificationsError"].textContent,/retry/);assert.equal(tableRefreshes,2);
 nextError=null;await reconcileNotificationPermission();assert.equal(tableRefreshes,3);
 let resume;release=new Promise(resolve=>resume=resolve);
 const first=reconcileNotificationPermission(),second=reconcileNotificationPermission();
 assert.equal(requests.length,5);resume();await Promise.all([first,second]);assert.equal(tableRefreshes,4);release=null;
 currentPath="/transactions";await reconcileNotificationPermission();assert.equal(tableRefreshes,4);assert.equal(requests.length,6);
}
'''
        result = subprocess.run([nodePath, "-e", harness + functions + "\ncheck().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

class RecoveryFrontendTests(unittest.TestCase):
    def render(self, mode, recoveryEnabled=True, fields=None, message=""):
        application = Flask(__name__)
        with application.app_context():
            return render_template_string(Path("index.html").read_text(), mode=mode, title="Recovery test", csrfToken="synthetic-csrf", registrationEnabled=False, recoveryEnabled=recoveryEnabled, version="26.11.10", fields=fields or {}, message=message)

    def testAllAuthenticationModesExcludeApplicationContent(self):
        for mode in ("signIn", "register", "profile", "verify", "welcome", "forgotPassword", "resetPassword", "recoveryComplete", "unconfigured"):
            with self.subTest(mode=mode):
                html = self.render(mode, message="<script>unsafe message</script>")
                self.assertIn('src="/authentication.js"', html)
                self.assertIn('class="signIn"', html)
                self.assertIn("No Part Finance 26.11.10", html)
                self.assertNotIn('src="/app.js', html)
                self.assertNotIn('id="dashboardPage"', html)
                self.assertNotIn("<script>unsafe message</script>", html)
                for marker in ("{{", "{%", "__APPLICATION_VERSION__"):
                    self.assertNotIn(marker, html)

    def testRecoveryLinkPolicyAndNativeFormFields(self):
        self.assertNotIn('href="/forgot-password"', self.render("signIn", False))
        self.assertIn('href="/forgot-password"', self.render("signIn"))
        forgot = self.render("forgotPassword", fields={"username": "<script>username</script>"}, message="If eligible, a link will be sent.")
        self.assertIn('method="post" action="/forgot-password"', forgot)
        self.assertIn('name="csrfToken" value="synthetic-csrf"', forgot)
        self.assertIn('name="username" type="text" inputmode="email"', forgot)
        self.assertIn("&lt;script&gt;username&lt;/script&gt;", forgot)
        self.assertNotIn("<script>username</script>", forgot)
        self.assertIn("If eligible, a link will be sent.", forgot)
        reset = self.render("resetPassword", fields={"token": "a" * 43}, message="Passwords do not match.")
        self.assertIn('method="post" action="/reset-password"', reset)
        self.assertIn('name="token" value="' + "a" * 43 + '"', reset)
        self.assertIn('name="newPassword" type="password"', reset)
        self.assertIn('name="confirmation" type="password"', reset)
        self.assertIn("Passwords do not match.", reset)
        complete = self.render("recoveryComplete", message="Password reset. Sign in again.")
        self.assertIn('href="/sign-in"', complete)
        self.assertNotIn("<form", complete)

    def testResetFragmentTransferRemovalAndNoTokenRefresh(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Set NODE_BINARY for reset fragment handler checks")
        source = Path("authentication.js").read_text()
        function = "function preparePasswordReset" + source.split("function preparePasswordReset", 1)[1].split("preparePasswordReset();", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
let form, location, replaced;
const document={querySelector(){return form;}};
const history={replaceState(state,title,path){replaced=path;location.hash="";}};
function reset(fragment,postedToken=""){
 location={hash:fragment,pathname:"/reset-password",search:""};replaced=null;
 const token={value:postedToken},message={textContent:"",hidden:true},submit={disabled:false};
 form={querySelector(key){return {"[data-reset-token]":token,"[data-reset-token-message]":message,"[data-reset-submit]":submit}[key];},addEventListener(key,callback){this[key]=callback;}};
 preparePasswordReset();return {token,message,submit};
}
async function check(){
 const secret="A".repeat(42)+"_";
 let controls=reset("#"+secret);
 assert.equal(controls.token.value,secret);assert.equal(replaced,"/reset-password");
 assert.equal(location.hash,"");assert.equal(controls.message.hidden,true);assert.equal(controls.submit.disabled,false);
 let prevented=false;form.submit({preventDefault(){prevented=true;}});assert.equal(prevented,false);
 controls=reset("");assert.equal(controls.token.value,"");assert.equal(controls.submit.disabled,true);assert.equal(controls.message.hidden,false);
 assert.equal(replaced,null,"Refresh cannot reconstruct the removed secret");
 controls=reset("",secret);assert.equal(controls.token.value,secret);assert.equal(controls.submit.disabled,false,"Validation retry retains server-posted hidden token");
 for(const invalid of ["short","A".repeat(44),"A".repeat(42)+"%", "<script>"+"A".repeat(35)]){
  controls=reset("#"+invalid);assert.equal(controls.token.value,"");assert.equal(controls.submit.disabled,true);assert.equal(replaced,"/reset-password");
 }
 controls=reset("#"+secret);controls.token.value="changed";prevented=false;
 form.submit({preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(controls.submit.disabled,true);
 form=null;preparePasswordReset();
}
'''
        result = subprocess.run([nodePath, "-e", harness + function + "\ncheck().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
