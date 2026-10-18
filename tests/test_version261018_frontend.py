"""Focused executable checks for release presentation and PWA metadata."""

import os
import html
import json
import re
from pathlib import Path
import shutil
import subprocess
import unittest

import tests  # Enable the database guard before application imports.


projectRoot = Path(__file__).resolve().parents[1]


class ReleaseFrontendTests(unittest.TestCase):
    def testCreditDestinationPresentationAndSavedDescription(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        javascript = (projectRoot / "app.js").read_text(encoding="utf-8")
        presentation = "function paymentDescription(account)" + javascript.split(
            "function paymentDescription(account)", 1
        )[1].split("async function refreshTransferAmountHelper", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
const accounts = [
 {id:1,name:"Disney",accountClass:"credit",accountSubtype:"Credit Card",balanceCents:-100},
 {id:2,name:"Disney Card",accountClass:"credit",accountSubtype:"Credit Card",balanceCents:-100},
 {id:3,name:"Apple Card",accountClass:"credit",accountSubtype:"Credit Card",balanceCents:-100},
 {id:4,name:"Loan",accountClass:"credit",accountSubtype:"Personal Loan",balanceCents:-100},
 {id:5,name:"Other <script>",accountClass:"credit",accountSubtype:"Other",balanceCents:-100},
 {id:6,name:"Savings",accountClass:"cash",balanceCents:100},
 {id:7,name:"External",accountClass:"system"}
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
}
'''
        result = subprocess.run([nodePath, "-e", presentation + harness], capture_output=True, text=True)
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
        )[1].split("\n", 1)[0]
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
        badgeFunction = next(line for line in javascript.splitlines() if line.startswith("function statusBadge("))
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
        result = subprocess.run([nodePath, "-e", badgeFunction + "\n" + renderFunction + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
