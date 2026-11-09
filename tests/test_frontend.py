"""Executable browser checks for financial controls, reporting, and themes."""

import tests  # Enable the production-database guard before application imports.
import os
import shutil
import subprocess
import unittest
from pathlib import Path
import html
import json
import re

projectRoot = Path(__file__).resolve().parents[1]

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
        friendlyDate = "function friendlyCalendarDate" + javascript.split("function friendlyCalendarDate", 1)[1].split("function submittedMoneyValue", 1)[0]
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

class ApplicationFrontendTests(unittest.TestCase):
    def testAccountBalanceDisplaysStayConsistentAcrossCallers(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "app.js").read_text(encoding="utf-8")
        functions = "\n".join(javascript[javascript.index(start):javascript.index(end)] for start, end in (
            ("function dollars(", "function friendlyCalendarDate("),
            ("function friendlyAccountBalance(", "async function loadSimulator("),
            ("function renderSimulatorResults(", "function openSimulatorPlanDialog("),
            ("function updateTransferPresentation(", "async function refreshTransferAmountHelper("),
            ("function renderDashboardAccounts(", "function renderDashboardCategories("),
            ("function setReconciliationBalance(", "async function saveReconciliation("),
        ))
        harness = r'''
const assert = require("node:assert/strict");
function element(){return {children:[],textContent:"",checked:false,value:"",selectedOptions:[],classList:{add(){}},replaceChildren(){this.children=[];},append(...children){this.children.push(...children);},insertRow(){const row=element();row.insertCell=()=>{const cell=element();row.children.push(cell);return cell;};this.children.push(row);return row;}};}
const controls={};
const document={querySelector:selector=>controls[selector]??=element(),createElement:()=>element()};
// Explicit expected strings preserve Cash signs, Credit debt/overpayment wording,
// thousands separators, zero balances, and projected amounts independent of current balances.
const cases=[
 ["Cash positive","cash",null,123456,-5000,"$1,234.56","-$50.00","-$51.00","$1,234.56"],
 ["Cash zero","cash",null,0,0,"$0.00","$0.00","-$1.00","$0.00"],
 ["Cash negative","cash",null,-123456,300,"-$1,234.56","$3.00","$2.00","-$1,234.56"],
 ["Card debt","credit","Credit Card",-123456,-100,"$1,234.56","$1.00","$1.00","$1,234.56 owed"],
 ["Card zero","credit","Credit Card",0,0,"$0.00","$0.00","$0.00","$0.00 owed"],
 ["Card overpaid","credit","Credit Card",123456,1000,"$1,234.56 credit","$10.00 credit","$10.00 credit","$1,234.56 credit"],
 ["Loan debt","credit","Personal Loan",-6543,-8000,"$65.43","$80.00","$80.00","$65.43 owed"],
 ["Other overpaid","credit","Other",42,42,"$0.42 credit","$0.42 credit","$0.42 credit","$0.42 credit"]
];
const accounts=cases.map(([name,accountClass,accountSubtype,balanceCents],index)=>Object.freeze({id:index+1,name,accountClass,accountSubtype,balanceCents}));
accounts.push(Object.freeze({id:99,name:"External",accountClass:"system",balanceCents:-100}));
const originalAccounts=JSON.stringify(accounts);
let notification,referenceLoads=0;
async function loadReferenceData(){referenceLoads++;}
function sendNotification(message){notification=message;}
function refreshTransferAmountHelper(){}
const simulator={plan:{totals:{startingCashCents:0,plannedIncomeCents:0,plannedSpendingCents:0,transfersPaymentsCents:0,unallocatedMoneyCents:0},results:cases.map((row,index)=>({...accounts[index],active:true,projectedBalanceCents:row[4],bufferCents:100}))}};
async function check(){
 for(const [index,row] of cases.entries()){
  const account=accounts[index];
  assert.equal(friendlyAccountBalance(account),row[5]);
  document.querySelector("#transferFromAccount").value=String(account.id);
  document.querySelector("#transferToAccount").value=String(account.id);
  document.querySelector("#transferToAccount").selectedOptions=[{dataset:{accountClass:account.accountClass}}];
  document.querySelector("#transferDescription").value="Saved custom description";
  updateTransferPresentation(false);
  for(const selector of ["#transferFromBalance","#transferToBalance"]){assert.equal(controls[selector].hidden,false);assert.equal(controls[selector].textContent,`Current Balance: ${row[5]}`);}
  assert.equal(controls["#transferDescription"].value,"Saved custom description");
  setReconciliationBalance(account);
  assert.equal(controls["#reconciliationCurrentBalance"].textContent,row[8]);
  assert.equal(controls["#reconciliationHint"].textContent,account.accountClass==="credit"?"Enter the actual amount currently owed as a positive number.":"Enter the actual current balance.");
 }
 for(const id of ["99",""]){
  controls["#transferFromAccount"].value=id;controls["#transferToAccount"].value=id;
  controls["#transferToAccount"].selectedOptions=id?[{dataset:{accountClass:"system"}}]:[];
  updateTransferPresentation(false);
  assert.equal(controls["#transferFromBalance"].hidden,true);assert.equal(controls["#transferToBalance"].hidden,true);
 }
 const dashboard={accounts:accounts.filter(account=>account.accountClass!=="system"),minimumBalanceAlerts:[],creditUtilizationAlerts:[],totalCreditBalanceCents:6501,totalUtilization:null};
 for(const hideZero of [false,true]){
  document.querySelector("#hideZeroBalances").checked=hideZero;renderDashboardAccounts(dashboard);
  const displayed=[...controls["#cashAccountRows"].children,...controls["#creditAccountRows"].children].map(row=>[row.children[0].textContent,row.children[1].textContent]);
  assert.deepEqual(displayed.sort(),cases.filter(row=>!hideZero||row[3]!==0).map(row=>[row[0],row[5]]).sort());
  assert.equal(controls["#cashTotal"].textContent,"$0.00");assert.equal(controls["#totalCreditBalance"].textContent,"$65.01");
 }
 for(const excludeBuffer of [false,true]){
  document.querySelector("#excludeBufferFromProjectedBalances").checked=excludeBuffer;renderSimulatorResults();
  assert.deepEqual(controls["#simulatorResultRows"].children.map(row=>row.children[1].textContent),cases.map(row=>row[excludeBuffer?7:6]));
 }
 await ledgerNotification("Saved",accounts.map(account=>account.id));
 assert.equal(notification,["Saved",...cases.map(row=>`${row[0]}: ${row[5]}`)].join("\n"));assert.equal(referenceLoads,1);
 assert.equal(JSON.stringify(accounts),originalAccounts,"Formatting must never change account balances or records");
}
'''
        result = subprocess.run([nodePath, "-e", functions + harness + "check().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def testDashboardEditorsUsePythonAmountsAndPreserveSavePayloads(self):
        from app import combinedRecentEvents
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "app.js").read_text(encoding="utf-8")
        functions = "\n".join(javascript[javascript.index(start):javascript.index(end)] for start, end in (
            ("function dollars(", "function friendlyCalendarDate("),
            ("function editOrdinaryTransaction(", "async function removeEditedTransaction("),
            ("function renderRecentTransactions(", "function dashboardQuery("),
        ))
        ordinaryCases = [
            ("Expense", "expense", -1173, "11.73"),
            ("Refund", "expense", 1727, "-17.27"),
            ("Income", "income", 151966, "1519.66"),
            ("Reversal", "income", -5000, "-50.00"),
            ("Uncategorized negative", None, -123, "-1.23"),
            ("Uncategorized positive", None, 456, "4.56"),
        ]
        rows = []
        expectedAmounts = {}
        for accountId in (1, 2):
            for pinned in (False, True):
                for description, categoryRole, amountCents, enteredAmount in ordinaryCases:
                    transactionId = len(rows) + 1
                    rows.append({"id": transactionId, "date": "2024-02-29", "accountId": accountId,
                                 "accountName": "Archived Cash" if accountId == 1 else "Archived Credit",
                                 "description": description, "categoryId": {"expense": 3, "income": 4}.get(categoryRole),
                                 "categoryName": {"expense": "Income-shaped name", "income": "Expense-shaped name"}.get(categoryRole),
                                 "categoryRole": categoryRole, "amountCents": amountCents, "pinned": pinned, "transferId": None})
                    expectedAmounts[transactionId] = enteredAmount
        events = combinedRecentEvents(rows, limit=len(rows))
        harness = r'''
const assert = require("node:assert/strict");
function element(){return {children:[],dataset:{},value:"",checked:false,hidden:false,append(...children){this.children.push(...children);},replaceChildren(){this.children=[];},insertCell(){const cell=element();this.children.push(cell);return cell;},insertRow(){return this.insertCell();},addEventListener(event,callback){this[event]=callback;},reset(){this.dataset={};},close(){this.open=false;}};}
const controls={};
const document={querySelector:selector=>controls[selector]??=element(),createElement:()=>element()};
let browserSession={role:"owner"},simulatorRecordingRowId=null,currentPath="/",refreshes=0;
const requests=[],transferEdits=[];
function friendlyCalendarDate(value){return value;}
function activeAccountsWith(id){return [id];}
function selectableCategoriesWith(id){return [id];}
function activeTemplates(){return [];}
function setAccountOptions(control,options,placeholder,id){control.value=String(id);}
function setMixedCategoryOptions(control,options,placeholder,id){control.value=String(id||"");}
function setOptions(control){control.value="";}
function configurePendingEntry(){}
function openDialog(dialog){dialog.open=true;}
function submittedMoneyValue(value){return value;}
function editTransfer(id){transferEdits.push(id);}
async function api(path,options){requests.push({path,...options});}
async function ledgerNotification(){throw new Error("Unchanged save must not notify");}
async function refreshDailyPage(){refreshes++;}
function showFormError(form,error){throw error;}
async function check(){
 const original=JSON.stringify(events);
 for(const [render,bodySelector] of [[renderRecentTransactions,"#recentTransactionRows"],[renderQuickUpdates,"#quickUpdateRows"]]){
  render(events);
  const rendered=controls[bodySelector].children;
  assert.equal(rendered.length,events.length);
  for(const [index,event] of events.entries()){
   rendered[index].children[5].children[0].click();
   const form=controls["#transactionForm"],amount=expectedAmounts[event.id];
   assert.equal(controls["#transactionDialog"].open,true);
   assert.equal(controls["#transactionAmount"].value,amount);
   assert.equal(JSON.parse(form.dataset.originalValues).amount,amount);
   assert.equal(controls["#transactionPinned"].checked,event.pinned);
   assert.equal(controls["#transactionDate"].value,event.date);
   assert.equal(controls["#transactionDescription"].value,event.description);
   assert.equal(controls["#transactionDialogTitle"].textContent,"Edit Transaction");
   await saveTransaction({preventDefault(){},currentTarget:form});
   assert.deepEqual(requests.at(-1),{path:`/api/transactions/${event.id}`,method:"PATCH",body:JSON.stringify({date:event.date,accountId:String(event.accountId),description:event.description,categoryId:String(event.categoryId||""),amount,pinned:event.pinned,alsoLogReimbursement:false})});
   assert.equal(controls["#transactionDialog"].open,false);
  }
 }
 assert.equal(refreshes,events.length*2);
 const transfer={...events[0],transferId:"valid-pair",combinedTransfer:true};
 const invalid={...events[0],transferId:"broken-pair",combinedTransfer:false};
 renderRecentTransactions([transfer,invalid]);
 for(const row of controls["#recentTransactionRows"].children)row.children[5].children[0].click();
 assert.deepEqual(transferEdits,["valid-pair","broken-pair"],"Linked rows must keep their transfer editor routing");
 assert.equal(JSON.stringify(events),original,"Editing must not mutate the response or reconstruct its amounts");
}
'''
        fixtures = "const events=" + json.dumps(events) + ";const expectedAmounts=" + json.dumps(expectedAmounts) + ";\n"
        result = subprocess.run([nodePath, "-e", functions + fixtures + harness + "check().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def testCreditDestinationPresentationAndSavedDescription(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "app.js").read_text(encoding="utf-8")
        presentation = "function updateTransferPresentation(" + javascript.split(
            "function updateTransferPresentation(", 1
        )[1].split("async function refreshTransferAmountHelper", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
const accounts = [
 {id:1,defaultTransferDescription:"Disney Card Payment",name:"Disney",accountClass:"credit",accountSubtype:"Credit Card",balanceCents:-100},
 {id:2,defaultTransferDescription:"Disney Card Payment",name:"Disney Card",accountClass:"credit",accountSubtype:"Credit Card",balanceCents:-100},
 {id:3,defaultTransferDescription:"Apple Card Payment",name:"Apple Card",accountClass:"credit",accountSubtype:"Credit Card",balanceCents:-100},
 {id:4,defaultTransferDescription:"Loan Payment",name:"Loan",accountClass:"credit",accountSubtype:"Personal Loan",balanceCents:-100},
 {id:5,defaultTransferDescription:"Other <script> Payment",name:"Other <script>",accountClass:"credit",accountSubtype:"Other",balanceCents:-100},
 {id:6,defaultTransferDescription:"Transfer to Savings",name:"Savings",accountClass:"cash",balanceCents:100},
 {id:7,defaultTransferDescription:"Transfer to External",name:"External",accountClass:"system"}
];
const hint = {hidden:true,children:[],replaceChildren(){this.children=[]},append(text){this.children.push(text)}};
const controls = {
 "#transferFromAccount":{value:"6"}, "#transferToAccount":{value:"",selectedOptions:[]},
 "#transferFromBalance":{}, "#transferToBalance":{}, "#transferPaymentHint":hint,
 "#transferDescription":{value:"custom saved description"}, "#transferSubmitButton":{}
};
const document = {querySelector(selector){return controls[selector]}};
function dollars(cents){return `$${(cents/100).toFixed(2)}`;}
function refreshTransferAmountHelper(){}
for (const account of accounts) {
 controls["#transferToAccount"].value=String(account.id);
 controls["#transferToAccount"].selectedOptions=[{dataset:{accountClass:account.accountClass}}];
 controls["#transferDescription"].value="custom saved description";
 updateTransferPresentation(false);
 assert.equal(controls["#transferDescription"].value,"custom saved description");
 const credit=account.accountClass==="credit";
 assert.equal(controls["#transferSubmitButton"].textContent,credit?"Make Payment":"Save");
 assert.equal(hint.hidden,!credit);
 assert.deepEqual(hint.children,credit?[`This will be recorded as a payment to ${account.name}. `]:[]);
 updateTransferPresentation(true);
 const expected=["Disney Card Payment","Disney Card Payment","Apple Card Payment","Loan Payment","Other <script> Payment","Transfer to Savings","Transfer to External"][account.id-1];
 assert.equal(controls["#transferDescription"].value,expected);
 // Copy the server value even when it cannot be inferred from browser fields.
 account.defaultTransferDescription="Server supplied <description>";
 updateTransferPresentation(true);
 assert.equal(controls["#transferDescription"].value,account.defaultTransferDescription);
}
controls["#transferToAccount"].value="";
controls["#transferToAccount"].selectedOptions=[];
updateTransferPresentation(true);
assert.equal(controls["#transferDescription"].value,"Transfer");
'''
        balanceFunction = javascript[javascript.index("function friendlyAccountBalance("):javascript.index("async function ledgerNotification(")]
        result = subprocess.run([nodePath, "-e", balanceFunction + presentation + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def testSimulatorSummaryLabelOrderAndSignedValues(self):
        source = (projectRoot / "index.html").read_text(encoding="utf-8")
        summary = source.split('class="summaryTable simulatorSummary"', 1)[1].split("</dl>", 1)[0]
        self.assertEqual(
            [html.unescape(label) for label in re.findall(r"<dt>(.*?)</dt>", summary)],
            ["Starting Cash", "Planned Income", "Planned Spending & Buffer", "Transfers & Payments", "Unallocated Money"],
        )
        self.assertEqual(re.findall(r'<dd id="(.*?)"', summary), [
            "simulatorStartingCash", "simulatorIncomeTotal", "simulatorSpendingTotal", "simulatorTransferTotal", "simulatorUnallocated",
        ])
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "app.js").read_text(encoding="utf-8")
        renderFunction = "function renderSimulatorResults()" + javascript.split(
            "function renderSimulatorResults()", 1
        )[1].split("function openSimulatorPlanDialog", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
const controls = {};
const document = {querySelector(selector){return controls[selector] ||= {replaceChildren(){}};}};
function dollars(cents){return `$${(cents/100).toFixed(2)}`;}
const simulator = {plan:{results:[],totals:{startingCashCents:9000,plannedIncomeCents:0,plannedSpendingCents:0,transfersPaymentsCents:0,unallocatedMoneyCents:9000}}};
for (const transferCents of [0, -5000, 5000]) {
 simulator.plan.totals.transfersPaymentsCents=transferCents;
 simulator.plan.totals.unallocatedMoneyCents=9000-transferCents;
 renderSimulatorResults();
 assert.equal(controls["#simulatorTransferTotal"].textContent,dollars(transferCents));
 assert.equal(controls["#simulatorUnallocated"].textContent,dollars(9000-transferCents));
}
'''
        result = subprocess.run([nodePath, "-e", renderFunction + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def testLegacyRollupErrorSuppressesOnlyCategoryFiguresAndRecovers(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "app.js").read_text(encoding="utf-8")
        renderFunction = "function renderDashboardCategories(dashboard)" + javascript.split(
            "function renderDashboardCategories(dashboard)", 1
        )[1].split("function renderDashboardExpenseAccounts", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
const controls = {
 "#expenseCategoryRows":{children:["old figures"],replaceChildren(){this.children=[]}},
 "#categoryReportingError":{}, "#expenseCategoryTable":{}, "#hideZeroBalances":{checked:true}
};
const document = {querySelector(selector){return controls[selector]}};
function addDashboardAmountRow(body,name){body.children.push(name)}
const dashboard = {categoryReportingError:'Category "<script>" needs manual correction.',categories:[{id:1,name:"Child",parentCategoryId:null,amountCents:100}]};
renderDashboardCategories(dashboard);
assert.equal(controls["#categoryReportingError"].textContent,dashboard.categoryReportingError);
assert.equal(controls["#categoryReportingError"].hidden,false);
assert.equal(controls["#expenseCategoryTable"].hidden,true);
assert.deepEqual(controls["#expenseCategoryRows"].children,[]);
dashboard.categoryReportingError=null;
renderDashboardCategories(dashboard);
assert.equal(controls["#categoryReportingError"].textContent,"");
assert.equal(controls["#categoryReportingError"].hidden,true);
assert.equal(controls["#expenseCategoryTable"].hidden,false);
assert.deepEqual(controls["#expenseCategoryRows"].children,["Child"]);
'''
        result = subprocess.run([nodePath, "-e", renderFunction + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def testThemeMetadataTracksPreferenceSystemAndStorageFallback(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "theme.js").read_text(encoding="utf-8")
        harness = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
function setup(savedPreference, darkSystem, blockedStorage=false, legacyListener=false) {
 const root = {dataset:{},removeAttribute(name){assert.equal(name,"data-theme");delete this.dataset.theme;}};
 const meta = {setAttribute(name,value){assert.equal(name,"content");this.content=value;}};
 const media = {matches:darkSystem};
 if(legacyListener)media.addListener=(callback)=>media.change=callback;
 else media.addEventListener=(name,callback)=>{assert.equal(name,"change");media.change=callback;};
 const context = {
  window:{matchMedia(query){assert.equal(query,"(prefers-color-scheme: dark)");return media;}},
  document:{documentElement:root,querySelector(query){assert.equal(query,'meta[name="theme-color"]');return meta;}},
  localStorage:{getItem(key){assert.equal(key,"theme");if(blockedStorage)throw new Error("blocked");return savedPreference;},setItem(key,value){if(blockedStorage)throw new Error("blocked");savedPreference=value;}}
 };
 vm.runInNewContext(themeSource,context);
 return {window:context.window,root,meta,media};
}
for(const preference of [null,"system","invalid","light","dark"]) {
 for(const darkSystem of [false,true]) {
  const state=setup(preference,darkSystem);
  const effectiveDark=preference==="dark"||(!["light","dark"].includes(preference)&&darkSystem);
  assert.equal(state.meta.content,effectiveDark?"#202124":"#FFFFFF");
  assert.equal(state.window.readThemePreference(),["light","dark"].includes(preference)?preference:"system");
  state.media.matches=!darkSystem;state.media.change();
  assert.equal(state.meta.content,preference==="dark"||(!["light","dark"].includes(preference)&&!darkSystem)?"#202124":"#FFFFFF");
  state.window.applyThemePreference("light");assert.equal(state.meta.content,"#FFFFFF");assert.equal(state.root.dataset.theme,"light");
  state.window.applyThemePreference("dark");assert.equal(state.meta.content,"#202124");assert.equal(state.root.dataset.theme,"dark");
  state.window.applyThemePreference("system");assert.equal(state.root.dataset.theme,undefined);
 }
}
for(const legacy of [false,true]) {
 const blocked=setup("light",true,true,legacy);
 assert.equal(blocked.window.readThemePreference(),"system");assert.equal(blocked.meta.content,"#202124");
 blocked.window.applyThemePreference("light");assert.equal(blocked.meta.content,"#FFFFFF");
 blocked.window.applyThemePreference("system");blocked.media.matches=false;blocked.media.change();assert.equal(blocked.meta.content,"#FFFFFF");
}
'''
        result = subprocess.run([nodePath, "-e", "const themeSource=" + json.dumps(javascript) + ";\n" + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def testDashboardUsesBackendUtilizationStateAndLabel(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "app.js").read_text(encoding="utf-8")
        renderFunction = "function renderDashboardAccounts(dashboard)" + javascript.split(
            "function renderDashboardAccounts(dashboard)", 1
        )[1].split("function renderDashboardCategories", 1)[0]
        badgeFunction = "function statusBadge(" + javascript.split(
            "function statusBadge(", 1
        )[1].split("function renderSimulatorResults", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
function element(){return {children:[],attributes:{},replaceChildren(){this.children=[];},append(...children){this.children.push(...children);},setAttribute(name,value){this.attributes[name]=value;},getAttribute(name){return this.attributes[name];},insertRow(){const row=element();this.children.push(row);row.insertCell=()=>{const cell=element();row.children.push(cell);return cell;};return row;}};}
const controls = {};
const document = {querySelector(selector){return controls[selector] ||= element();},createElement(){return element();}};
function dollars(cents){return `$${(cents/100).toFixed(2)}`;}
const status = (label,state)=>({label,state,accessibleLabel:`Backend ${state} utilization: ${label}`});
const dashboard = {
 accounts:[{id:1,name:"Near boundary",accountClass:"credit",accountSubtype:"Credit Card",creditLimitCents:2500000,balanceCents:-749900,utilization:status("30.00%","green")}],
 minimumBalanceAlerts:[],creditUtilizationAlerts:[],totalCreditBalanceCents:749900,
 totalUtilization:status("50.00%","yellow")
};
renderDashboardAccounts(dashboard);
const accountBadge=controls["#creditAccountRows"].children[0].children[0].children[1];
assert.equal(accountBadge.textContent,"30.00%");assert.equal(accountBadge.className,"utilizationBadge utilizationGreen");
assert.equal(accountBadge.getAttribute("aria-label"),"Backend green utilization: 30.00%");
assert.equal(controls["#totalCreditUtilization"].textContent,"50.00%");
assert.equal(controls["#totalCreditUtilization"].className,"utilizationBadge utilizationYellow");
assert.equal(controls["#totalCreditUtilization"].getAttribute("aria-label"),"Backend yellow utilization: 50.00%");
assert.equal(controls["#totalCreditUtilizationRow"].hidden,false);
dashboard.accounts[0].utilization=null;dashboard.totalUtilization=null;
renderDashboardAccounts(dashboard);
assert.equal(controls["#creditAccountRows"].children[0].children[0].children.length,0);
assert.equal(controls["#totalCreditUtilizationRow"].hidden,true);
'''
        balanceFunction = javascript[javascript.index("function friendlyAccountBalance("):javascript.index("async function ledgerNotification(")]
        result = subprocess.run([nodePath, "-e", badgeFunction + "\n" + balanceFunction + renderFunction + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
