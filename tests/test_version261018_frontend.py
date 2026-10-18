"""Focused executable checks for release presentation and PWA metadata."""

import os
import html
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
