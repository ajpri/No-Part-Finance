import tests
import json
import sqlite3
import os
import shutil
import subprocess
from pathlib import Path
import unittest

import database as ledger
from app import createApp
from tests import test_pending_next as pendingFixtures
from tests.test_release_2610 import signIn


class IntegrationTransferDescriptionTests(unittest.TestCase):
    setUp = pendingFixtures.PendingNextTests.setUp
    tearDown = pendingFixtures.PendingNextTests.tearDown
    submit = pendingFixtures.PendingNextTests.submit
    rows = pendingFixtures.PendingNextTests.rows
    snapshot = pendingFixtures.PendingNextTests.snapshot
    integrationUpdateFields = pendingFixtures.PendingNextTests.integrationUpdateFields
    integrationPendingRow = pendingFixtures.PendingNextTests.integrationPendingRow
    updateThroughIntegration = pendingFixtures.PendingNextTests.updateThroughIntegration

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
        for index, (destination, expected) in enumerate(pairs):
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
const begin = source.indexOf("function paymentDescription(account)");
const end = source.indexOf("function updateTransferPresentation", begin);
const pendingBegin = source.indexOf("function pendingTransferDefaultDescription(row)");
const pendingEnd = source.indexOf("function cancelPendingRowChanges", pendingBegin);
let actions = 0, previews = 0;
const pendingContext = {accounts: [{id: 1, name: "Visa", accountClass: "credit", accountSubtype: "Credit Card"}], pendingDecisionBusy: false, updatePendingRowActions: () => actions++, previewPendingRow: () => previews++};
vm.createContext(pendingContext);
vm.runInContext(source.slice(begin, end) + source.slice(pendingBegin, pendingEnd), pendingContext);
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
