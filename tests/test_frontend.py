"""Focused behavior checks for asynchronous browser controls using optional Node.js."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

import tests  # Activates the database guard before any application imports.


class FrontendAsyncTests(unittest.TestCase):
    def testTransferHelperIgnoresOutdatedResponses(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js is unavailable; asynchronous frontend behavior check requires a JavaScript runtime")
        javascript = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        helperFunction = "async function refreshTransferAmountHelper()" + javascript.split(
            "async function refreshTransferAmountHelper()", 1
        )[1].split("async function editTransfer", 1)[0]
        counterDeclaration = next((line for line in javascript.splitlines() if line.startswith("let transferHelperRequest =")), "")
        harness = r"""
const assert = require("node:assert/strict");
const controls = {
  "#transferFromAccount": {value: "1"},
  "#transferToAccount": {value: "2"},
  "#transferId": {value: ""},
  "#transferDialog": {open: true},
  "#transferAmount": {value: "unchanged"},
  "#transferForm": {error: ""},
  "#transferPaymentHint": {hidden: true, children: [], append(child) { this.children.push(child); }},
};
const requests = [];
const document = {
  querySelector(selector) { return controls[selector]; },
  createElement() { return {setAttribute() {}, addEventListener(event, callback) { this.click = callback; }}; },
};
function api(path) { return new Promise((resolve, reject) => requests.push({path, resolve, reject})); }
function showFormError(form, error) { form.error = error.message; }
function setMoneyInputValue(input, value) { input.value = value; }
function clearHint() { controls["#transferPaymentHint"].children = []; controls["#transferPaymentHint"].hidden = true; }
const eligible = (amountCents) => ({eligible: true, label: `Use ${amountCents}`, amountCents});
async function check() {
  const oldRequest = refreshTransferAmountHelper();
  controls["#transferToAccount"].value = "3";
  clearHint();
  const latestRequest = refreshTransferAmountHelper();
  requests[1].resolve(eligible(2500));
  await latestRequest;
  requests[0].resolve(eligible(9900));
  await oldRequest;
  assert.equal(controls["#transferPaymentHint"].children.length, 1, "late response must not append an old helper");
  const latestButton = controls["#transferPaymentHint"].children[0];
  assert.equal(latestButton.textContent, "Use 2500");
  assert.equal(controls["#transferAmount"].value, "unchanged", "response must not set the amount automatically");
  latestButton.click();
  assert.equal(controls["#transferAmount"].value, "25.00");

  clearHint();
  const beforeBlank = refreshTransferAmountHelper();
  controls["#transferFromAccount"].value = "";
  await refreshTransferAmountHelper();
  requests[2].resolve(eligible(9900));
  await beforeBlank;
  assert.equal(controls["#transferPaymentHint"].children.length, 0, "blank selection invalidates pending request");

  controls["#transferFromAccount"].value = "1";
  const beforeClose = refreshTransferAmountHelper();
  controls["#transferDialog"].open = false;
  requests[3].resolve(eligible(9900));
  await beforeClose;
  assert.equal(controls["#transferPaymentHint"].children.length, 0, "closed dialog must not receive helper");

  controls["#transferDialog"].open = true;
  const beforeEditing = refreshTransferAmountHelper();
  controls["#transferId"].value = "10";
  requests[4].resolve(eligible(9900));
  await beforeEditing;
  assert.equal(controls["#transferPaymentHint"].children.length, 0, "create response cannot enter transfer editing");

  const ineligibleRequest = refreshTransferAmountHelper();
  requests[5].resolve({eligible: false});
  await ineligibleRequest;
  assert.equal(controls["#transferPaymentHint"].children.length, 0);

  controls["#transferId"].value = "";
  const beforeReopen = refreshTransferAmountHelper();
  controls["#transferDialog"].open = false;
  controls["#transferDialog"].open = true;
  clearHint();
  const afterReopen = refreshTransferAmountHelper();
  requests[7].resolve(eligible(4000));
  await afterReopen;
  requests[6].resolve(eligible(9900));
  await beforeReopen;
  assert.equal(controls["#transferPaymentHint"].children.length, 1, "same-pair request from prior dialog must be ignored");
  assert.equal(controls["#transferPaymentHint"].children[0].textContent, "Use 4000");

  clearHint();
  const outdatedFailure = refreshTransferAmountHelper();
  const currentFailure = refreshTransferAmountHelper();
  requests[8].reject(new Error("Outdated request failed"));
  await outdatedFailure;
  assert.equal(controls["#transferForm"].error, "", "stale failures must not overwrite the current form");
  requests[9].reject(new Error("Helper request failed"));
  await currentFailure;
  assert.equal(controls["#transferForm"].error, "Helper request failed", "current failures remain visible for correction");
  assert.equal(controls["#transferPaymentHint"].children.length, 0);
}
check().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run([nodePath, "-e", counterDeclaration + "\n" + helperFunction + "\n" + harness], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def testAuditPagingIgnoresStaleResponsesAndUsesSafeText(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js is unavailable; audit pagination requires a JavaScript runtime")
        javascript = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        auditFunctions = "let auditRequestSequence =" + javascript.split("let auditRequestSequence =", 1)[1].split("function showSettingsSection", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
const requests = [];
let location = new URL("http://localhost/settings?page=1&perPage=10#audit-log");
const history = {replaceState(_,__,url) { location = new URL(url); }, pushState(_,__,url) { location = new URL(url); }};
function element() { return {textContent:"", disabled:false, value:"10", children:[], append(...items) { this.children.push(...items); }, replaceChildren() { this.children=[]; }, insertRow() { const row={cells:[], insertCell() { const cell=element(); this.cells.push(cell); return cell; }}; this.children.push(row); return row; }}; }
const controls = Object.fromEntries(["auditError", "auditRows", "auditPerPage", "auditPrevious", "auditNext", "auditRange"].map(key=>["#"+key,element()]));
const document = {querySelector(selector) { return controls[selector]; }, createElement:element, createTextNode(text) { return {textContent:text}; }};
function api(path) { return new Promise((resolve,reject)=>requests.push({path,resolve,reject})); }
function friendlyCalendarDate(value) { return "formatted " + value; }
async function check() {
  const old = loadAuditLog();
  location = new URL("http://localhost/settings?page=2&perPage=10#audit-log");
  const latest = loadAuditLog();
  requests[1].resolve({page:2, perPage:10, total:25,start:11,end:20,hasPrevious:true,hasNext:true, rows:[{dateTime:"recorded time",user:"<script>user</script>",action:"Updated",type:"Transaction",oldValues:null,newValues:[{label:"Description",value:"<script>text</script>"}]}]});
  await latest;
  requests[0].resolve({page:1, rows:[],start:0,end:0,total:0,hasPrevious:false,hasNext:false});
  await old;
  assert.equal(controls["#auditRange"].textContent,"11–20 of 25");
  assert.equal(controls["#auditRows"].children.length,1);
  const row=controls["#auditRows"].children[0];
  assert.equal(row.cells[1].textContent,"<script>user</script>");
  assert.equal(row.cells[5].children[0].children[1].textContent,"<script>text</script>");
  const stale = loadAuditLog();
  location = new URL("http://localhost/settings#accounts");
  requests[2].resolve({page:1,rows:[],start:0,end:0,total:0});
  await stale;
  assert.equal(controls["#auditRange"].textContent,"11–20 of 25");
  location = new URL("http://localhost/settings?page=2&perPage=10#audit-log");
  controls["#auditPerPage"].value="100";
  navigateAuditPage(1);
  assert.equal(location.search,"?page=1&perPage=100");
  assert.equal(location.hash,"#audit-log");
  requests[3].resolve({page:1,rows:[],start:0,end:0,total:0,hasPrevious:false,hasNext:false});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(controls["#auditPrevious"].disabled,true);
  assert.equal(controls["#auditNext"].disabled,true);
}
'''
        result = subprocess.run([nodePath, "-e", harness + auditFunctions + "\ncheck().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testAuditTimestampUsesFriendlyDateWithoutChangingServerTime(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js is unavailable; timestamp formatting requires a JavaScript runtime")
        javascript = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        friendlyDate = "function friendlyCalendarDate" + javascript.split("function friendlyCalendarDate", 1)[1].split("function setMoneyInputValue", 1)[0]
        auditDate = "function friendlyAuditTimestamp" + javascript.split("function friendlyAuditTimestamp", 1)[1].split("function renderAuditValues", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
const year = new Date().getFullYear();
assert.equal(friendlyAuditTimestamp(`${year}-01-02 00:03:04 CST`), "Jan 2 00:03:04 CST");
assert.equal(friendlyAuditTimestamp(`${year - 1}-12-31 23:59:59 CST`), `Dec 31, ${year - 1} 23:59:59 CST`);
assert.equal(friendlyAuditTimestamp("Not recorded"), "Not recorded");
'''
        for timezone in ("America/Chicago", "Pacific/Kiritimati", "Pacific/Honolulu"):
            result = subprocess.run([nodePath, "-e", friendlyDate + auditDate + harness], capture_output=True, text=True, env={**os.environ, "TZ": timezone})
            self.assertEqual(0, result.returncode, result.stderr)

    def testCachedDashboardBalancesStayScopedAndRefreshWithoutOfflineNavigation(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js unavailable")
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        functions = source[source.index("function dashboardRefreshLabel("):source.index("function renderDashboard(dashboard)")]
        harness = r'''
const assert = require("node:assert/strict");
let browserSession={userId:7,ledgerId:"one"},currentPath="/",offlineMode=false,lastDashboard=null,lastDashboardQuery=null,cachedBalanceSnapshot=null,dashboardRequestId=0,dashboardCurrentDate=null;
const controls=new Map();
const document={body:{dataset:{}},querySelector(selector){return this.getElementById(selector.slice(1));},getElementById(id){if(!controls.has(id))controls.set(id,{textContent:"",hidden:false,replaceChildren(){this.textContent="";}});return controls.get(id);}};
let snapshots=[],readError=false,renderedBalances=null,renderedDashboard=null,saved=0,resolveRead;
const OfflineFinance={async available(userId){assert.equal(userId,7);if(readError)throw Error("storage denied");return {snapshots};}};
function offlineUpdatedLabel(){return "Updated 2:35 PM";}
function renderDashboardAccounts(value){renderedBalances=value;document.getElementById("cashTotal").textContent="cached";}
function renderDashboard(value){renderedDashboard=value;document.getElementById("cashTotal").textContent="fresh";}
function dashboardQuery(){return "period=thisMonth";}
async function cacheDashboard(){saved++;}
let requests=[];
function api(){return new Promise((resolve,reject)=>requests.push({resolve,reject}));}
const own={userId:7,ledgerId:"one",savedAt:"2026-11-02T14:35:00Z",dashboard:{accounts:[{balanceCents:12345}]},controls:{dashboardPeriod:{value:"lastMonth"}}};
async function check(){
 snapshots=[{...own,userId:8},{...own,ledgerId:"other"}];await showCachedDashboardBalances();assert.equal(renderedBalances,null);
 readError=true;await showCachedDashboardBalances();assert.equal(renderedBalances,null);readError=false;
 snapshots=[own];await showCachedDashboardBalances();assert.equal(renderedBalances,own.dashboard);assert.equal(renderedDashboard,null,"Do not render saved reporting totals or restore reporting controls");
 assert.equal(document.body.dataset.cachedBalances,"true");assert.equal(document.body.dataset.offline,undefined);
 assert.equal(document.querySelector("#dashboardRefreshMessage").textContent,"Updating… · Last updated 2:35 PM");assert.equal(controls.has("mainNavigation"),false);
 let loading=loadDashboard();assert.match(document.querySelector("#dashboardRefreshMessage").textContent,/Last updated/);
 requests.at(-1).reject(Error("server error"));await loading;assert.equal(document.body.dataset.cachedBalances,"true");assert.match(document.querySelector("#dashboardRefreshMessage").textContent,/Couldn't refresh.*Last updated/);assert.equal(document.querySelector("#dashboardRefreshRetry").hidden,false);
 loading=loadDashboard();const fresh={accounts:[{balanceCents:999}],currentDate:"2026-11-02"};requests.at(-1).resolve(fresh);await loading;
 assert.equal(renderedDashboard,fresh);assert.equal(lastDashboard,fresh);assert.equal(cachedBalanceSnapshot,null);assert.equal(document.body.dataset.cachedBalances,"false");assert.equal(document.querySelector("#dashboardRefreshMessage").textContent,"");assert.equal(saved,1);
 // A slow cache read may not overwrite a completed fresh request or a different ledger.
 lastDashboard=null;OfflineFinance.available=()=>new Promise(resolve=>{resolveRead=resolve;});const delayed=showCachedDashboardBalances();browserSession={userId:7,ledgerId:"other"};resolveRead({snapshots:[own]});await delayed;assert.equal(document.getElementById("cashTotal").textContent,"fresh");
 browserSession.ledgerId="one";const delayedFresh=showCachedDashboardBalances();lastDashboard=fresh;resolveRead({snapshots:[own]});await delayedFresh;assert.equal(cachedBalanceSnapshot,null);
 // Clearing only the provisional balances never erases fresh results.
 clearCachedDashboardBalances();assert.equal(document.getElementById("cashTotal").textContent,"fresh");
 lastDashboard=null;OfflineFinance.available=async()=>({snapshots:[own]});await showCachedDashboardBalances();clearCachedDashboardBalances();assert.equal(document.getElementById("cashTotal").textContent,"");assert.equal(document.body.dataset.cachedBalances,"false");assert.equal(document.getElementById("minimumBalanceBanner").hidden,true);
 currentPath="/settings";await showCachedDashboardBalances();assert.equal(cachedBalanceSnapshot,null);
 browserSession.ledgerId=null;currentPath="/";await showCachedDashboardBalances();assert.equal(cachedBalanceSnapshot,null);
}
'''
        result = subprocess.run([nodePath, "-e", harness + functions + "check().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def testDeniedDashboardRequestClearsProvisionalBalances(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js unavailable")
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        apiFunction = source[source.index("async function api("):source.index("function option(")]
        harness = r'''
const assert=require("node:assert/strict");
let browserSession={userId:7,ledgerId:"one"},offlineMode=false,offlineRecovery=false,lifecycleReady=true,cleared=0,redirected=0,status=403;
const location={href:"http://localhost/?ledger=one",assign(){redirected++;}};
const OfflineFinance={async clear(userId,ledgerId){assert.equal(userId,7);assert.equal(ledgerId,status===401?null:"one");}};
function clearCachedDashboardBalances(){cleared++;}
async function fetch(){return {ok:false,status,async json(){return {error:"Access denied"};}};}
async function check(){for(const code of [403,401]){status=code;await assert.rejects(api("/api/dashboard"),/Access denied/);}assert.equal(cleared,2);assert.equal(redirected,1);}
'''
        result = subprocess.run([nodePath, "-e", harness + apiFunction + "check().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
