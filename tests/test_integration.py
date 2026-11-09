"""Integration requests, authentication, Pending decisions, and retry integrity."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import snapshotWithoutLoginTime
import json
import re
import sqlite3
import tempfile
import threading
import zipfile
from io import BytesIO
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from app import createApp
from database import ValidationError, SubmissionConflict, createAccount, createCategory, createTransaction, createTransfer, createSimulatorPlan, saveSimulatorRow, createIntegrationSecret, submitPendingEntry, decidePendingTransactions, pendingReviewRows, updateCategory
from users import resetPassword
import database as ledger
from users import createUser
from tests.test_support import connectFixtureDatabase as connectDatabase
from tests.test_support import signIn

class IntegrationApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test-finance.db"
        self.app = createApp(self.path)
        self.app.config["PACKAGE_OUTPUT_DIRECTORY"] = str(Path(self.directory.name) / "packages")
        self.connection = connectDatabase(self.path)
        createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
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
                        self.assertEqual(403 if rule.endpoint in {"register", "forgotPassword", "resetPassword"} else 302 if rule.endpoint in {"completeProfile", "verifyCode", "resendCode", "registrationWelcome"} else 401, visitor.open(url, method=method, json={}).status_code)
                        for token in (None, "incorrect-token", otherToken):
                            headers = {} if token is None else {"X-CSRF-Token": token}
                            response = self.owner.open(url, method=method, json={}, headers=headers)
                            self.assertEqual(403, response.status_code)
                            if rule.endpoint in {"register", "forgotPassword", "resetPassword"}:
                                self.assertIn("disabled", response.get_data(as_text=True))
                            else:
                                self.assertIn("CSRF", response.get_json()["error"])
                            self.assertTrue(before == self.taskFourSnapshot(), "Rejected CSRF changed database state")
                        self.assertEqual(403, self.agent.open(url, method=method, json={}, headers=self.headers).status_code)
                        if rule.endpoint not in {"evaluateMoneyExpression", "accountMinimumHelper", "accountUtilizationWarningHelper", "logOut", "ledgers", "invitationDecision"}:
                            self.assertEqual(302 if rule.endpoint in {"completeProfile", "verifyCode", "registrationWelcome"} else 400 if rule.endpoint in {"ledgers", "invitationDecision", "resendCode", "saveOwnName", "saveOwnPassword", "revokeOwnSessions", "disableOwnNotifications", "renameOwnNotificationDevice"} else 403, auditor.open(url, method=method, json={}).status_code)
                        self.assertTrue(before == self.taskFourSnapshot(), "Unauthorized write changed database state")
        finally:
            self.owner.environ_base["HTTP_X_CSRF_TOKEN"] = validToken
        self.assertEqual(200, self.owner.get("/api/session").status_code)
        # The integration submission boundary intentionally uses Bearer authentication, not browser CSRF.
        self.assertEqual(201, self.submit().status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testTemplateCataloguePreservesEligibilityWithOneJoinedRead(self):
        import app as applicationModule
        archivedAccountId = createAccount(self.connection, "Archived Cash", "cash")
        archivedCategoryId = createCategory(self.connection, "Archived Expense")
        parentId = createCategory(self.connection, "Rollup")
        childId = createCategory(self.connection, "Child", parentId)
        externalId = self.connection.execute("SELECT id FROM accounts WHERE systemIdentity='external'").fetchone()[0]
        expected = {}
        def template(name, accountId, categoryId, eligible, role=None, active=True):
            templateId = ledger.createTemplate(self.connection, name, accountId, None, categoryId, None)
            if not active:
                with self.connection:
                    self.connection.execute("UPDATE transactionTemplates SET active=0 WHERE id=?", (templateId,))
            expected[templateId] = (eligible, role)
            return templateId
        template("Partial", None, None, True)
        template("Expense", self.cashId, self.expenseId, True, "expense")
        template("Income", self.creditId, self.incomeId, True, "income")
        template("No Account", None, self.expenseId, True, "expense")
        template("No Category", self.cashId, None, True)
        template("Archived Account", archivedAccountId, self.incomeId, False, "income")
        template("Archived Category", self.cashId, archivedCategoryId, False)
        template("Inactive Template", self.cashId, self.incomeId, False, "income", False)
        rollupTemplateId = template("Historical rollup reference", self.cashId, childId, False)
        systemTemplateId = template("Historical System reference", self.cashId, self.expenseId, False, "expense")
        with self.connection:
            self.connection.execute("UPDATE accounts SET active=0 WHERE id=?", (archivedAccountId,))
            self.connection.execute("UPDATE categories SET active=0 WHERE id=?", (archivedCategoryId,))
            self.connection.execute("UPDATE transactionTemplates SET categoryId=? WHERE id=?", (parentId, rollupTemplateId))
            self.connection.execute("UPDATE transactionTemplates SET accountId=? WHERE id=?", (externalId, systemTemplateId))
        browserRows = self.owner.get("/api/templates").get_json()
        browserById = {row["id"]: row for row in browserRows}
        statements = []
        rawConnect = applicationModule.connectDatabase
        def tracedConnect(*arguments, **keywords):
            connection = rawConnect(*arguments, **keywords)
            connection.set_trace_callback(statements.append)
            return connection
        before = list(self.connection.iterdump())
        with patch.object(applicationModule, "connectDatabase", tracedConnect):
            response = self.agent.get("/api/integration/read/templates", headers=self.headers)
        self.assertEqual(200, response.status_code)
        rows = response.get_json()
        self.assertEqual(list(browserById), [row["id"] for row in rows])
        self.assertEqual(1, sum("FROM scopedtransactionTemplates AS tt" in statement for statement in statements))
        self.assertFalse(any("SELECT active, accountClass FROM scopedaccounts WHERE id =" in statement or "SELECT c.*, EXISTS(" in statement for statement in statements))
        for row in rows:
            eligible, role = expected[row["id"]]
            self.assertIs(eligible, row["ordinaryEligible"])
            self.assertEqual(role, row.get("reportingRole"))
            if role is None:
                self.assertNotIn("reportingRole", row)
            ordinary = {key: value for key, value in row.items() if key not in ("ordinaryEligible", "reportingRole")}
            self.assertEqual(browserById[row["id"]], ordinary)
            self.assertFalse({"accountActive", "accountClass", "categoryActive", "categoryRole", "hasChildren"} & row.keys())
        self.assertEqual(before, list(self.connection.iterdump()))

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
        # Retained identity audit records now prevent deleting an established User.
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("DELETE FROM users WHERE id IN (SELECT userId FROM ledgerAccess WHERE role = 'auditor')")
        from users import setUserEnabled
        auditorId = self.connection.execute("SELECT id FROM users WHERE username = 'Auditor'").fetchone()[0]
        setUserEnabled(self.connection, auditorId, False)
        createUser(self.connection, "auditor", "Replacement Auditor", "replacement password", firstName="Auditor")
        with self.assertRaises(ValidationError):
            createUser(self.connection, "auditor", "Auditor", "replacement password", firstName="Auditor")
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
                    self.assertEqual(302 if rule.endpoint in {"completeProfile", "verifyCode", "registrationWelcome"} else 400 if rule.endpoint in {"ledgers", "invitationDecision", "resendCode", "saveOwnName", "saveOwnPassword", "revokeOwnSessions", "disableOwnNotifications", "renameOwnNotificationDevice"} else 403, auditor.open(url, method=method, json={}).status_code)
                    self.assertEqual(403, self.agent.open(url, method=method, json={}, headers=self.headers).status_code)
        for url in ["/api/backup", "/api/package", "/api/integration-secrets", "/api/pending"]:
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
                return submitPendingEntry(connection, self.secretId, self.proposed)
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
        self.assertEqual(401, restored.test_client().get("/api/integration/read/accounts", headers=self.headers).status_code)
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
            self.assertIn("26.11.10", response.headers["Content-Disposition"])
            with zipfile.ZipFile(BytesIO(response.get_data())) as archive:
                copiedPath = Path(self.directory.name) / "package-database.db"
                copiedPath.write_bytes(archive.read("No-Part-Finance/finance.db"))
                self.assertIn('applicationVersion = "26.11.10"', archive.read("No-Part-Finance/packageApplication.py").decode())
                self.assertIn("No-Part-Finance/users.py", archive.namelist())
                self.assertIn("No-Part-Finance/openapi.json", archive.namelist())
            connection = connectDatabase(copiedPath)
            try:
                self.assertIsNone(connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.secretId,)).fetchone()[0])
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
        self.assertEqual("26.11.10", applicationVersion)

    def testAuditorAndIntegrationSimulatorReadsNeverWrite(self):
        planId = createSimulatorPlan(self.connection, "Example Plan", "2026-10-01")
        saveSimulatorRow(self.connection, planId, None, "income", toAccountId=self.cashId, label="Example Income", amount="10.00")
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        before = self.path.read_bytes()
        for client, url, headers in [(auditor, "/api/simulator", {}), (self.agent, "/api/integration/read/simulator", self.headers)]:
            self.assertEqual(200, client.get(url, headers=headers).status_code)
        self.assertEqual(before, self.path.read_bytes())

class IntegrationApiFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.directory.name) / "synthetic.db"
        self.backupsPath = Path(self.directory.name) / "backups"
        self.backupsPath.mkdir()
        self.application = createApp(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        self.cash = ledger.createAccount(self.connection, "Example Cash", "cash")
        self.expense = ledger.createCategory(self.connection, "Example Expense")
        self.integrationId = ledger.createIntegrationSecret(self.connection, "Example Agent")
        self.secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.integrationId,)).fetchone()[0]
        self.headers = {"Authorization": "Bearer " + self.secret}
        self.client = self.application.test_client()
        self.limit = self.application.config["MAX_CONTENT_LENGTH"]
        self.proposal = {"submissionId": "example", "date": "2026-10-01", "accountId": self.cash, "categoryId": self.expense, "description": "Example purchase", "amount": "1.00"}

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def snapshot(self):
        return tuple(snapshotWithoutLoginTime(self.connection))

    def bodyWithSize(self, size, content=None):
        encoded = json.dumps(self.proposal if content is None else content).encode("utf-8")
        return encoded + b" " * (size - len(encoded))

class IntegrationRequestSizeTests(IntegrationApiFixture):
    def testOversizedSubmissionUsesDocumentedJsonAndWritesNothing(self):
        before = self.snapshot()
        for body in (self.bodyWithSize(self.limit + 1), b"PRIVATE MALFORMED " + b"x" * self.limit, b"\xc3\xa9" * (self.limit // 2 + 1)):
            with self.subTest(byteLength=len(body)):
                response = self.client.post("/api/integration/submissions", data=body, content_type="application/json", headers=self.headers)
                self.assertEqual(413, response.status_code)
                self.assertEqual("application/json", response.mimetype)
                self.assertEqual({"error": "Request exceeds the 1 MiB limit"}, response.get_json())
                self.assertEqual(before, self.snapshot())

    def testByteBoundaryStillParsesAndValidatesAtLimit(self):
        self.assertEqual(1048576, self.limit)
        response = self.client.post("/api/integration/submissions", data=self.bodyWithSize(self.limit), content_type="application/json", headers=self.headers)
        self.assertEqual(201, response.status_code)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM pendingSubmissions").fetchone()[0])
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=self.bodyWithSize(self.limit, {}), content_type="application/json", headers=self.headers)
        self.assertEqual(400, response.status_code)
        self.assertEqual({"error"}, set(response.get_json()))
        self.assertEqual(before, self.snapshot())

    def testAuthenticationAndOwnerPrecedenceRemainUnchanged(self):
        oversized = self.bodyWithSize(self.limit + 1)
        before = self.snapshot()
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = self.client.post("/api/integration/submissions", data=oversized, content_type="application/json", headers=headers)
            self.assertEqual(401, response.status_code)
            self.assertEqual({"error"}, set(response.get_json()))
        self.assertEqual(before, self.snapshot())
        with self.connection:
            self.connection.execute("UPDATE integrationSecrets SET active = 0 WHERE id = ?", (self.integrationId,))
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=oversized, content_type="application/json", headers=self.headers)
        self.assertEqual(401, response.status_code)
        self.assertEqual(before, self.snapshot())
        self.connection.execute("PRAGMA foreign_keys = OFF")
        with self.connection:
            self.connection.execute("DROP TRIGGER preserveOwnerMembership")
            self.connection.execute("DELETE FROM ledgerAccess")
        self.connection.execute("PRAGMA foreign_keys = ON")
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=oversized, content_type="application/json", headers=self.headers)
        self.assertEqual(503, response.status_code)
        self.assertEqual(before, self.snapshot())

    def testPendingUpdateOversizeLeavesSavedProposalIntact(self):
        proposalId = self.client.post("/api/integration/submissions", json=self.proposal, headers=self.headers).get_json()["id"]
        before = self.snapshot()
        response = self.client.put(f"/api/integration/submissions/{proposalId}", data=self.bodyWithSize(self.limit + 1), content_type="application/json", headers=self.headers)
        self.assertEqual(413, response.status_code)
        self.assertEqual({"error": "Request exceeds the 1 MiB limit"}, response.get_json())
        self.assertEqual(before, self.snapshot())

    def testOrdinaryErrorsAndBrowserHtmlRemainUnchanged(self):
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=b"{invalid", content_type="application/json", headers=self.headers)
        self.assertEqual(400, response.status_code)
        self.assertEqual({"error"}, set(response.get_json()))
        owner = self.application.test_client()
        signIn(owner)
        response = owner.post("/api/transactions", data=self.bodyWithSize(self.limit + 1), content_type="application/json")
        self.assertEqual(413, response.status_code)
        self.assertEqual("text/html", response.mimetype)
        self.assertEqual(before, self.snapshot())

class IntegrationHistoryFilterTests(IntegrationApiFixture):
    def setUp(self):
        super().setUp()
        createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.otherCash = ledger.createAccount(self.connection, "Other Cash", "cash")
        self.purchase = ledger.createTransaction(self.connection, self.cash, "2026-10-01", "Example purchase", self.expense, "10.00")
        self.refund = ledger.createTransaction(self.connection, self.cash, "2026-10-02", "Example refund", self.expense, "-2.00")
        self.transferId = ledger.createTransfer(self.connection, "2026-10-03", self.cash, self.otherCash, "5.00", "Example transfer")
        with self.connection:
            self.connection.execute("INSERT INTO transactions (accountId, date, description, amountCents, transferId) VALUES (?, ?, ?, ?, ?)", (self.cash, "2026-10-04", "Invalid linked row", -300, "invalid-group"))
        self.historyPath = "/api/integration/read/transactions"

    def history(self, **filters):
        return self.client.get(self.historyPath, query_string={"period": "all", **filters}, headers=self.headers)

    def testMalformedSuppliedIdsReturnJson400WithoutWrites(self):
        before = self.snapshot()
        for name in ("accountId", "categoryId"):
            for value in ("", "0", "-1", "-99", "abc", "1.5", "1e2", " ", "null", "1/2"):
                with self.subTest(name=name, value=value):
                    response = self.history(**{name: value, "includeSummary": "true", "description": "Example", "limit": "1", "offset": "0"})
                    self.assertEqual(400, response.status_code)
                    self.assertEqual("application/json", response.mimetype)
                    self.assertEqual({"error": ("Account" if name == "accountId" else "Category") + " ID must be a positive integer"}, response.get_json())
                    self.assertEqual(before, self.snapshot())
        for filters in ({"accountId": self.cash, "categoryId": "0"}, {"accountId": "", "categoryId": self.expense}):
            self.assertEqual(400, self.history(**filters).status_code)

    def testOmittedExistingAndUnavailableIdsPreserveEnvelopeAndSummary(self):
        before = self.snapshot()
        result = self.history(includeSummary="true").get_json()
        self.assertEqual(4, result["total"])
        self.assertEqual({"expenseCents": 800, "incomeCents": 0, "transferPaymentCents": 500}, result["summary"])
        for filters in ({"accountId": 999999}, {"categoryId": 999999}, {"accountId": self.cash, "categoryId": 999999}):
            with self.subTest(filters=filters):
                response = self.history(includeSummary="true", **filters)
                self.assertEqual(200, response.status_code)
                self.assertEqual({"transactions": [], "total": 0, "offset": 0, "limit": 100, "nextOffset": None, "summary": {"expenseCents": 0, "incomeCents": 0, "transferPaymentCents": 0}}, response.get_json())
        result = self.history(accountId=self.cash, categoryId=self.expense, includeSummary="true").get_json()
        self.assertEqual(2, result["total"])
        self.assertEqual({self.purchase, self.refund}, {event["id"] for event in result["transactions"]})
        self.assertEqual(800, result["summary"]["expenseCents"])
        self.assertEqual(before, self.snapshot())

    def testArchivedReferencesStillMatchAndIntegerSyntaxIsPreserved(self):
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.cash,))
            self.connection.execute("UPDATE categories SET active = 0 WHERE id = ?", (self.expense,))
        before = self.snapshot()
        for value in (str(self.cash), f"+{self.cash}", f" {self.cash} ", f"0{self.cash}"):
            response = self.history(accountId=value)
            self.assertEqual(200, response.status_code)
            self.assertEqual(4, response.get_json()["total"])
        result = self.history(accountId=self.cash, categoryId=self.expense).get_json()
        self.assertEqual(2, result["total"])
        self.assertEqual(before, self.snapshot())

    def testTransferSideCategoryExclusionAndInvalidLinkedVisibility(self):
        before = self.snapshot()
        source = self.history(accountId=self.cash).get_json()
        destination = self.history(accountId=self.otherCash).get_json()
        self.assertEqual(1, destination["total"])
        event = destination["transactions"][0]
        self.assertTrue(event["combinedTransfer"])
        self.assertEqual(self.transferId, event["transferId"])
        self.assertIn(event, source["transactions"])
        invalid = next(row for row in source["transactions"] if row["transferId"] == "invalid-group")
        self.assertFalse(invalid["combinedTransfer"])
        self.assertEqual(-300, invalid["displayAmountCents"])
        self.assertEqual(0, self.history(categoryId=self.expense, type="transfers").get_json()["total"])
        self.assertEqual(before, self.snapshot())

    def testOtherFiltersPaginationAndSummaryRemainAuthoritative(self):
        before = self.snapshot()
        filters = {"accountId": self.cash, "categoryId": self.expense, "period": "custom", "startDate": "2026-10-01", "endDate": "2026-10-02", "type": "transactions", "description": "EXAMPLE", "amountMode": "between", "amountMin": "1.00", "amountMax": "10.00", "sort": "amount", "direction": "desc", "includeSummary": "true", "limit": 1}
        first = self.history(**filters).get_json()
        self.assertEqual((2, 1, self.purchase), (first["total"], first["nextOffset"], first["transactions"][0]["id"]))
        second = self.history(**filters, offset=1).get_json()
        self.assertEqual((2, None, self.refund), (second["total"], second["nextOffset"], second["transactions"][0]["id"]))
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual({"expenseCents": 800, "incomeCents": 0, "transferPaymentCents": 0}, first["summary"])
        self.assertEqual(before, self.snapshot())

    def testPermissionsAndBrowserStableIdBehaviorRemainUnchanged(self):
        before = self.snapshot()
        self.assertEqual(401, self.client.get(self.historyPath, query_string={"accountId": "0"}).status_code)
        owner = self.application.test_client()
        signIn(owner)
        auditor = self.application.test_client()
        signIn(auditor, "Auditor", "auditor password")
        for browser in (owner, auditor):
            self.assertEqual(401, browser.get(self.historyPath, query_string={"accountId": self.cash}).status_code)
            self.assertEqual(200, browser.get("/api/transactions?period=all&accountId=0").status_code)
            self.assertEqual([], browser.get("/api/transactions?period=all&accountId=0").get_json())
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
