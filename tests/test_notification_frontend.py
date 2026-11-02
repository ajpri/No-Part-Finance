"""Notification device table and removal handlers use plain text and explicit actions."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import tests


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
