from tests.test_support import snapshotWithoutLoginTime
from tests.test_support import useHistoricalSchema21
import json
import getpass
import re
import runpy
import sqlite3
import tempfile
import threading
import zipfile
from io import BytesIO
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import tests
from app import createApp
from database import (
    ValidationError, SubmissionConflict, connectDatabase, initializeDatabase,
    createAccount, createCategory, createTransaction, createTransfer, createTemplate,
    createSimulatorPlan, saveSimulatorRow, createIntegrationSecret,
    submitPendingTransaction, decidePendingTransactions, pendingReviewRows,
    updateCategory, schemaVersion,
)
from users import createUser, resetPassword, configureOnStartup, manageUsers
from tests.test_support import connectFixtureDatabase as connectDatabase


def signIn(client, username="Owner", password="owner password"):
    html = client.get("/sign-in").get_data(as_text=True)
    token = re.search(r'name="csrfToken" value="([^"]+)"', html).group(1)
    response = client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token})
    if response.status_code == 302:
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
    return response


class OctoberTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test-finance.db"
        self.app = createApp(self.path)
        self.app.config["PACKAGE_OUTPUT_DIRECTORY"] = str(Path(self.directory.name) / "packages")
        self.connection = connectDatabase(self.path)
        createUser(self.connection, "owner", "Owner", "owner password")
        createUser(self.connection, "auditor", "Auditor", "auditor password")
        self.cashId = createAccount(self.connection, "Example Cash", "cash", openingBalance="100.00")
        self.creditId = createAccount(self.connection, "Example Card", "credit", "Credit Card", openingBalance="50.00")
        self.expenseId = createCategory(self.connection, "Example Expense")
        self.incomeId = createCategory(self.connection, "Example Income", reportingRole="income")
        self.secretId = createIntegrationSecret(self.connection, "Example Agent")
        self.secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.secretId,)).fetchone()[0]
        self.headers = {"Authorization": "Bearer " + self.secret}
        self.owner = self.app.test_client()
        self.assertEqual(302, signIn(self.owner).status_code)
        self.agent = self.app.test_client()
        self.proposed = {"submissionId": "example-1", "date": "2026-10-01", "accountId": self.cashId, "description": "Example expense", "categoryId": self.expenseId, "amount": "50.00"}

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def submit(self, proposed=None):
        return self.agent.post("/api/integration/submissions", json=proposed or self.proposed, headers=self.headers)

    def approveRow(self, proposalId, **corrections):
        return {"id": proposalId, **{key: value for key, value in self.proposed.items() if key != "submissionId"}, "reviewedRole": "expense", **corrections}

    def taskFourSnapshot(self):
        # Compare all authoritative state without printing synthetic credentials on failure.
        return tuple(snapshotWithoutLoginTime(self.connection))

    def testMaintainedMutationAuthenticationAndCsrfLeaveDatabaseUnchanged(self):
        otherOwner = self.app.test_client()
        self.assertEqual(302, signIn(otherOwner).status_code)
        otherToken = otherOwner.get("/api/session").get_json()["csrfToken"]
        validToken = self.owner.environ_base.pop("HTTP_X_CSRF_TOKEN")
        visitor = self.app.test_client()
        auditor = self.app.test_client()
        self.assertEqual(302, signIn(auditor, "Auditor", "auditor password").status_code)
        before = self.taskFourSnapshot()
        try:
            for rule in self.app.url_map.iter_rules():
                if rule.endpoint == "signIn" or str(rule).startswith("/api/integration/"):
                    continue
                url = re.sub(r"<[^>]+>", "1", str(rule))
                for method in rule.methods & {"POST", "PATCH", "PUT", "DELETE"}:
                    with self.subTest(route=url, method=method):
                        self.assertEqual(401, visitor.open(url, method=method, json={}).status_code)
                        for token in (None, "incorrect-token", otherToken):
                            headers = {} if token is None else {"X-CSRF-Token": token}
                            response = self.owner.open(url, method=method, json={}, headers=headers)
                            self.assertEqual(403, response.status_code)
                            self.assertIn("CSRF", response.get_json()["error"])
                            self.assertTrue(before == self.taskFourSnapshot(), "Rejected CSRF changed database state")
                        self.assertEqual(403, self.agent.open(url, method=method, json={}, headers=self.headers).status_code)
                        if rule.endpoint not in {"evaluateMoneyExpression", "accountMinimumHelper", "accountUtilizationWarningHelper", "logOut"}:
                            self.assertEqual(403, auditor.open(url, method=method, json={}).status_code)
                        self.assertTrue(before == self.taskFourSnapshot(), "Unauthorized write changed database state")
        finally:
            self.owner.environ_base["HTTP_X_CSRF_TOKEN"] = validToken
        self.assertEqual(200, self.owner.get("/api/session").status_code)
        # The integration submission boundary intentionally uses Bearer authentication, not browser CSRF.
        self.assertEqual(201, self.submit().status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testProposalApprovalUsesAllEntrySignsOnCashAndCredit(self):
        updateCategory(self.connection, self.expenseId, "Income-looking name", reportingRole="expense")
        updateCategory(self.connection, self.incomeId, "Expense-looking name", reportingRole="income")
        initialAccounts = self.owner.get("/api/accounts").get_json()
        initialReport = self.owner.get("/api/dashboard?period=all").get_json()
        decisions = []
        expectedLedger = []
        expectedDisplay = []
        for accountId in (self.cashId, self.creditId):
            for index, (categoryId, role, amount, ledgerCents, displayCents) in enumerate([
                (self.expenseId, "expense", "12.34", -1234, 1234),
                (self.expenseId, "expense", "-2.34", 234, -234),
                (self.incomeId, "income", "9.00", 900, 900),
                (self.incomeId, "income", "-1.00", -100, -100),
            ]):
                proposed = {**self.proposed, "submissionId": f"sign-{accountId}-{index}", "accountId": accountId, "categoryId": categoryId, "amount": amount}
                response = self.submit(proposed)
                self.assertEqual(201, response.status_code)
                decisions.append({"id": response.get_json()["id"], **{key: value for key, value in proposed.items() if key != "submissionId"}, "reviewedRole": role, "reviewVersion": 0})
                expectedLedger.append(ledgerCents)
                expectedDisplay.append(displayCents)
        self.assertEqual(initialAccounts, self.owner.get("/api/accounts").get_json())
        self.assertEqual(initialReport, self.owner.get("/api/dashboard?period=all").get_json())
        response = self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": decisions, "approveAll": True})
        self.assertEqual(200, response.status_code)
        self.assertEqual(8, response.get_json()["count"])
        self.assertEqual(expectedLedger, [row[0] for row in self.connection.execute("SELECT amountCents FROM transactions ORDER BY id")])
        response = self.agent.get("/api/integration/read/transactions?period=all", headers=self.headers)
        self.assertEqual(200, response.status_code)
        events = sorted(response.get_json()["transactions"], key=lambda event: event["id"])
        self.assertEqual(expectedDisplay, [event["displayAmountCents"] for event in events])
        balances = {row["id"]: row["balanceCents"] for row in self.owner.get("/api/accounts").get_json() if row["accountClass"] != "system"}
        self.assertEqual({self.cashId: 9800, self.creditId: -5200}, balances)
        pending = self.agent.get("/api/integration/submissions", headers=self.headers).get_json()["submissions"]
        self.assertTrue(all(row["status"] == "Approved" for row in pending))

    def testRetryIdentitySurvivesCorrectionArchivingRejectionAndRestart(self):
        proposal = self.submit().get_json()
        corrected = {"date": "2026-09-30", "accountId": self.creditId, "description": "Corrected income reversal", "categoryId": self.incomeId, "amount": "-7.00", "reviewedRole": "income", "reviewVersion": 0}
        self.assertEqual(200, self.owner.patch(f"/api/pending/{proposal['id']}", json=corrected).status_code)
        with self.connection:
            self.connection.execute("UPDATE accounts SET active=0 WHERE id=?", (self.cashId,))
            self.connection.execute("UPDATE categories SET active=0 WHERE id=?", (self.expenseId,))
        before = self.taskFourSnapshot()
        restarted = createApp(self.path)
        agent = restarted.test_client()
        retried = agent.post("/api/integration/submissions", json=self.proposed, headers=self.headers)
        self.assertEqual(200, retried.status_code)
        self.assertEqual(proposal["id"], retried.get_json()["id"])
        self.assertEqual("Pending", retried.get_json()["status"])
        self.assertEqual(409, agent.post("/api/integration/submissions", json={**self.proposed, "amount": "51.00"}, headers=self.headers).status_code)
        self.assertEqual(400, agent.post("/api/integration/submissions", json={**self.proposed, "submissionId": "new-archived"}, headers=self.headers).status_code)
        self.assertTrue(before == self.taskFourSnapshot(), "Retries changed original or reviewed proposal")
        owner = restarted.test_client()
        self.assertEqual(302, signIn(owner).status_code)
        saved = owner.get("/api/pending").get_json()["rows"][0]
        for key in ("date", "accountId", "description", "categoryId", "amount"):
            self.assertEqual(corrected[key], saved[key])
        self.assertEqual(1, saved["reviewVersion"])
        response = owner.post("/api/pending/decision", json={"decision": "reject", "rows": [{"id": proposal["id"], "reviewVersion": 1}]})
        self.assertEqual(200, response.status_code)
        rejected = self.taskFourSnapshot()
        finalAgent = createApp(self.path).test_client()
        retried = finalAgent.post("/api/integration/submissions", json=self.proposed, headers=self.headers)
        self.assertEqual(200, retried.status_code)
        self.assertEqual(proposal["id"], retried.get_json()["id"])
        self.assertEqual("Rejected", retried.get_json()["status"])
        self.assertTrue(rejected == self.taskFourSnapshot(), "Rejected retry changed database state")
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testLateApprovalStatusFailuresRollbackLedgerAndProposalState(self):
        first = self.submit().get_json()["id"]
        second = self.submit({**self.proposed, "submissionId": "late-second"}).get_json()["id"]
        rows = [self.approveRow(first), self.approveRow(second)]
        for selected, approveAll, failedId in [(rows[:1], False, first), (rows, False, second), (rows, True, second)]:
            with self.subTest(approveAll=approveAll, rowCount=len(selected)):
                with self.connection:
                    self.connection.execute(f"CREATE TRIGGER taskFourApprovalFailure BEFORE UPDATE OF status ON pendingSubmissions WHEN NEW.id = {failedId} AND NEW.status = 'Approved' BEGIN SELECT RAISE(ABORT, 'late approval status failure'); END")
                before = self.taskFourSnapshot()
                report = self.owner.get("/api/dashboard?period=all").get_json()
                response = self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": selected, "approveAll": approveAll})
                self.assertEqual(500, response.status_code)
                self.assertTrue(before == self.taskFourSnapshot(), "Late approval failed to roll back authoritative state")
                self.assertEqual(report, self.owner.get("/api/dashboard?period=all").get_json())
                self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
                with self.connection:
                    self.connection.execute("DROP TRIGGER taskFourApprovalFailure")
        self.assertEqual(200, self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": rows, "approveAll": True}).status_code)
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testLateRejectionStatusFailureRollsBackEntireBatch(self):
        first = self.submit().get_json()["id"]
        second = self.submit({**self.proposed, "submissionId": "reject-second"}).get_json()["id"]
        with self.connection:
            self.connection.execute(f"CREATE TRIGGER taskFourRejectionFailure BEFORE UPDATE OF status ON pendingSubmissions WHEN NEW.id = {second} AND NEW.status = 'Rejected' BEGIN SELECT RAISE(ABORT, 'late rejection status failure'); END")
        before = self.taskFourSnapshot()
        report = self.owner.get("/api/dashboard?period=all").get_json()
        request = {"decision": "reject", "rows": [{"id": first, "reviewVersion": 0}, {"id": second, "reviewVersion": 0}]}
        self.assertEqual(500, self.owner.post("/api/pending/decision", json=request).status_code)
        self.assertTrue(before == self.taskFourSnapshot(), "Partial rejection persisted after batch failure")
        self.assertEqual(report, self.owner.get("/api/dashboard?period=all").get_json())
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        with self.connection:
            self.connection.execute("DROP TRIGGER taskFourRejectionFailure")
        self.assertEqual(200, self.owner.post("/api/pending/decision", json=request).status_code)
        self.assertEqual(["Rejected", "Rejected"], [row[0] for row in self.connection.execute("SELECT status FROM pendingSubmissions ORDER BY id")])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testPublicDocumentationAndNoOwnerExposure(self):
        emptyApp = createApp(Path(self.directory.name) / "empty.db")
        client = emptyApp.test_client()
        for route in ["/", "/new", "/transactions", "/settings", "/budget", "/simulator", "/pending", "/sign-in"]:
            response = client.get(route)
            self.assertEqual(503, response.status_code)
            self.assertIn("not configured", response.get_data(as_text=True))
            self.assertIn("An Owner is required", response.get_data(as_text=True))
            self.assertIn("choose Create Owner", response.get_data(as_text=True))
            self.assertIn("reload this page", response.get_data(as_text=True))
            self.assertEqual("noindex, nofollow, noarchive", response.headers["X-Robots-Tag"])
        for route in ["/api/accounts", "/api/dashboard", "/api/backup", "/api/integration/read/accounts", "/health"]:
            self.assertEqual(503, client.get(route).status_code)
        for route in ["/docs", "/openapi.json", "/robots.txt"]:
            with client.get(route) as response:
                self.assertEqual(200, response.status_code)

    def testLoginCsrfFixedExpiryAndLogout(self):
        visitor = self.app.test_client()
        self.assertEqual(302, visitor.get("/").status_code)
        self.assertEqual(401, visitor.get("/api/accounts").status_code)
        self.assertEqual(403, visitor.post("/sign-in", data={"username": "Owner", "password": "owner password"}).status_code)
        with patch("app.time.time", return_value=1000000):
            self.assertEqual(302, signIn(visitor, " owner ").status_code)
            details = visitor.get("/api/session").get_json()
            self.assertEqual(1000000 + 168 * 3600, details["expiresAt"])
        with patch("app.time.time", return_value=1000000 + 168 * 3600 - 1):
            response = visitor.get("/api/session")
            self.assertEqual(details["expiresAt"], response.get_json()["expiresAt"])
            self.assertNotIn("Set-Cookie", response.headers)
        with patch("app.time.time", return_value=details["expiresAt"]):
            self.assertEqual(401, visitor.get("/api/session").status_code)
        self.assertEqual(302, signIn(visitor).status_code)
        visitor.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, visitor.post("/api/logout").status_code)
        visitor.environ_base["HTTP_X_CSRF_TOKEN"] = visitor.get("/api/session").get_json()["csrfToken"]
        self.assertEqual(200, visitor.post("/api/logout").status_code)
        self.assertEqual(401, visitor.get("/api/accounts").status_code)

    def testCookiesConcurrencyResetRestartAndAuditorRemoval(self):
        other = self.app.test_client()
        self.assertEqual(302, signIn(other).status_code)
        self.assertEqual(200, self.owner.get("/api/session").status_code)
        resetPassword(self.connection, self.connection.execute("SELECT id FROM scopedusers WHERE role = 'owner'").fetchone()[0], "new password")
        self.assertEqual(401, self.owner.get("/api/session").status_code)
        self.assertEqual(401, other.get("/api/session").status_code)
        self.assertEqual(302, signIn(other, password="new password").status_code)
        restarted = createApp(self.path).test_client()
        restarted.set_cookie("npfSession", other.get_cookie("npfSession").value)
        self.assertEqual(401, restarted.get("/api/session").status_code)
        secure = self.app.test_client()
        response = secure.get("/sign-in", base_url="https://localhost")
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        with self.connection:
            self.connection.execute("DELETE FROM users WHERE id IN (SELECT userId FROM ledgerAccess WHERE role = 'auditor')")
        createUser(self.connection, "auditor", "Auditor", "replacement password")
        self.assertEqual(401, auditor.get("/api/session").status_code)

    def testProxyAuthorizationDoesNotReplaceBrowserLoginOrCsrf(self):
        basic = {"Authorization": "Basic ZmljdGlvbmFsOnByb3h5"}
        visitor = self.app.test_client()
        visitor.environ_base["HTTP_AUTHORIZATION"] = basic["Authorization"]
        self.assertEqual(302, visitor.get("/").status_code)
        self.assertEqual(401, visitor.get("/api/accounts").status_code)
        self.assertEqual(401, visitor.get("/api/integration/read/accounts").status_code)
        self.assertEqual(302, signIn(visitor).status_code)
        with visitor.get("/") as response:
            self.assertEqual(200, response.status_code)
        self.assertEqual(200, visitor.get("/api/accounts").status_code)
        visitor.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, visitor.post("/api/integration-secrets", json={"label": "Blocked"}).status_code)
        auditor = self.app.test_client()
        auditor.environ_base["HTTP_AUTHORIZATION"] = basic["Authorization"]
        signIn(auditor, "Auditor", "auditor password")
        self.assertEqual(200, auditor.get("/api/accounts").status_code)
        self.assertEqual(403, auditor.post("/api/accounts", json={}).status_code)
        for scheme in ["Bearer", "bearer", "bEaReR"]:
            header = {"Authorization": scheme + " " + self.secret}
            self.assertEqual(200, self.agent.get("/api/integration/read/accounts", headers=header).status_code)
            self.assertEqual(403, visitor.get("/api/accounts", headers=header).status_code)
            self.assertEqual(403, visitor.get("/sign-in", headers=header).status_code)
        self.assertEqual(403, visitor.get("/api/accounts", headers={"Authorization": "Bearer"}).status_code)
        self.assertEqual(401, self.agent.get("/api/integration/read/accounts", headers={"Authorization": "bearer invalid"}).status_code)

    def testGenericFailureAndThrottling(self):
        client = self.app.test_client()
        for username in ["Owner", "does not exist", "Owner", "Owner", "Owner"]:
            response = signIn(client, username, "wrong password")
            self.assertEqual(401, response.status_code)
            self.assertIn("Invalid username or password.", response.get_data(as_text=True))
        self.assertEqual(429, signIn(client).status_code)

    def testEveryExistingWriteRejectsAuditorAndIntegration(self):
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint in {"signIn", "logOut", "evaluateMoneyExpression", "accountMinimumHelper", "accountUtilizationWarningHelper", "integrationSubmissions", "updateIntegrationPending"}:
                continue
            url = re.sub(r"<[^>]+>", "1", str(rule))
            for method in rule.methods & {"POST", "PATCH", "PUT", "DELETE"}:
                with self.subTest(route=url, method=method):
                    self.assertEqual(403, auditor.open(url, method=method, json={}).status_code)
                    self.assertEqual(403, self.agent.open(url, method=method, json={}, headers=self.headers).status_code)
        for url in ["/api/backup", "/api/package", "/api/integration-secrets", "/api/pending", "/pending"]:
            self.assertEqual(403, auditor.get(url).status_code)
        for url in ["/", "/transactions", "/settings", "/budget", "/simulator", "/api/accounts", "/api/dashboard", "/api/simulator"]:
            with auditor.get(url) as response:
                self.assertEqual(200, response.status_code)
        self.assertEqual(403, self.agent.get("/api/accounts", headers=self.headers).status_code)

    def testReadContractAndPaginationWithoutSecretsOrExternalBalances(self):
        for operationPath, operations in json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text()).get("paths").items():
            if "read/" not in operationPath:
                continue
            response = self.agent.get(operationPath, headers=self.headers)
            self.assertEqual(200, response.status_code, operationPath)
            self.assertNotIn(self.secret, response.get_data(as_text=True))
            self.assertEqual(401, self.agent.get(operationPath).status_code)
            self.assertEqual(400, self.agent.get(operationPath + "?unknown=1", headers=self.headers).status_code)
        accounts = self.agent.get("/api/integration/read/accounts", headers=self.headers).get_json()
        external = next(account for account in accounts if account["accountClass"] == "system")
        self.assertNotIn("balanceCents", external)
        self.assertNotIn("balanceOffsetCents", external)
        self.assertFalse(external["ordinaryEligible"])
        for amount in ["10.00", "-2.00"]:
            createTransaction(self.connection, self.cashId, "2026-10-01", "Example", self.expenseId, amount)
        result = self.agent.get("/api/integration/read/transactions?period=all&limit=1&includeSummary=true", headers=self.headers).get_json()
        self.assertEqual(2, result["total"])
        self.assertEqual(1, result["nextOffset"])
        self.assertEqual(800, result["summary"]["expenseCents"])
        self.assertEqual(1, len(result["transactions"]))
        for query in ["limit=501", "offset=-1", "limit=letters", "includeSummary=yes", "amountMode=exact&amount=letters"]:
            self.assertEqual(400, self.agent.get("/api/integration/read/transactions?" + query, headers=self.headers).status_code)

    def testPendingHasNoLedgerEffectAndIdentitySurvivesCorrectionRestart(self):
        initialBalance = self.owner.get("/api/accounts").get_json()
        result = self.submit()
        self.assertEqual(201, result.status_code)
        proposal = result.get_json()
        self.assertEqual("Pending", proposal["status"])
        self.assertEqual(initialBalance, self.owner.get("/api/accounts").get_json())
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual(200, self.submit().status_code)
        self.assertEqual(409, self.submit({**self.proposed, "amount": "51.00"}).status_code)
        approval = self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": [self.approveRow(proposal["id"], amount="-20.00")]})
        self.assertEqual(200, approval.status_code)
        self.assertEqual(2000, self.connection.execute("SELECT amountCents FROM transactions").fetchone()[0])
        restarted = createApp(self.path).test_client()
        retried = restarted.post("/api/integration/submissions", json=self.proposed, headers=self.headers).get_json()
        self.assertEqual(proposal["id"], retried["id"])
        self.assertEqual("Approved", retried["status"])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual(409, self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": [self.approveRow(proposal["id"])]}).status_code)

    def testRejectRevocationAndStatusIsolation(self):
        proposal = self.submit().get_json()
        self.assertEqual(200, self.owner.post("/api/pending/decision", json={"decision": "reject", "rows": [{"id": proposal["id"]}]}).status_code)
        self.assertEqual("Rejected", self.submit().get_json()["status"])
        secondId = createIntegrationSecret(self.connection, "Other Agent")
        secondSecret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id=?", (secondId,)).fetchone()[0]
        secondHeaders = {"Authorization": "Bearer " + secondSecret}
        self.assertEqual(0, self.agent.get("/api/integration/submissions", headers=secondHeaders).get_json()["total"])
        self.assertEqual(201, self.agent.post("/api/integration/submissions", json=self.proposed, headers=secondHeaders).status_code)
        self.assertEqual(200, self.owner.delete(f"/api/integration-secrets/{self.secretId}").status_code)
        self.assertEqual(401, self.submit().status_code)
        restarted = createApp(self.path).test_client()
        self.assertEqual(401, restarted.get("/api/integration/read/accounts", headers=self.headers).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testSecretCreationRecoveryAndRestrictions(self):
        created = self.owner.post("/api/integration-secrets", json={"label": "Second Example Agent"})
        self.assertEqual(201, created.status_code)
        secretId = created.get_json()["id"]
        recovered = self.owner.post(f"/api/integration-secrets/{secretId}/reveal").get_json()["secret"]
        self.assertGreaterEqual(len(recovered), 43)
        self.assertNotIn(recovered, self.owner.get("/api/integration-secrets").get_data(as_text=True))
        restarted = createApp(self.path)
        owner = restarted.test_client()
        signIn(owner)
        self.assertEqual(recovered, owner.post(f"/api/integration-secrets/{secretId}/reveal").get_json()["secret"])
        self.assertEqual(403, restarted.test_client().post(f"/api/integration-secrets/{secretId}/reveal", headers=self.headers).status_code)
        self.assertEqual(400, owner.post("/api/integration-secrets", json={"label": " "}).status_code)
        self.assertEqual(400, owner.post("/api/integration-secrets", json=[]).status_code)

    def testRelabelPreservesCredentialsProposalIdentityAndRevocation(self):
        proposalId = self.submit().get_json()["id"]
        proposalBefore = tuple(self.connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (proposalId,)).fetchone())
        originalSecret = dict(self.connection.execute("SELECT * FROM integrationSecrets WHERE id = ?", (self.secretId,)).fetchone())
        url = f"/api/integration-secrets/{self.secretId}"
        self.assertEqual(200, self.owner.patch(url, json={"label": "  Renamed <Agent>  "}).status_code)
        updatedSecret = dict(self.connection.execute("SELECT * FROM integrationSecrets WHERE id = ?", (self.secretId,)).fetchone())
        self.assertEqual({**originalSecret, "label": "Renamed <Agent>"}, updatedSecret)
        self.assertEqual("Renamed <Agent>", self.owner.get("/api/pending").get_json()["rows"][0]["label"])
        self.assertEqual(proposalBefore, tuple(self.connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (proposalId,)).fetchone()))
        self.assertEqual(proposalId, self.submit().get_json()["id"])
        for invalid in [{"label": " "}, {"label": "x" * 129}, {"label": 123}, [], {"label": "New", "secret": "replace"}]:
            self.assertEqual(400, self.owner.patch(url, json=invalid).status_code)
        self.assertEqual(400, self.owner.patch("/api/integration-secrets/999999", json={"label": "Missing"}).status_code)
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        self.assertEqual(403, auditor.patch(url, json={"label": "Forbidden"}).status_code)
        self.assertEqual(403, self.agent.patch(url, json={"label": "Forbidden"}, headers=self.headers).status_code)
        self.owner.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, self.owner.patch(url, json={"label": "No CSRF"}).status_code)
        signIn(self.owner)
        self.assertEqual(200, self.owner.delete(url).status_code)
        self.assertEqual("Renamed <Agent>", self.owner.get("/api/pending").get_json()["rows"][0]["label"])
        self.assertEqual(200, self.owner.patch(url, json={"label": "Revoked label"}).status_code)
        restarted = createApp(self.path)
        owner = restarted.test_client()
        signIn(owner)
        persisted = next(row for row in owner.get("/api/integration-secrets").get_json() if row["id"] == self.secretId)
        self.assertEqual("Revoked label", persisted["label"])
        self.assertEqual(0, persisted["active"])
        pending = owner.get("/api/pending").get_json()["rows"]
        self.assertEqual([(proposalId, "Revoked label")], [(row["id"], row["label"]) for row in pending])
        self.assertEqual(self.secret, owner.post(url + "/reveal").get_json()["secret"])
        self.assertEqual(401, restarted.test_client().get("/api/integration/read/accounts", headers=self.headers).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testHistoryGroupingArchivedRowsAndInvalidTransferSigns(self):
        transferId = createTransfer(self.connection, "2026-10-01", self.cashId, self.creditId, "10.00", "Example payment")
        createTransaction(self.connection, self.creditId, "2026-10-01", "Example refund", self.expenseId, "-2.00")
        for selectedAccount in [self.cashId, self.creditId]:
            result = self.agent.get(f"/api/integration/read/transactions?period=all&type=transfers&accountId={selectedAccount}", headers=self.headers).get_json()
            self.assertEqual(1, result["total"])
            self.assertTrue(result["transactions"][0]["combinedTransfer"])
            self.assertEqual(1000, result["transactions"][0]["amountCents"])
        with self.connection:
            self.connection.execute("UPDATE accounts SET active=0 WHERE id=?", (self.creditId,))
            self.connection.execute("UPDATE transactions SET description='Inconsistent pair' WHERE transferId=? AND amountCents>0", (transferId,))
        result = self.agent.get("/api/integration/read/transactions?period=all&type=transfers&includeSummary=true", headers=self.headers).get_json()
        self.assertEqual(2, result["total"])
        self.assertTrue(all(not row["combinedTransfer"] for row in result["transactions"]))
        self.assertCountEqual([-1000, 1000], [row["displayAmountCents"] for row in result["transactions"]])
        self.assertEqual(0, result["summary"]["transferPaymentCents"])
        accounts = self.agent.get("/api/integration/read/accounts", headers=self.headers).get_json()
        archived = next(account for account in accounts if account["id"] == self.creditId)
        self.assertEqual(0, archived["active"])
        self.assertFalse(archived["ordinaryEligible"])

    def testMalformedInputDatesSystemAndArchivedEligibility(self):
        for corrections in [{"amount": "letters"}, {"amount": "1.234"}, {"date": "2025-02-29"}, {"date": "2026-2-01"}, {"accountId": True}, {"amount": 50}, {"transferId": "not allowed"}]:
            self.assertEqual(400, self.submit({**self.proposed, **corrections}).status_code)
        externalId = self.connection.execute("SELECT id FROM accounts WHERE accountClass='system'").fetchone()[0]
        self.assertEqual(400, self.submit({**self.proposed, "accountId": externalId}).status_code)
        for index, corrections in enumerate([{"date": "2028-02-29", "amount": "0"}, {"amount": "-12.34"}, {"categoryId": self.incomeId, "amount": "-50.00"}, {"accountId": self.creditId, "amount": "10.00"}]):
            self.assertEqual(201, self.submit({**self.proposed, "submissionId": str(index), **corrections}).status_code)
        proposal = self.submit().get_json()
        with self.connection:
            self.connection.execute("UPDATE accounts SET active=0 WHERE id=?", (self.cashId,))
        self.assertEqual(200, self.submit().status_code)
        self.assertEqual(400, self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": [self.approveRow(proposal["id"])]}).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testSavePendingCorrectionPersistsWithoutLedgerOrRetryChanges(self):
        proposalId = self.submit().get_json()["id"]
        original = self.connection.execute("SELECT originalContent FROM pendingSubmissions WHERE id=?", (proposalId,)).fetchone()[0]
        savedFields = {"date": "2026-09-30", "accountId": self.creditId, "description": "Corrected refund", "categoryId": self.expenseId, "amount": "-20.00", "reviewedRole": "expense", "reviewVersion": 0}
        self.assertEqual(200, self.owner.patch(f"/api/pending/{proposalId}", json=savedFields).status_code)
        review = self.owner.get("/api/pending").get_json()["rows"][0]
        self.assertEqual(1, review["reviewVersion"])
        for key in ("date", "accountId", "description", "categoryId", "amount"):
            self.assertEqual(savedFields[key], review[key])
        self.assertEqual(2000, review["ledgerAmountCents"])
        self.assertEqual(original, self.connection.execute("SELECT originalContent FROM pendingSubmissions WHERE id=?", (proposalId,)).fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual("Pending", self.submit().get_json()["status"])
        self.assertEqual(409, self.submit({**self.proposed, "amount": "-20.00"}).status_code)
        restarted = createApp(self.path)
        owner = restarted.test_client()
        signIn(owner)
        self.assertEqual(review, owner.get("/api/pending").get_json()["rows"][0])
        decision = {"decision": "approve", "rows": [{"id": proposalId, **savedFields, "reviewVersion": 1}]}
        self.assertEqual(200, owner.post("/api/pending/decision", json=decision).status_code)
        ledger = self.connection.execute("SELECT accountId, description, amountCents FROM transactions").fetchone()
        self.assertEqual((self.creditId, "Corrected refund", 2000), tuple(ledger))
        self.assertEqual(409, owner.patch(f"/api/pending/{proposalId}", json={**savedFields, "reviewVersion": 1}).status_code)

    def testSavePendingValidationAndRoleChangeRollBack(self):
        proposalId = self.submit().get_json()["id"]
        fields = {key: value for key, value in self.approveRow(proposalId).items() if key != "id"}
        fields["reviewVersion"] = 0
        url = f"/api/pending/{proposalId}"
        for invalid in [{**fields, "amount": "letters"}, {**fields, "date": "2026-02-30"}, {**fields, "accountId": 9999}, {**fields, "description": []}, {**fields, "extra": 1}, []]:
            self.assertEqual(400, self.owner.patch(url, json=invalid).status_code)
        updateCategory(self.connection, self.expenseId, "Now Income", reportingRole="income")
        self.assertEqual(409, self.owner.patch(url, json=fields).status_code)
        self.assertEqual(200, self.owner.patch(url, json={**fields, "reviewedRole": "income"}).status_code)
        with self.connection:
            self.connection.execute("UPDATE accounts SET active=0 WHERE id=?", (self.cashId,))
        self.assertEqual(400, self.owner.patch(url, json={**fields, "reviewedRole": "income", "reviewVersion": 1}).status_code)
        self.assertEqual(1, self.connection.execute("SELECT reviewVersion FROM pendingSubmissions WHERE id=?", (proposalId,)).fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testSavedPendingRevisionProtectsConcurrentSavesAndStaleDecisions(self):
        proposalId = self.submit().get_json()["id"]
        fields = {key: value for key, value in self.approveRow(proposalId).items() if key != "id"}
        fields["reviewVersion"] = 0
        barrier = threading.Barrier(2)
        from database import savePendingReview
        def save(description):
            connection = connectDatabase(self.path)
            try:
                barrier.wait()
                try:
                    savePendingReview(connection, proposalId, {**fields, "description": description})
                    return "saved"
                except SubmissionConflict:
                    return "stale"
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(["saved", "stale"], list(pool.map(save, ["First", "Second"])))
        for decision in ["approve", "reject"]:
            self.assertEqual(409, self.owner.post("/api/pending/decision", json={"decision": decision, "rows": [self.approveRow(proposalId)]}).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual(200, self.owner.post("/api/pending/decision", json={"decision": "reject", "rows": [{"id": proposalId, "reviewVersion": 1}]}).status_code)
        self.assertEqual(409, self.owner.patch(f"/api/pending/{proposalId}", json={**fields, "reviewVersion": 1}).status_code)

    def testRoleChangeFlagPreservesAmountAndRequiresReviewedRole(self):
        proposal = self.submit().get_json()
        updateCategory(self.connection, self.expenseId, "Renamed Income", reportingRole="income")
        review = self.owner.get("/api/pending").get_json()["rows"][0]
        self.assertTrue(review["roleChanged"])
        self.assertEqual("50.00", review["amount"])
        self.assertEqual(5000, review["ledgerAmountCents"])
        self.assertEqual(409, self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": [self.approveRow(proposal["id"])]}).status_code)
        self.assertEqual(200, self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": [self.approveRow(proposal["id"], reviewedRole="income")]}).status_code)
        self.assertEqual("Approved", self.submit().get_json()["status"])

    def testApproveAllCreatesSavedQueueOnceWithAuthoritativeSigns(self):
        first = self.submit({**self.proposed, "amount": "-20.00"}).get_json()["id"]
        second = self.submit({**self.proposed, "submissionId": "all-income", "categoryId": self.incomeId, "amount": "30.00"}).get_json()["id"]
        rows = [self.approveRow(first, amount="-20.00"), self.approveRow(second, categoryId=self.incomeId, amount="30.00", reviewedRole="income")]
        request = {"decision": "approve", "rows": rows, "approveAll": True}
        response = self.owner.post("/api/pending/decision", json=request)
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.get_json()["count"])
        self.assertEqual([2000, 3000], [row[0] for row in self.connection.execute("SELECT amountCents FROM transactions ORDER BY id")])
        self.assertEqual(0, self.owner.get("/api/pending").get_json()["count"])
        self.assertEqual(409, self.owner.post("/api/pending/decision", json=request).status_code)
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testApproveAllRejectsUnsavedInvalidStaleOrChangedQueueAtomically(self):
        first = self.submit().get_json()["id"]
        second = self.submit({**self.proposed, "submissionId": "all-second", "description": "Second"}).get_json()["id"]
        rows = [self.approveRow(first), self.approveRow(second, description="Second")]
        request = {"decision": "approve", "rows": rows, "approveAll": True}
        self.assertEqual(409, self.owner.post("/api/pending/decision", json={**request, "rows": rows[:1]}).status_code)
        self.assertEqual(409, self.owner.post("/api/pending/decision", json={**request, "rows": [rows[0], {**rows[1], "amount": "60.00"}]}).status_code)
        self.assertEqual(400, self.owner.post("/api/pending/decision", json={**request, "approveAll": "true"}).status_code)
        self.assertEqual(400, self.owner.post("/api/pending/decision", json={**request, "decision": "reject"}).status_code)
        saved = {key: value for key, value in rows[1].items() if key != "id"}
        saved["reviewVersion"] = 0
        self.assertEqual(200, self.owner.patch(f"/api/pending/{second}", json=saved).status_code)
        self.assertEqual(409, self.owner.post("/api/pending/decision", json=request).status_code)
        rows[1]["reviewVersion"] = 1
        with self.connection:
            self.connection.execute("CREATE TRIGGER forceAllApprovalFailure BEFORE INSERT ON transactions WHEN NEW.description = 'Second' BEGIN SELECT RAISE(ABORT, 'all approval fixture failure'); END")
        self.assertEqual(500, self.owner.post("/api/pending/decision", json=request).status_code)
        with self.connection:
            self.connection.execute("DROP TRIGGER forceAllApprovalFailure")
        updateCategory(self.connection, self.expenseId, "Now Income", reportingRole="income")
        self.assertEqual(409, self.owner.post("/api/pending/decision", json=request).status_code)
        for row in rows:
            row["reviewedRole"] = "income"
        with self.connection:
            self.connection.execute("UPDATE accounts SET active=0 WHERE id=?", (self.cashId,))
        self.assertEqual(400, self.owner.post("/api/pending/decision", json=request).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual(2, self.owner.get("/api/pending").get_json()["count"])

    def testApprovalBatchRollbackAndConcurrentDecisions(self):
        first = self.submit().get_json()["id"]
        second = self.submit({**self.proposed, "submissionId": "example-2"}).get_json()["id"]
        with self.connection:
            self.connection.execute("CREATE TRIGGER forceApprovalFailure BEFORE INSERT ON transactions WHEN NEW.description = 'Fail' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        rows = [self.approveRow(first), self.approveRow(second, description="Fail")]
        self.assertEqual(500, self.owner.post("/api/pending/decision", json={"decision": "approve", "rows": rows}).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual(2, len(pendingReviewRows(self.connection)))
        with self.connection:
            self.connection.execute("DROP TRIGGER forceApprovalFailure")
        barrier = threading.Barrier(2)
        def approve():
            connection = connectDatabase(self.path)
            try:
                barrier.wait()
                try:
                    decidePendingTransactions(connection, "approve", [self.approveRow(first)])
                    return "approved"
                except SubmissionConflict:
                    return "stale"
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: approve(), range(2)))
        self.assertCountEqual(["approved", "stale"], outcomes)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testConcurrentSubmissionIsOneDurableIdentity(self):
        barrier = threading.Barrier(2)
        def submit():
            connection = connectDatabase(self.path)
            try:
                barrier.wait()
                return submitPendingTransaction(connection, self.secretId, self.proposed)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(results[0][0]["id"], results[1][0]["id"])
        self.assertCountEqual([True, False], [result[1] for result in results])

    def testBackupRestoreIncludesExactCredentialAndSubmissionState(self):
        self.submit()
        backupBytes = self.owner.get("/api/backup").get_data()
        backupPath = Path(self.directory.name) / "explicit-backup.db"
        backupPath.write_bytes(backupBytes)
        self.owner.delete(f"/api/integration-secrets/{self.secretId}")
        restored = createApp(backupPath)
        self.assertEqual(200, restored.test_client().get("/api/integration/read/accounts", headers=self.headers).status_code)
        connection = connectDatabase(backupPath)
        try:
            self.assertEqual(1, connection.execute("SELECT active FROM integrationSecrets WHERE id=?", (self.secretId,)).fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM pendingSubmissions").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()

    def testPackagesCarryMatchingVersionAndExactCredentialsOnlyWithDatabase(self):
        from packageApplication import applicationVersion
        response = self.owner.get("/api/package")
        try:
            self.assertEqual(200, response.status_code)
            self.assertIn("26.10", response.headers["Content-Disposition"])
            with zipfile.ZipFile(BytesIO(response.get_data())) as archive:
                copiedPath = Path(self.directory.name) / "package-database.db"
                copiedPath.write_bytes(archive.read("No-Part-Finance/finance.db"))
                self.assertIn('applicationVersion = "26.10.22"', archive.read("No-Part-Finance/packageApplication.py").decode())
                self.assertIn("No-Part-Finance/users.py", archive.namelist())
                self.assertIn("No-Part-Finance/openapi.json", archive.namelist())
            connection = connectDatabase(copiedPath)
            try:
                self.assertEqual(self.secret, connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.secretId,)).fetchone()[0])
            finally:
                connection.close()
        finally:
            response.close()
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        with auditor.get("/api/package-no-db") as response:
            self.assertEqual(200, response.status_code)
            with zipfile.ZipFile(BytesIO(response.get_data())) as archive:
                self.assertNotIn("No-Part-Finance/finance.db", archive.namelist())
                self.assertFalse(any(self.secret.encode() in archive.read(name) for name in archive.namelist()))
        self.assertEqual("26.10.22", applicationVersion)

    def testAuditorAndIntegrationSimulatorReadsNeverWrite(self):
        planId = createSimulatorPlan(self.connection, "Example Plan", "2026-10-01")
        saveSimulatorRow(self.connection, planId, None, "income", toAccountId=self.cashId, label="Example Income", amount="10.00")
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        before = self.path.read_bytes()
        for client, url, headers in [(auditor, "/api/simulator", {}), (self.agent, "/api/integration/read/simulator", self.headers)]:
            self.assertEqual(200, client.get(url, headers=headers).status_code)
        self.assertEqual(before, self.path.read_bytes())


class UserManagementAndMigrationTests(unittest.TestCase):
    def testUsernamePasswordConstraintsAndCasefoldUniqueness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-users.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                for username in ["", "x" * 65, "bad\nname"]:
                    with self.assertRaises(ValidationError):
                        createUser(connection, "owner", username, "valid password")
                for password in ["short", "x" * 129]:
                    with self.assertRaises(ValidationError):
                        createUser(connection, "owner", "Owner", password)
                createUser(connection, "owner", "  Straße  ", "  password α  ")
                self.assertEqual("Straße", connection.execute("SELECT username FROM users").fetchone()[0])
                with self.assertRaises(ValidationError):
                    createUser(connection, "auditor", "STRASSE", "valid password")
                with self.assertRaises(ValidationError):
                    createUser(connection, "owner", "Other", "valid password")
            finally:
                connection.close()

    def testStartupDeclineInterruptionAndOptionalAuditorFailure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-setup.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                for response in ["", "n", EOFError(), KeyboardInterrupt(), OSError()]:
                    with patch("users.input", side_effect=[response]), patch("builtins.print") as output:
                        configureOnStartup(connection)
                    setupText = str(output.call_args_list)
                    self.assertIn("cannot sign in", setupText)
                    self.assertIn("Restart to retry Owner setup", setupText)
                    self.assertIn("startWindows.bat manage-users", setupText)
                    self.assertIn("./startLinux.sh manage-users", setupText)
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                with patch("users.input", side_effect=["y", "Owner", "y", "Auditor"]), patch("users.getpass.getpass", side_effect=["valid password", "valid password", "mismatch", "different"]), patch("builtins.print") as output:
                    configureOnStartup(connection)
                setupText = str(output.call_args_list)
                self.assertIn("Your Owner is saved and can sign in", setupText)
                self.assertIn("Optional Auditor setup did not complete", setupText)
                self.assertIn("Passwords do not match", setupText)
                self.assertNotIn("No Owner was created", setupText)
                self.assertNotIn("valid password", setupText)
                self.assertNotIn("mismatch", setupText)
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
                with connection:
                    connection.execute("DELETE FROM users")
                originalCreateUser = createUser
                def failOptionalAuditor(connection, role, username, password):
                    if role == "auditor":
                        raise sqlite3.OperationalError("fixture write failure")
                    return originalCreateUser(connection, role, username, password)
                with patch("users.input", side_effect=["y", "Owner", "y", "Auditor"]), patch("users.getpass.getpass", return_value="valid password"), patch("users.createUser", side_effect=failOptionalAuditor), patch("builtins.print") as output:
                    configureOnStartup(connection)
                self.assertIn("Your Owner is saved and can sign in", str(output.call_args_list))
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
                with patch("users.input", side_effect=["1", "1", "2", "0", "0"]), patch("users.getpass.getpass", side_effect=["new owner pass", "new owner pass", "auditor pass", "auditor pass", "new auditor pass", "new auditor pass"]), patch("builtins.print") as output:
                    manageUsers(path)
                    text = str(output.call_args_list)
                    self.assertNotIn("new owner pass", text)
                    self.assertNotIn("scrypt:", text)
                    self.assertIn("User management does not start the server", text)
                    self.assertIn("without manage-users", text)
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
            finally:
                connection.close()

    def testProtectedInputFailureAndSuccessfulSetupRetry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-protected-input.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                def unavailablePasswordInput(prompt):
                    warnings.warn("Password input may be echoed", getpass.GetPassWarning)
                    self.fail("Unprotected password input must never proceed")

                with patch("users.input", side_effect=["y", "Example Owner"]), patch("users.getpass.getpass", side_effect=unavailablePasswordInput), patch("builtins.print") as output:
                    configureOnStartup(connection)
                self.assertIn("Protected password input is unavailable", str(output.call_args_list))
                self.assertIn("No Owner was created", str(output.call_args_list))
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

                with patch("users.input", side_effect=["y", "Example Owner", ""]), patch("users.getpass.getpass", return_value="  password α  ") as passwordInput, patch("builtins.print") as output:
                    configureOnStartup(connection)
                setupText = str(output.call_args_list)
                self.assertIn("8–128 characters", setupText)
                self.assertIn("typing shows no characters", setupText)
                self.assertIn("Auditor setup skipped", setupText)
                self.assertNotIn("  password α  ", setupText)
                self.assertEqual([("Password: ",), ("Confirm password: ",)], [call.args for call in passwordInput.call_args_list])
                client = createApp(path).test_client()
                self.assertEqual(302, signIn(client, "Example Owner", "  password α  ").status_code)
                with patch("users.input") as startupInput, patch("builtins.print") as output:
                    configureOnStartup(connection)
                startupInput.assert_not_called()
                output.assert_not_called()

                with patch("users.input", side_effect=["3", "1", "Example Auditor"]), patch("users.getpass.getpass", side_effect=unavailablePasswordInput), patch("builtins.print") as output:
                    manageUsers(path)
                self.assertIn("User management closed", str(output.call_args_list))
                self.assertIn("interactive Command Prompt or terminal", str(output.call_args_list))
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
                with patch("users.input", side_effect=["3", "1", "Example Auditor", "0"]), patch("users.getpass.getpass", return_value="auditor password"), patch("builtins.print"):
                    manageUsers(path)
                self.assertEqual(302, signIn(client, "Example Auditor", "auditor password").status_code)
                self.assertEqual(403, client.post("/api/accounts", json={}).status_code)
            finally:
                connection.close()

    def testStartupExplainsBrowserAccessAfterSkippedOrCompletedSetup(self):
        scenarios = (
            ([""], []),
            (["y", "Example Owner", ""], ["owner"]),
            (["y", "Example Owner", "y", "Example Auditor"], ["auditor", "owner"]),
            (["y", "Example Owner", KeyboardInterrupt()], ["owner"]),
        )
        for responses, expectedRoles in scenarios:
            with self.subTest(expectedRoles=expectedRoles, interrupted=isinstance(responses[-1], KeyboardInterrupt)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "explicit-startup.db"
                with patch.dict("os.environ", {"FINANCE_DATABASE_PATH": str(path)}), patch("sys.argv", ["app.py"]), patch("users.input", side_effect=responses), patch("users.getpass.getpass", return_value="startup password"), patch("werkzeug.serving.make_server") as runServer, patch("builtins.print") as output, self.assertLogs(level="INFO") as startupLogs:
                    runpy.run_path(str(Path(__file__).resolve().parents[1] / "app.py"), run_name="__main__")
                runServer.assert_called_once()
                self.assertEqual(("0.0.0.0", 5000), runServer.call_args.args[:2])
                self.assertEqual({"threaded": True}, runServer.call_args.kwargs)
                runServer.return_value.serve_forever.assert_called_once()
                runServer.return_value.shutdown.assert_called_once()
                runServer.return_value.server_close.assert_called_once()
                logText = "\n".join(startupLogs.output)
                self.assertIn("open http://127.0.0.1:5000", logText)
                self.assertIn("Keep this terminal open", logText)
                self.assertNotIn("startup password", logText + str(output.call_args_list))
                connection = connectDatabase(path)
                try:
                    self.assertEqual(expectedRoles, [row[0] for row in connection.execute("SELECT role FROM scopedusers ORDER BY role")])
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
                    self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                finally:
                    connection.close()

    def testVersion19MigrationPreservesPendingContentCredentialsAndLedger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-v19-migration.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                createUser(connection, "owner", "Migration Owner", "owner password")
                cashId = createAccount(connection, "Migration Cash", "cash", openingBalance="100.00")
                categoryId = createCategory(connection, "Migration Expense")
                createTransaction(connection, cashId, "2026-09-30", "Existing ledger", categoryId, "10.00")
                secretId = createIntegrationSecret(connection, "Migration Agent")
                proposal, _ = submitPendingTransaction(connection, secretId, {"submissionId": "existing", "date": "2026-09-30", "accountId": cashId, "description": "Existing pending", "categoryId": categoryId, "amount": "5.00"})
                useHistoricalSchema21(connection)
                with connection:
                    connection.execute("ALTER TABLE pendingSubmissions DROP COLUMN reviewContent")
                    connection.execute("ALTER TABLE pendingSubmissions DROP COLUMN reviewVersion")
                    useHistoricalSchema21(connection)
                    connection.execute("PRAGMA user_version = 19")
                before = {row[0]: [dict(record) for record in connection.execute('SELECT * FROM "' + row[0] + '"')] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                connection.close()
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                for table, records in before.items():
                    after = [dict(row) for row in connection.execute('SELECT * FROM ' + ('scopedusers' if table == 'users' else '"' + table + '"'))]
                    if table == "pendingSubmissions":
                        self.assertEqual(None, after[0].pop("reviewContent"))
                        self.assertEqual(0, after[0].pop("reviewVersion"))
                    after = [{key: row[key] for key in records[index]} for index, row in enumerate(after)]
                    if table == "sqlite_sequence":
                        records = sorted(records, key=lambda row: row["name"])
                        after = sorted(after, key=lambda row: row["name"])
                    self.assertEqual(records, after, table)
                self.assertEqual(schemaVersion, connection.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()
            backups = list((Path(directory) / "backups").glob("pre-migration-v19-to-v26-*.db"))
            self.assertEqual(1, len(backups))

    def testMigrationPreservesEveryExistingTableAndBackup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-migration.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            cashId = createAccount(connection, "Fixture Cash", "cash", openingBalance="123.45")
            creditId = createAccount(connection, "Fixture Credit", "credit", "Credit Card")
            categoryId = createCategory(connection, "Fixture Expense")
            createTransaction(connection, cashId, "2026-10-01", "Fixture", categoryId, "10.00")
            createTransfer(connection, "2026-10-01", cashId, creditId, "5.00")
            createTemplate(connection, "Fixture Template", cashId, "Fixture", categoryId, "10.00")
            useHistoricalSchema21(connection)
            before = {row[0]: [tuple(record) for record in connection.execute('SELECT * FROM "' + row[0] + '"')] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('users', 'integrationSecrets', 'pendingSubmissions', 'sqlite_sequence')")}
            with connection:
                connection.execute("DROP TABLE pendingSubmissions")
                connection.execute("DROP TABLE integrationSecrets")
                connection.execute("DROP TABLE users")
                useHistoricalSchema21(connection)
                connection.execute("PRAGMA user_version = 18")
            connection.close()
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                self.assertEqual(schemaVersion, connection.execute("PRAGMA user_version").fetchone()[0])
                for table, records in before.items():
                    self.assertEqual(records, [tuple(row)[1:] for row in connection.execute('SELECT * FROM "' + table + '"')])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()
            backups = list((Path(directory) / "backups").glob("pre-migration-v18-to-v26-*.db"))
            self.assertEqual(1, len(backups))
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(18, backup.execute("PRAGMA user_version").fetchone()[0])
            finally:
                backup.close()
