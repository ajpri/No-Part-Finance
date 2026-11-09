import tests
from tests.test_support import PendingFixture
import json
import sqlite3
import os
import shutil
import subprocess
from pathlib import Path
import unittest

import database as ledger
from app import createApp
from tests.test_support import signIn


class IntegrationTransferDescriptionTests(PendingFixture, unittest.TestCase):
    def setEnabled(self, enabled=True):
        response = self.owner.put("/api/settings/integration-transfers", json={"useDefaultDescription": enabled})
        self.assertEqual(200, response.status_code, response.get_json())

    def testNormalizedSubmissionRollsBackIfReviewWriteFails(self):
        self.setEnabled()
        with self.connection:
            self.connection.execute("CREATE TRIGGER failNormalizedReview BEFORE UPDATE OF reviewContent ON pendingSubmissions BEGIN SELECT RAISE(ABORT, 'review failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(self.transfer)
        self.assertEqual(before, self.snapshot())

    def testDefaultOffPersistsAndDoesNotRewriteExistingProposals(self):
        self.assertEqual("0", self.owner.get("/api/settings").get_json()["integrationTransferDefaultDescription"])
        proposal = self.submit(self.transfer)
        self.setEnabled()
        self.assertEqual("Example payment", self.integrationPendingRow(proposal["id"])["description"])
        restarted = createApp(self.path).test_client()
        signIn(restarted)
        self.assertEqual("1", restarted.get("/api/settings").get_json()["integrationTransferDefaultDescription"])
        self.setEnabled(False)
        self.assertEqual("0", self.owner.get("/api/settings").get_json()["integrationTransferDefaultDescription"])

    def testSubmissionNormalizesNamingAndPreservesOriginalRetries(self):
        self.setEnabled()
        cash = ledger.createAccount(self.connection, "Savings", "cash")
        loan = ledger.createAccount(self.connection, "Example Loan", "credit", "Personal Loan")
        card = ledger.createAccount(self.connection, "Another CARD", "credit", "Credit Card")
        visa = ledger.createAccount(self.connection, "Visa", "credit", "Credit Card")
        other = ledger.createAccount(self.connection, "Other Debt", "credit", "Other")
        pairs = ((visa, "Visa Card Payment"), (other, "Other Debt Payment"), (self.card, "Example Card Payment"), (cash, "Transfer to Savings"), (loan, "Example Loan Payment"), (card, "Another CARD Payment"), (self.external, "Transfer to External"))
        edgeCases = (("Scorecard", "Scorecard Card Payment"), ("Visa card", "Visa card Payment"), ("_Card", "_Card Card Payment"), ("éCard", "éCard Payment"), ("Card <script>", "Card <script> Card Payment"))
        pairs += tuple((ledger.createAccount(self.connection, name, "credit", "Credit Card"), expected) for name, expected in edgeCases)
        before = self.snapshot()
        browserAccounts = {row["id"]: row for row in self.owner.get("/api/accounts").get_json()}
        integrationResponse = self.agent.get("/api/integration/read/accounts", headers=self.headers)
        self.assertEqual(200, integrationResponse.status_code)
        integrationAccounts = {row["id"]: row for row in integrationResponse.get_json()}
        self.assertEqual(before, self.snapshot(), "Catalogue defaults must not write any state")
        for index, (destination, expected) in enumerate(pairs):
            self.assertEqual(expected, browserAccounts[destination]["defaultTransferDescription"])
            self.assertEqual(expected, integrationAccounts[destination]["defaultTransferDescription"])
            submitted = {**self.transfer, "submissionId": f"normalized-{index}", "toAccountId": destination}
            response = self.agent.post("/api/integration/submissions", json=submitted, headers=self.headers)
            self.assertEqual(201, response.status_code, response.get_json())
            proposal = self.connection.execute("SELECT * FROM pendingSubmissions WHERE submissionId = ?", (submitted["submissionId"],)).fetchone()
            self.assertEqual(submitted["description"], json.loads(proposal["originalContent"])["description"])
            self.assertEqual(expected, self.integrationPendingRow(proposal["id"])["description"])
            self.assertEqual(200, self.agent.post("/api/integration/submissions", json=submitted, headers=self.headers).status_code)
            self.assertEqual(409, self.agent.post("/api/integration/submissions", json={**submitted, "description": expected}, headers=self.headers).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertEqual({expected for _, expected in pairs}, {row[0] for row in self.connection.execute("SELECT description FROM transactions")})
        self.assertTrue(all(row[0] == 0 for row in self.connection.execute("SELECT SUM(amountCents) FROM transactions GROUP BY transferId")))

    def testCatalogueRefreshAndAccountChangesPreserveSavedDescriptions(self):
        self.setEnabled()
        proposal = self.submit(self.transfer)
        ledger.createTransfer(self.connection, "2028-02-29", self.cash, self.card, "5.00", "Saved custom payment")
        plan = ledger.createSimulatorPlan(self.connection, "Example Plan", "2028-02-29")
        rowId = ledger.saveSimulatorRow(self.connection, plan, None, "transfer", self.cash, self.card, label="Saved custom label", amount="5.00")
        ledger.updateAccount(self.connection, self.card, "Renamed Loan", "credit", "Personal Loan", active=False)
        before = self.snapshot()
        for response in (self.owner.get("/api/accounts"), self.agent.get("/api/integration/read/accounts", headers=self.headers)):
            self.assertEqual(200, response.status_code)
            account = next(row for row in response.get_json() if row["id"] == self.card)
            self.assertEqual("Renamed Loan Payment", account["defaultTransferDescription"])
            self.assertFalse(account["active"])
        self.assertEqual("Example Card Payment", self.integrationPendingRow(proposal["id"])["description"])
        self.assertEqual(["Saved custom payment"] * 2, [row[0] for row in self.connection.execute("SELECT description FROM transactions")])
        self.assertEqual("Saved custom label", self.connection.execute("SELECT label FROM simulatorRows WHERE id = ?", (rowId,)).fetchone()[0])
        self.assertEqual(before, self.snapshot())
        self.assertNotIn("defaultTransferDescription", {row[1] for row in self.connection.execute("PRAGMA table_info(accounts)")})
        contract = self.owner.get("/openapi.json").get_json()
        schema = contract["paths"]["/api/integration/read/accounts"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["items"]
        self.assertIn("defaultTransferDescription", schema["required"])
        self.assertEqual("string", schema["properties"]["defaultTransferDescription"]["type"])

    def testFrontendSimulatorAndPendingCopyCatalogueDefaultsWithoutChangingSavedDrafts(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("JavaScript runtime unavailable")
        ledger.createAccount(self.connection, "Savings", "cash")
        ledger.createAccount(self.connection, "Loan", "credit", "Personal Loan")
        ledger.createAccount(self.connection, "Other <script>", "credit", "Other")
        catalogue = self.owner.get("/api/accounts").get_json()
        harness = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8"), accounts = JSON.parse(fs.readFileSync(0, "utf8"));
class Element {
 constructor(tag) {this.tag=tag;this.children=[];this.dataset={};this.events={};this.value="";this.className="";const names=new Set();this.classList={add:name=>names.add(name),remove:name=>names.delete(name),contains:name=>names.has(name)};}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=[...items];}
 insertCell(){const cell=new Element("td");this.append(cell);return cell;}
 insertBefore(item,before){this.children.splice(this.children.indexOf(before),0,item);}
 addEventListener(name,handler){(this.events[name]??=[]).push(handler);}
 dispatch(name){for(const handler of this.events[name]||[])handler({});}
 setAttribute(){}
 matches(selector){return selector.startsWith(".")?this.className.split(" ").includes(selector.slice(1))||this.classList.contains(selector.slice(1)):this.tag===selector;}
 querySelectorAll(selector){return this.children.filter(child=>child instanceof Element).flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
const body=new Element("tbody");
const document={createElement:tag=>new Element(tag),querySelector:selector=>{assert.equal(selector,"#simulatorRows");return body;}};
function select(rows,placeholder,value){const control=new Element("select");control.value=String(value??"");return control;}
let previews=0;
const context={accounts,document,simulatorDirtyRows:new Set(),simulatorSelect:select,simulatorAccountSelect:select,dollars:()=>"$5.00",pendingDecisionBusy:false,previewPendingRow:()=>previews++};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf("function appendSimulatorRow("),source.indexOf("function recordSimulatorRow(")),context);
const cash=accounts.find(account=>account.accountClass==="cash"),card=accounts.find(account=>account.accountClass==="credit");
for(const rowType of ["transfer","payoff"]){
 const saved={id:10,rowType,fromAccountId:cash.id,toAccountId:card.id,label:"Saved <custom> label",amountCents:500,effectiveAmountCents:500};
 context.appendSimulatorRow(saved);
 const row=body.children.at(-1),accountCell=row.children[2],label=()=>row.children[3].querySelector("input");
 assert.equal(label().value,saved.label,"Rendering an existing row preserves its label");
 const destination=accountCell.querySelector(".toAccount"),from=accountCell.querySelector(".fromAccount");
 label().value="User's custom label";from.dispatch("change");assert.equal(label().value,"User's custom label");
 for(const account of accounts){
  label().value="User's custom label";destination.value=String(account.id);destination.dispatch("change");
  assert.equal(label().value,["cash","credit"].includes(account.accountClass)?account.defaultTransferDescription:"User's custom label");
 }
 destination.value="999999";label().value="Keep unavailable destination label";destination.dispatch("change");assert.equal(label().value,"Keep unavailable destination label");
 const cancel=row.children[5].querySelectorAll("button").find(button=>button.textContent==="Cancel Changes");
 cancel.dispatch("click");assert.equal(label().value,saved.label);assert.equal(accountCell.querySelector(".toAccount").value,String(card.id));
}
// Exercise the Pending draft, default-button state, and cancellation together.
const draftStart=source.indexOf("function pendingDraft("),draftEnd=source.indexOf("\nfunction ",draftStart+1);
vm.runInContext(source.slice(draftStart,draftEnd),context);
const actionsStart=source.indexOf("function updatePendingRowActions("),actionsEnd=source.indexOf("async function savePendingRow(",actionsStart);
vm.runInContext(source.slice(actionsStart,actionsEnd)+source.slice(source.indexOf("function pendingTransferDefaultDescription("),source.indexOf("async function previewPendingRow(")),context);
const fields=Object.fromEntries(Object.entries({fromAccountId:String(cash.id),toAccountId:String(card.id),description:"Saved custom description",date:"2028-02-29",amount:"5.00"}).map(([name,value])=>[name,{value,dataset:{pendingField:name}}]));
const button={setAttribute(){}};
const row={dataset:{id:"20",kind:"transfer"},saving:false,classList:{toggle(){}},querySelectorAll:()=>Object.values(fields),querySelector(selector){if(selector==="[data-pending-default-description]")return button;if(selector.startsWith("[data-pending-field="))return fields[selector.match(/data-pending-field="?([^"\]]+)/)[1]];return {};}};
row.savedDraft=JSON.parse(JSON.stringify(context.pendingDraft(row)));
context.updatePendingRowActions(row);assert.equal(fields.description.value,"Saved custom description");assert.equal(button.disabled,false);
for(const account of accounts){
 fields.toAccountId.value=String(account.id);fields.description.value="User draft";
 assert.equal(context.usePendingTransferDescription(row),true);assert.equal(fields.description.value,account.defaultTransferDescription);assert.equal(button.disabled,true);
 fields.description.value+=" edited";context.updatePendingRowActions(row);assert.equal(button.disabled,false);
}
fields.toAccountId.value="999999";fields.description.value="Keep unavailable destination description";context.updatePendingRowActions(row);assert.equal(button.disabled,true);assert.equal(context.usePendingTransferDescription(row),false);assert.equal(fields.description.value,"Keep unavailable destination description");
fields.toAccountId.value=String(card.id);
for(const busy of ["saving","decision"]){row.saving=busy==="saving";context.pendingDecisionBusy=busy==="decision";assert.equal(context.usePendingTransferDescription(row),false);assert.equal(fields.description.value,"Keep unavailable destination description");}
row.saving=false;context.pendingDecisionBusy=false;context.cancelPendingRowChanges(row);assert.equal(fields.description.value,row.savedDraft.description);assert.equal(fields.toAccountId.value,row.savedDraft.toAccountId);
assert(previews>0);
"""
        javascriptPath = Path(__file__).resolve().parents[1] / "app.js"
        result = subprocess.run([nodePath, "-e", harness, str(javascriptPath)], input=json.dumps(catalogue), capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def testUpdatesUseChangedDestinationAndOwnerManualAndOrdinaryStayEditable(self):
        self.setEnabled()
        proposal = self.submit(self.transfer)
        original = self.connection.execute("SELECT originalContent FROM pendingSubmissions WHERE id = ?", (proposal["id"],)).fetchone()[0]
        fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        self.assertEqual(200, self.updateThroughIntegration(proposal["id"], {**fields, "fromAccountId": self.card, "toAccountId": self.cash, "description": "Wrong destination wording"}).status_code)
        self.assertEqual("Transfer to Example Cash", self.integrationPendingRow(proposal["id"])["description"])
        self.assertEqual(original, self.connection.execute("SELECT originalContent FROM pendingSubmissions WHERE id = ?", (proposal["id"],)).fetchone()[0])
        fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        self.assertEqual(200, self.owner.patch(f"/api/pending/{proposal['id']}", json={**fields, "reviewedRole": None, "description": "Owner wording"}).status_code)
        self.assertEqual("Owner wording", self.integrationPendingRow(proposal["id"])["description"])
        manual = self.submit({**self.transfer, "submissionId": "manual"}, manual=True)
        self.assertEqual("Example payment", self.integrationPendingRow(manual["id"])["description"])
        ordinary = self.submit(self.ordinary)
        fields = self.integrationUpdateFields(self.integrationPendingRow(ordinary["id"]))
        self.assertEqual(200, self.updateThroughIntegration(ordinary["id"], {**fields, "description": "Ordinary wording"}).status_code)
        self.assertEqual("Ordinary wording", self.integrationPendingRow(ordinary["id"])["description"])
        self.setEnabled(False)
        fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        self.assertEqual(200, self.updateThroughIntegration(proposal["id"], {**fields, "description": "Integration wording"}).status_code)
        self.assertEqual("Integration wording", self.integrationPendingRow(proposal["id"])["description"])

    def testSettingsValidateStrictBooleanOwnerAndCsrf(self):
        before = self.snapshot()
        for submitted in ({}, {"useDefaultDescription": "true"}, {"useDefaultDescription": 1}, {"useDefaultDescription": None}, {"useDefaultDescription": True, "extra": True}, []):
            self.assertEqual(400, self.owner.put("/api/settings/integration-transfers", json=submitted).status_code)
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        request = {"useDefaultDescription": True}
        self.assertEqual(403, auditor.put("/api/settings/integration-transfers", json=request).status_code)
        self.assertEqual(403, self.agent.put("/api/settings/integration-transfers", headers=self.headers, json=request).status_code)
        self.assertEqual(401, self.app.test_client().put("/api/settings/integration-transfers", json=request).status_code)
        token = self.owner.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, self.owner.put("/api/settings/integration-transfers", json=request).status_code)
        self.owner.environ_base["HTTP_X_CSRF_TOKEN"] = token
        self.assertEqual(before, self.snapshot())

    def testBlankNormalizationStillValidatesFinancialFieldsAndStaleUpdates(self):
        self.setEnabled()
        for index, description in enumerate(("", "   ")):
            proposal = self.submit({**self.transfer, "submissionId": f"blank-{index}", "description": description})
            self.assertEqual("Example Card Payment", self.integrationPendingRow(proposal["id"])["description"])
        for replacement in ({"description": None}, {"description": 2}, {"toAccountId": 99999}, {"date": "2026-02-30"}, {"amount": "-1.00"}):
            before = self.snapshot()
            self.assertEqual(400, self.agent.post("/api/integration/submissions", json={**self.transfer, **replacement}, headers=self.headers).status_code)
            self.assertEqual(before, self.snapshot())
        missing = {key: value for key, value in self.transfer.items() if key != "description"}
        self.assertEqual(400, self.agent.post("/api/integration/submissions", json=missing, headers=self.headers).status_code)
        fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        for replacement in ({"description": None}, {"description": 2}, {"toAccountId": 99999}, {"date": "2026-02-30"}, {"amount": "-1.00"}):
            before = self.snapshot()
            self.assertEqual(400, self.updateThroughIntegration(proposal["id"], {**fields, **replacement}).status_code)
            self.assertEqual(before, self.snapshot())
        self.assertEqual(200, self.updateThroughIntegration(proposal["id"], {**fields, "description": " "}).status_code)
        before = self.snapshot()
        self.assertEqual(409, self.updateThroughIntegration(proposal["id"], fields).status_code)
        self.assertEqual(before, self.snapshot())

    def testIntegrationManualSourceUpdateNormalizesAndRetryKeepsOwnerEdits(self):
        manual = self.submit({**self.transfer, "submissionId": "manual"}, manual=True)
        self.setEnabled()
        fields = self.integrationUpdateFields(self.integrationPendingRow(manual["id"]))
        self.assertEqual(200, self.updateThroughIntegration(manual["id"], {**fields, "description": "Wrong"}).status_code)
        self.assertEqual("Example Card Payment", self.integrationPendingRow(manual["id"])["description"])
        proposal = self.submit(self.transfer)
        fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        self.assertEqual(200, self.owner.patch(f"/api/pending/{proposal['id']}", json={**fields, "reviewedRole": None, "description": "Owner wording"}).status_code)
        self.setEnabled(False)
        self.assertEqual(200, self.agent.post("/api/integration/submissions", json=self.transfer, headers=self.headers).status_code)
        self.setEnabled()
        self.assertEqual(200, self.agent.post("/api/integration/submissions", json=self.transfer, headers=self.headers).status_code)
        self.assertEqual("Owner wording", self.integrationPendingRow(proposal["id"])["description"])

    def testFrontendSettingsSaveAndPendingDefaultDescriptionButton(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js is unavailable; frontend behavior check requires a JavaScript runtime")
        harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8");
const settingHandler = source.slice(source.indexOf('document.querySelector("#integrationTransferSettingsForm").addEventListener'), source.indexOf('document.querySelector("#integrationSecretForm").addEventListener'));
let handler, request, notification, failure;
const errorElement = {textContent: "Old error"};
const form = {querySelector: () => errorElement, addEventListener: (name, callback) => handler = callback};
const checkbox = {checked: true};
const context = {document: {querySelector: selector => selector === "#integrationTransferSettingsForm" ? form : checkbox}, api: async (path, options) => {request = {path, options}; if (failure) throw new Error("Save failed");}, sendNotification: message => notification = message, showFormError: (target, error) => {assert.equal(target, form); errorElement.textContent = error.message;}};
vm.runInNewContext(settingHandler, context);
(async () => {
let prevented = 0;
await handler({currentTarget: form, preventDefault: () => prevented++});
assert.equal(request.path, "/api/settings/integration-transfers");
assert.equal(request.options.method, "PUT");
assert.deepEqual(JSON.parse(request.options.body), {useDefaultDescription: true});
assert.equal(errorElement.textContent, "");
assert.equal(notification, "Integration settings saved.");
failure = true; checkbox.checked = false;
await handler({currentTarget: form, preventDefault: () => prevented++});
assert.equal(errorElement.textContent, "Save failed");
assert.deepEqual(JSON.parse(request.options.body), {useDefaultDescription: false});
assert.equal(prevented, 2);
assert(source.includes('defaultDescription.addEventListener("click", () => usePendingTransferDescription(row))'));
const pendingBegin = source.indexOf("function pendingTransferDefaultDescription(row)");
const pendingEnd = source.indexOf("function cancelPendingRowChanges", pendingBegin);
let actions = 0, previews = 0;
const pendingContext = {accounts: [{id: 1, name: "Visa", accountClass: "credit", accountSubtype: "Credit Card", defaultTransferDescription: "Visa Card Payment"}], pendingDecisionBusy: false, updatePendingRowActions: () => actions++, previewPendingRow: () => previews++};
vm.createContext(pendingContext);
vm.runInContext(source.slice(pendingBegin, pendingEnd), pendingContext);
const description = {value: "Incorrect"};
const row = {dataset: {kind: "transfer"}, saving: false, querySelector: selector => selector.includes("toAccountId") ? {value: "1"} : description};
pendingContext.row = row;
assert.equal(vm.runInContext("usePendingTransferDescription(row)", pendingContext), true);
assert.equal(description.value, "Visa Card Payment");
assert.equal(actions, 1); assert.equal(previews, 1);
console.log("Settings save success/failure and Pending default-description button checks passed.");
})();
"""
        javascriptPath = Path(__file__).resolve().parents[1] / "app.js"
        completed = subprocess.run([nodePath, "-e", harness, str(javascriptPath)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
