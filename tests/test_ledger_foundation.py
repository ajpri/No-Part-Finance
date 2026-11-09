"""Ledger isolation, access management, sharing, and audit history."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import LedgerNameFixture, SharingFixture
import json
import re
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import database as ledger
from packageApplication import applicationVersion
from users import passwordHash
import zipfile
from io import BytesIO
from app import createApp
from users import resetPassword, setAuditorAccess, manageUsers, selectRecord, setUserEnabled, manageSelectedUser
from tests.test_support import createPreProfileOwner
from users import createOwner, createUser, renameUser

class LedgerFoundationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.firstLedger = self.connection.ledgerId
        self.ownerId = createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        self.auditorId = createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.account = ledger.createAccount(self.connection, "Shared name", "cash", openingBalance="100")
        self.category = ledger.createCategory(self.connection, "Shared category")
        self.transaction = ledger.createTransaction(self.connection, self.account, "2026-10-20", "First ledger", self.category, "13")
        self.plan = ledger.createSimulatorPlan(self.connection, "First plan", "2026-10-20")
        self.secretId = ledger.createIntegrationSecret(self.connection, "First agent")
        self.secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.secretId,)).fetchone()[0]
        self.secondLedger = str(uuid4())
        with self.connection:
            self.connection.execute("INSERT INTO ledgers VALUES (?, ?)", (self.secondLedger, "Foreign ledger marker"))
            self.foreignOwner = self.connection.execute("INSERT INTO users (username, usernameKey, passwordHash, firstName) VALUES ('Other', 'other', ?, 'Test')", (passwordHash("other password"),)).lastrowid
            self.connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, ?, 'owner')", (self.foreignOwner, self.secondLedger))
        ledger.bindLedger(self.connection, self.secondLedger)
        ledger.createExternalSystemAccount(self.connection)
        self.foreignAccount = ledger.createAccount(self.connection, "Shared name", "cash", openingBalance="900")
        self.foreignCategory = ledger.createCategory(self.connection, "Shared category")
        self.foreignTransaction = ledger.createTransaction(self.connection, self.foreignAccount, "2026-10-20", "FOREIGN FINANCIAL MARKER", self.foreignCategory, "22")
        self.foreignPlan = ledger.createSimulatorPlan(self.connection, "FOREIGN PLAN MARKER", "2026-10-20")
        self.foreignSecretId = ledger.createIntegrationSecret(self.connection, "FOREIGN AGENT MARKER")
        self.foreignSecret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.foreignSecretId,)).fetchone()[0]
        ledger.bindLedger(self.connection, self.firstLedger)
        self.application = createApp(self.path)
        self.client = self.application.test_client()
        self.login(self.client, "Owner", "owner password")

    def login(self, client, username, password, address="127.0.0.1"):
        html = client.get("/sign-in").get_data(as_text=True)
        token = re.search('name="csrfToken" value="([^"]+)"', html).group(1)
        response = client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token}, environ_overrides={"REMOTE_ADDR": address})
        if response.status_code == 302:
            client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return response

    def testReadAndAggregateIsolation(self):
        for endpoint in ("/api/accounts", "/api/categories", "/api/transactions?period=all", "/api/dashboard", "/api/budget", "/api/simulator", "/api/settings", "/api/integration-secrets", "/api/system/database-status"):
            response = self.client.get(endpoint)
            self.assertEqual(200, response.status_code, (endpoint, response.get_data(as_text=True)))
            self.assertNotIn("FOREIGN", response.get_data(as_text=True))
        self.assertEqual(1, self.client.get("/api/system/database-status").get_json()["transactionCount"])
        self.assertEqual(8700, next(row for row in self.client.get("/api/accounts").get_json() if row["id"] == self.account)["balanceCents"])

    def testAccountBalanceQueryRequiresContextAndKeepsSelectedIdsLedgerScoped(self):
        before = tuple(self.connection.iterdump())
        rows = ledger.accountBalances(self.connection, activeOnly=True, accountIds=(self.account, self.foreignAccount))
        self.assertEqual([(self.account, 8700)], [(row["id"], row["balanceCents"]) for row in rows])
        for selectedIds in ((), (self.foreignAccount,), (999999,), ("1) OR 1=1 --",)):
            self.assertEqual([], ledger.accountBalances(self.connection, accountIds=selectedIds))
        response = self.client.get(f"/api/transfer-amount-helper?fromAccountId={self.account}&toAccountId={self.foreignAccount}")
        self.assertEqual({"eligible": False}, response.get_json())
        ledger.bindLedger(self.connection, self.secondLedger)
        try:
            rows = ledger.accountBalances(self.connection, accountIds=(self.account, self.foreignAccount))
            self.assertEqual([(self.foreignAccount, 87800)], [(row["id"], row["balanceCents"]) for row in rows])
        finally:
            ledger.bindLedger(self.connection, self.firstLedger)
        self.assertEqual(before, tuple(self.connection.iterdump()))
        unbound = ledger.connectDatabase(self.path)
        self.addCleanup(unbound.close)
        for selectedIds in (None, ()):
            with self.assertRaisesRegex(ledger.ValidationError, "Authorized ledger context is required"):
                ledger.accountBalances(unbound, accountIds=selectedIds)

    def testKnownForeignIdsAndMixedReferences(self):
        original = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        for transactionId in (self.foreignTransaction, 999999):
            response = self.client.delete(f"/api/transactions/{transactionId}")
            self.assertEqual(400, response.status_code)
        for accountId, categoryId in ((self.foreignAccount, self.category), (self.account, self.foreignCategory)):
            response = self.client.post("/api/transactions", json={"date": "2026-10-20", "description": "Injected", "accountId": accountId, "categoryId": categoryId, "amount": "10", "ledgerId": self.secondLedger})
            self.assertEqual(400, response.status_code)
        self.assertEqual(original, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("INSERT INTO transactions (accountId, categoryId, date, description, amountCents) VALUES (?, ?, '2026-10-20', 'Crossed', -1)", (self.foreignAccount, self.category))

    def testConstraintsAcrossEveryRelationshipAndOwner(self):
        statements = [
            ("UPDATE categories SET parentCategoryId = ? WHERE id = ?", (self.foreignCategory, self.category)),
            ("INSERT INTO transactionTemplates(name,accountId) VALUES ('Crossed', ?)", (self.foreignAccount,)),
            ("INSERT INTO simulatorRows(planId,rowType,rowOrder,fromAccountId) VALUES (?, 'live', 1, ?)", (self.plan, self.foreignAccount)),
            ("INSERT INTO pendingSubmissions(integrationId,submissionId,originalContent,originalRole) VALUES (?, 'crossed', '{}', 'expense')", (self.foreignSecretId,)),
            ("INSERT INTO ledgerAccess(userId,ledgerId,role) VALUES (?, ?, 'owner')", (self.foreignOwner, self.firstLedger)),
            ("UPDATE integrationSecrets SET ledgerId = ? WHERE id = ?", (self.secondLedger, self.secretId)),
        ]
        for statement, values in statements:
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError), self.connection:
                self.connection.execute(statement, values)

    def testAuditAtomicitySecretsAndDeletion(self):
        events = self.connection.execute("SELECT * FROM auditEvents WHERE ledgerId = ?", (self.firstLedger,)).fetchall()
        self.assertTrue(events)
        self.assertNotIn(self.secret, json.dumps([dict(row) for row in events]))
        self.connection.execute("CREATE TRIGGER failAudit BEFORE INSERT ON auditEvents BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
        before = self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.createTransfer(self.connection, "2026-10-20", self.account, self.connection.execute("SELECT id FROM scopedaccounts WHERE systemIdentity='external'").fetchone()[0], "5")
        self.assertEqual(before, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.connection.execute("DROP TRIGGER failAudit")
        ledger.deleteTransaction(self.connection, self.transaction)
        self.assertTrue(self.connection.execute("SELECT 1 FROM auditEvents WHERE recordType = 'transactions' AND recordId = ? AND action = 'delete'", (str(self.transaction),)).fetchone())
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("DELETE FROM auditEvents")

    def testMissingContextAndConcurrentRequests(self):
        raw = ledger.connectDatabase(self.path)
        try:
            with self.assertRaises(ledger.ValidationError):
                ledger.dashboardReport(raw)
        finally:
            raw.close()
        otherClient = self.application.test_client()
        self.assertEqual(302, self.login(otherClient, "Other", "other password").status_code)
        def accountIds(client):
            return {row["id"] for row in client.get("/api/accounts").get_json()}
        with ThreadPoolExecutor(max_workers=2) as executor:
            for first, second in zip(executor.map(accountIds, [self.client] * 8), executor.map(accountIds, [otherClient] * 8)):
                self.assertIn(self.account, first)
                self.assertNotIn(self.foreignAccount, first)
                self.assertIn(self.foreignAccount, second)
                self.assertNotIn(self.account, second)

    def testCurrentMembershipAndRoleAreAuthoritative(self):
        auditorClient = self.application.test_client()
        self.login(auditorClient, "Auditor", "auditor password")
        with self.connection:
            self.connection.execute("UPDATE ledgerAccess SET role = 'member' WHERE userId = ?", (self.auditorId,))
        self.assertEqual(201, auditorClient.post("/api/accounts", json={"name": "Member account", "accountClass": "cash"}).status_code)
        with self.connection:
            self.connection.execute("UPDATE ledgerAccess SET role = 'auditor' WHERE userId = ?", (self.auditorId,))
        self.assertEqual(403, auditorClient.post("/api/accounts", json={"name": "Blocked", "accountClass": "cash"}).status_code)
        with self.connection:
            self.connection.execute("DELETE FROM ledgerAccess WHERE userId = ?", (self.auditorId,))
        self.assertEqual(403, auditorClient.get("/api/accounts").status_code)
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("UPDATE ledgerAccess SET role = 'auditor' WHERE userId = ?", (self.ownerId,))

    def testIntegrationStateAndLedgerScope(self):
        headers = {"Authorization": f"Bearer {self.secret}"}
        endpoint = "/api/integration/read/accounts"
        response = self.client.get(endpoint, headers=headers)
        self.assertEqual(200, response.status_code)
        self.assertNotIn(self.foreignAccount, {row["id"] for row in response.get_json()})
        self.assertEqual(200, self.client.post(f"/api/integration-secrets/{self.secretId}/state", json={"enabled": False}).status_code)
        self.assertEqual(401, self.client.get(endpoint, headers=headers).status_code)
        self.client.post(f"/api/integration-secrets/{self.secretId}/state", json={"enabled": True})
        self.assertEqual(200, self.client.get(endpoint, headers=headers).status_code)
        self.client.delete(f"/api/integration-secrets/{self.secretId}")
        self.assertEqual(400, self.client.post(f"/api/integration-secrets/{self.secretId}/state", json={"enabled": True}).status_code)
        self.assertEqual(401, self.client.get(endpoint, headers=headers).status_code)
        self.assertEqual(400, self.client.post(f"/api/integration-secrets/{self.foreignSecretId}/state", json={"enabled": False}).status_code)

    def testExportFreshPagesStateAndSourcePreservation(self):
        for disable in (False, True):
            sourceBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM auditEvents")]
            if disable:
                for route in ("/api/backup", "/api/package"):
                    response = self.client.get(route + "?disableIntegrationKeys=true")
                    self.assertEqual(400, response.status_code)
                    self.assertEqual("Unsupported download option", response.get_json()["error"])
            response = self.client.get("/api/backup")
            self.assertEqual(200, response.status_code)
            self.assertFalse(b"FOREIGN FINANCIAL MARKER" in response.data)
            self.assertFalse(b"FOREIGN PLAN MARKER" in response.data)
            self.assertFalse(b"FOREIGN AGENT MARKER" in response.data)
            self.assertNotIn(self.foreignSecret.encode(), response.data)
            exportPath = Path(self.directory.name) / f"export-{disable}.db"
            exportPath.write_bytes(response.data)
            exported = ledger.connectDatabase(exportPath)
            try:
                self.assertEqual([self.firstLedger], [row[0] for row in exported.execute("SELECT id FROM ledgers")])
                self.assertEqual({self.ownerId, self.auditorId}, {row[0] for row in exported.execute("SELECT id FROM users")})
                self.assertEqual((None, 1), tuple(exported.execute("SELECT secret, active FROM integrationSecrets").fetchone()))
                self.assertTrue(all(row[0] == 1 for row in exported.execute("SELECT passwordSetupRequired FROM users")))
                self.assertEqual([], exported.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                exported.close()
            standalone = createApp(exportPath).test_client()
            self.assertEqual(401, self.login(standalone, "Owner", "owner password").status_code)
            self.assertEqual(sourceBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM auditEvents")])

    def testUsernameCooldownLastLoginAndVersion(self):
        user = self.connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (self.ownerId,)).fetchone()[0]
        for attempt in range(5):
            with patch("app.time.time", return_value=2000000000):
                response = self.login(self.application.test_client(), " OWNER ", "wrong password", f"10.0.0.{attempt}")
                self.assertEqual(401, response.status_code)
        with patch("app.time.time", return_value=2000000299):
            self.assertEqual(429, self.login(self.application.test_client(), "owner", "owner password", "10.0.1.1").status_code)
        self.assertEqual(user, self.connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (self.ownerId,)).fetchone()[0])
        with patch("app.time.time", return_value=2000000300):
            self.assertEqual(302, self.login(self.application.test_client(), "Owner", "owner password", "10.0.1.2").status_code)
        self.assertNotEqual(user, self.connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (self.ownerId,)).fetchone()[0])
        self.assertIn(f"No Part Finance {applicationVersion}", self.client.get("/sign-in").get_data(as_text=True))
        self.assertEqual(applicationVersion, self.client.get("/openapi.json").get_json()["info"]["version"])


    def testDatabaseRejectsUnscopedForeignWrites(self):
        for statement in ("UPDATE transactions SET amountCents = 999 WHERE id = ?", "DELETE FROM transactions WHERE id = ?"):
            with self.assertRaises(sqlite3.IntegrityError), self.connection:
                self.connection.execute(statement, (self.foreignTransaction,))
        self.assertEqual(-2200, self.connection.execute("SELECT amountCents FROM transactions WHERE id = ?", (self.foreignTransaction,)).fetchone()[0])

    def testAuditIdentifiesIntegrationAndApprovingOwner(self):
        submitted = {"submissionId": "audit-example", "date": "2026-10-20", "accountId": self.account, "categoryId": self.category, "description": "Submitted by integration", "amount": "4.00"}
        agent = self.application.test_client()
        response = agent.post("/api/integration/submissions", json=submitted, headers={"Authorization": f"Bearer {self.secret}"})
        self.assertEqual(201, response.status_code)
        proposalId = response.get_json()["id"]
        submittedEvent = self.connection.execute("SELECT actorKind, actorId, actorLabel FROM auditEvents WHERE recordType = 'pendingSubmissions' AND recordId = ? AND action = 'insert'", (str(proposalId),)).fetchone()
        self.assertEqual(("integration", str(self.secretId), "First agent"), tuple(submittedEvent))
        proposal = self.client.get("/api/pending").get_json()["rows"][0]
        approval = {"id": proposalId, **{key: proposal[key] for key in ledger.pendingFields("transaction")}, "reviewedRole": proposal["currentRole"], "reviewVersion": proposal["reviewVersion"]}
        response = self.client.post("/api/pending/decision", json={"decision": "approve", "rows": [approval]})
        self.assertEqual(200, response.status_code)
        approvedEvent = self.connection.execute("SELECT actorKind, actorId, actorLabel FROM auditEvents WHERE recordType = 'pendingSubmissions' AND recordId = ? AND action = 'update' ORDER BY id DESC", (str(proposalId),)).fetchone()
        self.assertEqual(("user", str(self.ownerId), "Owner"), tuple(approvedEvent))
        self.client.patch(f"/api/integration-secrets/{self.secretId}", json={"label": "Renamed"})
        self.assertEqual("First agent", self.connection.execute("SELECT actorLabel FROM auditEvents WHERE recordType = 'pendingSubmissions' AND recordId = ? AND action = 'insert'", (str(proposalId),)).fetchone()[0])

class LedgerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "historical.db"
        connection = sqlite3.connect(self.path)
        with connection:
            connection.executescript((Path(__file__).parent / "schema21.sql").read_text())
            connection.execute("INSERT INTO accounts (id,name,accountClass,systemIdentity) VALUES (1,'External','system','external')")
            connection.execute("INSERT INTO accounts (id,name,accountClass,balanceOffsetCents) VALUES (2,'Historical Cash','cash',12345)")
            connection.execute("INSERT INTO categories (id,name) VALUES (5,'Historical Expense')")
            connection.execute("INSERT INTO transactions (id,accountId,date,description,amountCents,categoryId) VALUES (40,2,'2020-02-29','History',-321,5)")
            connection.execute("INSERT INTO users (id,role,username,usernameKey,passwordHash,authVersion) VALUES (1,'owner','Historical','historical',?,7)", (passwordHash("historical password"),))
            connection.execute("INSERT INTO integrationSecrets (id,label,secret,active) VALUES (4,'Revoked historical key','synthetic-old-secret',0)")
        self.before = list(connection.iterdump())
        connection.close()

    def testMigrationPreservesHistoryCredentialsAndDoesNotInventAudit(self):
        ledger.initializeDatabase(self.path)
        connection = ledger.connectInitialLedgerDatabase(self.path)
        try:
            ledgerId = connection.ledgerId
            self.assertEqual("Historical’s Finances", connection.execute("SELECT name FROM ledgers").fetchone()[0])
            self.assertEqual((40, -321, "2020-02-29"), tuple(connection.execute("SELECT id,amountCents,date FROM transactions").fetchone()))
            self.assertEqual(12345, connection.execute("SELECT balanceOffsetCents FROM accounts WHERE id=2").fetchone()[0])
            self.assertEqual(("synthetic-old-secret", 0, 1), tuple(connection.execute("SELECT secret,active,revoked FROM integrationSecrets").fetchone()))
            self.assertEqual((7, None), tuple(connection.execute("SELECT authVersion,lastLoginAt FROM users").fetchone()))
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM auditEvents").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(ledgerId, connection.execute("SELECT ledgerId FROM transactions").fetchone()[0])
        finally:
            connection.close()
        ledger.initializeDatabase(self.path)
        connection = ledger.connectInitialLedgerDatabase(self.path)
        try:
            self.assertEqual(ledgerId, connection.ledgerId)
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM ledgerAccess").fetchone()[0])
        finally:
            connection.close()
        backups = list((self.path.parent / "backups").glob("*.db"))
        self.assertEqual(1, len(backups))
        backup = sqlite3.connect(backups[0])
        try:
            self.assertEqual(self.before, list(backup.iterdump()))
        finally:
            backup.close()

    def testLateMigrationFailureRestoresOriginalAndRetainsVerifiedBackup(self):
        with patch.object(ledger, "installAuditTriggers", side_effect=RuntimeError("late schema failure")):
            with self.assertRaisesRegex(RuntimeError, "late schema failure"):
                ledger.initializeDatabase(self.path)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(21, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(self.before, list(connection.iterdump()))
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()
        self.assertEqual(1, len(list((self.path.parent / "backups").glob("*.db"))))

    def testConcurrentAuditorCreationRetainsSeparateMemberships(self):
        ledger.initializeDatabase(self.path)
        import threading
        barrier = threading.Barrier(2)
        def create(index):
            connection = ledger.connectInitialLedgerDatabase(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    createUser(connection, "auditor", f"Auditor {index}", "auditor password", firstName="Test")
                    return "created"
                except ledger.ValidationError:
                    return "rejected"
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(["created", "created"], sorted(executor.map(create, (1, 2))))
        connection = ledger.connectInitialLedgerDatabase(self.path)
        try:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE role = 'auditor'").fetchone()[0])
            self.assertEqual(3, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            connection.close()

class LedgerManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.owner = createOwner(self.connection, "Owner", "owner password", firstName="Owner")
        self.firstLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.owner,)).fetchone()[0]
        ledger.bindLedger(self.connection, self.firstLedger)
        self.auditor = createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.otherAuditor = createUser(self.connection, "auditor", "Other Auditor", "auditor password", firstName="Other Auditor")
        self.application = createApp(self.path)

    def login(self, username="Owner", password="owner password", client=None):
        client = client or self.application.test_client()
        token = re.search('name="csrfToken" value="([^"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        response = client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token})
        if response.status_code == 302:
            client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client, response

    def testSimulatorPlanningPersistsWithoutCreatingAuditEvents(self):
        cash = ledger.createAccount(self.connection, "Cash", "cash")
        category = ledger.createCategory(self.connection, "Groceries")
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM auditEvents ORDER BY id")]
        plan = ledger.createSimulatorPlan(self.connection, "Try a plan", "2026-10-21")
        ledger.updateSimulatorPlan(self.connection, plan, "Renamed plan", "2026-10-22")
        row = ledger.saveSimulatorRow(self.connection, plan, None, "spend", fromAccountId=cash, categoryId=category, label="Groceries", amount="100")
        ledger.saveSimulatorRow(self.connection, plan, row, "spend", fromAccountId=cash, categoryId=category, label="Adjusted groceries", amount="120")
        self.assertEqual(12000, self.connection.execute("SELECT amountCents FROM scopedsimulatorRows WHERE id = ?", (row,)).fetchone()[0])
        duplicate = ledger.createSimulatorPlan(self.connection, "Another experiment", "2026-10-23", duplicatePlanId=plan)
        duplicatedRow = self.connection.execute("SELECT id FROM scopedsimulatorRows WHERE planId = ?", (duplicate,)).fetchone()[0]
        ledger.deleteSimulatorRow(self.connection, duplicate, duplicatedRow)
        ledger.setSimulatorRowExecuted(self.connection, plan, row, True)
        ledger.setSimulatorRowExecuted(self.connection, plan, row, False)
        ledger.setSimulatorRowExecuted(self.connection, plan, row, True)
        ledger.deleteExecutedSimulatorRows(self.connection, plan, 1)
        ledger.deleteSimulatorPlan(self.connection, duplicate)
        ledger.deleteSimulatorPlan(self.connection, plan)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM scopedsimulatorPlans").fetchone()[0])
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM auditEvents ORDER BY id")])

    def testSimulatorLedgerWritesStillAuditAndRollBackTogether(self):
        cash = ledger.createAccount(self.connection, "Cash", "cash")
        savings = ledger.createAccount(self.connection, "Savings", "cash")
        category = ledger.createCategory(self.connection, "Groceries")
        plan = ledger.createSimulatorPlan(self.connection, "Plan", "2026-10-21")
        spend = ledger.saveSimulatorRow(self.connection, plan, None, "spend", fromAccountId=cash, categoryId=category, label="Groceries", amount="100")
        transfer = ledger.saveSimulatorRow(self.connection, plan, None, "transfer", fromAccountId=cash, toAccountId=savings, label="Move", amount="10")
        self.connection.execute("CREATE TRIGGER failFinancialAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'transactions' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit unavailable"):
            ledger.recordSimulatorTransaction(self.connection, plan, spend, cash, "2026-10-21", "Actual groceries", category, "125")
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM scopedtransactions").fetchone()[0])
        self.assertEqual((0, None), tuple(self.connection.execute("SELECT executed, executedTransactionId FROM scopedsimulatorRows WHERE id = ?", (spend,)).fetchone()))
        self.connection.execute("DROP TRIGGER failFinancialAudit")
        transaction = ledger.recordSimulatorTransaction(self.connection, plan, spend, cash, "2026-10-21", "Actual groceries", category, "125")
        self.connection.execute("CREATE TRIGGER failFinancialAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'transactions' AND json_extract(NEW.afterValues, '$.amountCents') > 0 BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit unavailable"):
            ledger.recordSimulatorTransfer(self.connection, plan, transfer, "2026-10-21", cash, savings, "10", "Actual transfer")
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM scopedtransactions").fetchone()[0])
        self.assertEqual((0, None), tuple(self.connection.execute("SELECT executed, executedTransactionId FROM scopedsimulatorRows WHERE id = ?", (transfer,)).fetchone()))
        self.connection.execute("DROP TRIGGER failFinancialAudit")
        transferId, _ = ledger.recordSimulatorTransfer(self.connection, plan, transfer, "2026-10-21", cash, savings, "10", "Actual transfer")
        events = ledger.auditLogPage(self.connection)["rows"]
        self.assertEqual(1, sum(event["type"] == "Transaction" for event in events))
        self.assertEqual(1, sum(event["type"] == "Transfer / Payment" for event in events))
        self.assertFalse(any(event["type"].startswith("Simulator") for event in events))
        self.assertEqual(0, self.connection.execute("SELECT SUM(amountCents) FROM scopedtransactions WHERE transferId = ?", (transferId,)).fetchone()[0])
        ledger.deleteTransaction(self.connection, transaction)
        self.assertEqual((0, None), tuple(self.connection.execute("SELECT executed, executedTransactionId FROM scopedsimulatorRows WHERE id = ?", (spend,)).fetchone()))

    def testSimulatorAuditMigrationPreservesHistoryAndRollsBackFailure(self):
        # Recreate the six v25 planning audit triggers on a guarded synthetic database.
        for table in ("simulatorPlans", "simulatorRows"):
            columns = [row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})") if row["name"] != "ledgerId"]
            for action in ("INSERT", "UPDATE", "DELETE"):
                prefix = "OLD" if action == "DELETE" else "NEW"
                before = "NULL" if action == "INSERT" else "json_object(" + ", ".join(f"'{column}', OLD.{column}" for column in columns) + ")"
                after = "NULL" if action == "DELETE" else "json_object(" + ", ".join(f"'{column}', NEW.{column}" for column in columns) + ")"
                self.connection.execute(f"""CREATE TRIGGER audit{table}{action} AFTER {action} ON {table}
                    BEGIN INSERT INTO auditEvents (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, beforeValues, afterValues, operationId)
                    VALUES ({prefix}.ledgerId, '2026-10-21T12:00:00Z', auditActorKind(), auditActorId(), auditActorLabel(), '{action.lower()}', '{table}', {prefix}.id, {before}, {after}, auditOperationId()); END""")
        account = ledger.createAccount(self.connection, "Cash", "cash", openingBalance="50")
        category = ledger.createCategory(self.connection, "Groceries")
        ledger.createTransaction(self.connection, account, "2026-10-21", "Existing purchase", category, "10")
        plan = ledger.createSimulatorPlan(self.connection, "Existing plan", "2026-10-21")
        row = ledger.saveSimulatorRow(self.connection, plan, None, "spend", fromAccountId=account, categoryId=category, label="Existing row", amount="20")
        self.connection.execute("DROP TRIGGER invalidateBrowserSessions")
        self.connection.execute("DROP TABLE browserSessions")
        self.connection.execute("PRAGMA user_version = 25")
        self.connection.commit()
        beforeDump = list(self.connection.iterdump())
        beforeRows = {table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table}")] for table in (*ledger.ledgerTables, "auditEvents", "users", "ledgerAccess")}
        originalMigration = ledger.migrateToVersion26
        def failingMigration(connection):
            originalMigration(connection)
            raise RuntimeError("synthetic planning migration failure")
        with patch("database.migrateToVersion26", side_effect=failingMigration):
            with self.assertRaisesRegex(RuntimeError, "synthetic planning migration failure"):
                ledger.initializeDatabase(self.path)
        self.assertEqual(beforeDump, list(self.connection.iterdump()))
        with patch.object(ledger, "migrateToVersion27"), patch.object(ledger, "migrateToVersion28"):
            ledger.initializeDatabase(self.path)
        self.assertEqual(31, self.connection.execute("PRAGMA user_version").fetchone()[0])
        for table, recordedRows in beforeRows.items():
            self.assertEqual(recordedRows, [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table}")], table)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name GLOB 'auditsimulator*'").fetchone()[0])
        self.assertTrue(any(event["type"] == "Simulator row" for event in ledger.auditLogPage(self.connection)["rows"]))
        ledger.saveSimulatorRow(self.connection, plan, row, "spend", fromAccountId=account, categoryId=category, label="New experiment", amount="30")
        self.assertEqual(beforeRows["auditEvents"], [tuple(row) for row in self.connection.execute("SELECT * FROM auditEvents")])
        ledger.initializeDatabase(self.path)
        self.assertEqual(beforeRows["auditEvents"], [tuple(row) for row in self.connection.execute("SELECT * FROM auditEvents")])

    def testProvisioningPreservesInitialAndCreatesIndependentLedger(self):
        account = ledger.createAccount(self.connection, "Local", "cash", openingBalance="12.34")
        secondOwner = createOwner(self.connection, "Second", "second password", firstName="Second")
        secondLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (secondOwner,)).fetchone()[0]
        self.assertNotEqual(self.firstLedger, secondLedger)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM auditEvents WHERE ledgerId = ? AND recordType = 'ledgers' AND action = 'create'", (secondLedger,)).fetchone()[0])
        ledger.bindLedger(self.connection, secondLedger)
        self.assertEqual([("External", "system")], [tuple(row) for row in self.connection.execute("SELECT name, accountClass FROM scopedaccounts")])
        for table in ledger.ledgerTables:
            if table != "accounts":
                self.assertEqual(0, self.connection.execute(f"SELECT COUNT(*) FROM scoped{table}").fetchone()[0], table)
        ledger.bindLedger(self.connection, self.firstLedger)
        self.assertEqual(1234, self.connection.execute("SELECT balanceOffsetCents FROM scopedaccounts WHERE id = ?", (account,)).fetchone()[0])
        self.assertEqual(self.firstLedger, self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.owner,)).fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        initialPath = Path(self.directory.name) / "initial-history.db"
        ledger.initializeDatabase(initialPath)
        initialConnection = ledger.connectInitialLedgerDatabase(initialPath)
        try:
            originalLedger = initialConnection.ledgerId
            existingAccount = ledger.createAccount(initialConnection, "Existing history", "cash", openingBalance="25")
            initialOwner = createOwner(initialConnection, "Initial", "owner password", firstName="Initial")
            self.assertEqual([(originalLedger,)], [tuple(row) for row in initialConnection.execute("SELECT id FROM ledgers")])
            self.assertEqual(originalLedger, initialConnection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (initialOwner,)).fetchone()[0])
            self.assertEqual(2500, initialConnection.execute("SELECT balanceOffsetCents FROM scopedaccounts WHERE id = ?", (existingAccount,)).fetchone()[0])
        finally:
            initialConnection.close()

    def testProvisioningAndChangesRollbackWithAuditFailure(self):
        original = list(self.connection.iterdump())
        self.connection.execute("CREATE TRIGGER failAudit BEFORE INSERT ON auditEvents BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        baseline = list(self.connection.iterdump())
        for operation in (
            lambda: createOwner(self.connection, "Second", "second password", firstName="Second"),
            lambda: renameUser(self.connection, self.auditor, "Renamed"),
            lambda: setAuditorAccess(self.connection, self.auditor, False),
            lambda: resetPassword(self.connection, self.owner, "new password"),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                operation()
            self.assertEqual(baseline, list(self.connection.iterdump()))
        self.connection.execute("DROP TRIGGER failAudit")
        self.assertEqual(original, list(self.connection.iterdump()))

    def testAccessRetainsIdentityRequiresFreshLoginAndTargetsOnlyOneAuditor(self):
        first, _ = self.login("Auditor", "auditor password")
        second, _ = self.login("Other Auditor", "auditor password")
        setAuditorAccess(self.connection, self.auditor, False)
        self.assertEqual(401, first.get("/api/accounts").status_code)
        self.assertEqual(200, second.get("/api/accounts").status_code)
        self.assertEqual(302, self.login("Auditor", "auditor password")[1].status_code)
        setAuditorAccess(self.connection, self.auditor, True)
        self.assertEqual(401, first.get("/api/accounts").status_code)
        self.assertEqual(302, self.login("Auditor", "auditor password")[1].status_code)
        with self.assertRaises(ledger.ValidationError):
            setAuditorAccess(self.connection, self.owner, False)
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def testRenameUniquenessLedgerNameAndPasswordTargeting(self):
        ledgerName = self.connection.execute("SELECT name FROM ledgers WHERE id = ?", (self.firstLedger,)).fetchone()[0]
        renameUser(self.connection, self.owner, "Renamed Owner")
        renameUser(self.connection, self.auditor, "Straße")
        setAuditorAccess(self.connection, self.auditor, False)
        for action in (lambda: renameUser(self.connection, self.otherAuditor, "STRASSE"),
                       lambda: createOwner(self.connection, "STRASSE", "owner password", firstName="STRASSE")):
            with self.assertRaises(ledger.ValidationError):
                action()
        self.assertEqual(ledgerName, self.connection.execute("SELECT name FROM ledgers WHERE id = ?", (self.firstLedger,)).fetchone()[0])
        owner, _ = self.login("Renamed Owner")
        other, _ = self.login("Other Auditor", "auditor password")
        resetPassword(self.connection, self.owner, "new password")
        self.assertEqual(401, owner.get("/api/session").status_code)
        self.assertEqual(200, other.get("/api/session").status_code)
        self.assertNotIn("scrypt:", json.dumps([dict(row) for row in self.connection.execute("SELECT * FROM auditEvents")]))

    def testListSearchPaginationAndExplicitSelection(self):
        for index in range(21):
            createUser(self.connection, "auditor", f"Person {index:02}", "auditor password", firstName="Test")
        with patch("users.input", side_effect=["n", "1"]), patch("builtins.print") as output:
            selected = selectRecord(self.connection, "users")
        self.assertIn("page 2 of 2", str(output.call_args_list))
        self.assertGreater(selected, self.otherAuditor)
        with patch("users.input", side_effect=["s", "PERSON 20", "1"]), patch("builtins.print"):
            selected = selectRecord(self.connection, "users")
        self.assertEqual("Person 20", self.connection.execute("SELECT username FROM users WHERE id = ?", (selected,)).fetchone()[0])
        with patch("users.input", side_effect=["s", "absent", "c", "0"]), patch("builtins.print") as output:
            self.assertIsNone(selectRecord(self.connection, "users"))
        self.assertIn("No matches", str(output.call_args_list))

    def testCliCreationAndUserSelection(self):
        responses = ["2", "Second", "Second", "", "3", "2", "Second Auditor", "Second", "", "1", "s", "Second Auditor", "1", "1", "Renamed Auditor", "0", "0"]
        with patch("users.input", side_effect=responses), patch("users.getpass.getpass", return_value="valid password"), patch("builtins.print") as output:
            manageUsers(self.path)
        self.assertIn("User management does not start the server", str(output.call_args_list))
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM users WHERE username = 'Renamed Auditor'").fetchone())
        self.assertNotIn("valid password", str(output.call_args_list))

    def testMultipleLedgerBrowserIntegrationAndPortableDownloads(self):
        secondOwner = createOwner(self.connection, "Second", "second password", firstName="Second")
        secondLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (secondOwner,)).fetchone()[0]
        ledger.bindLedger(self.connection, secondLedger)
        createUser(self.connection, "auditor", "Second Auditor", "auditor password", firstName="Second Auditor")
        foreignAccount = ledger.createAccount(self.connection, "FOREIGN marker", "cash")
        foreignCategory = ledger.createCategory(self.connection, "FOREIGN category")
        secretId = ledger.createIntegrationSecret(self.connection, "Second agent")
        secret = self.connection.execute("SELECT secret FROM scopedintegrationSecrets WHERE id = ?", (secretId,)).fetchone()[0]
        secondClient, _ = self.login("Second", "second password")
        response = secondClient.post("/api/transactions", json={"accountId": foreignAccount, "categoryId": foreignCategory, "date": "2026-10-21", "description": "FOREIGN purchase", "amount": "5"})
        self.assertEqual(201, response.status_code)
        firstClient, _ = self.login()
        for url in ("/api/accounts", "/api/settings", "/api/transactions?period=all", "/api/dashboard"):
            self.assertNotIn("FOREIGN", firstClient.get(url).get_data(as_text=True))
        agent = self.application.test_client()
        self.assertIn("FOREIGN", agent.get("/api/integration/read/accounts", headers={"Authorization": f"Bearer {secret}"}).get_data(as_text=True))
        ledger.bindLedger(self.connection, self.firstLedger)
        setAuditorAccess(self.connection, self.auditor, False)
        before = self.path.read_bytes()
        for endpoint in ("/api/backup", "/api/package"):
            response = firstClient.get(endpoint)
            self.assertEqual(200, response.status_code)
            contents = response.data
            if endpoint == "/api/package":
                with zipfile.ZipFile(BytesIO(contents)) as archive:
                    contents = archive.read("No-Part-Finance/finance.db")
            exportedPath = Path(self.directory.name) / (endpoint.split("/")[-1] + ".db")
            exportedPath.write_bytes(contents)
            exported = sqlite3.connect(exportedPath)
            try:
                self.assertEqual(3, exported.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                self.assertEqual([(self.firstLedger,)], exported.execute("SELECT id FROM ledgers").fetchall())
                self.assertEqual(0, exported.execute("SELECT enabled FROM ledgerAccess WHERE userId = ?", (self.auditor,)).fetchone()[0])
                self.assertEqual([], exported.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                exported.close()
        self.assertEqual(before, self.path.read_bytes())

    def testMembershipUpgradePreservesRecordsAndRollsBackFailure(self):
        # Remove only newly added synthetic schema fields to exercise the genuine v22 upgrade.
        for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'audit%'").fetchall():
            self.connection.execute(f'DROP TRIGGER "{row["name"]}"')
        for action in ("INSERT", "UPDATE"):
            self.connection.execute(f"DROP TRIGGER transferLedger{action}")
        self.connection.execute("DROP INDEX auditOperationIndex")
        self.connection.execute("ALTER TABLE auditEvents DROP COLUMN operationId")
        ledger.installAuditTriggers(self.connection)
        self.connection.execute("DROP TRIGGER invalidateBrowserSessions")
        self.connection.execute("DROP TABLE browserSessions")
        self.connection.execute("ALTER TABLE users DROP COLUMN enabled")
        self.connection.execute("ALTER TABLE ledgerAccess DROP COLUMN enabled")
        self.connection.execute("PRAGMA user_version = 22")
        self.connection.commit()
        before = list(self.connection.iterdump())
        originalMigration = ledger.migrateToVersion23
        def failingMigration(connection):
            originalMigration(connection)
            raise RuntimeError("synthetic migration failure")
        with patch("database.migrateToVersion23", side_effect=failingMigration):
            with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                ledger.initializeDatabase(self.path)
        self.assertEqual(before, list(self.connection.iterdump()))
        with patch.object(ledger, "migrateToVersion27"), patch.object(ledger, "migrateToVersion28"):
            ledger.initializeDatabase(self.path)
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE enabled = 1").fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())

    def testOwnerLoginGatePreservesKeyStatesAndAuditorAccess(self):
        owner, _ = self.login()
        auditor, _ = self.login("Auditor", "auditor password")
        secretIds = [ledger.createIntegrationSecret(self.connection, label) for label in ("Enabled", "Disabled", "Revoked")]
        ledger.setIntegrationState(self.connection, secretIds[1], False)
        with self.connection:
            self.connection.execute("UPDATE integrationSecrets SET active = 0, revoked = 1 WHERE id = ?", (secretIds[2],))
        secrets = [tuple(row) for row in self.connection.execute("SELECT secret, active, revoked FROM scopedintegrationSecrets ORDER BY id")]
        agent = self.application.test_client()
        setUserEnabled(self.connection, self.owner, False)
        self.assertEqual(401, owner.get("/api/accounts").status_code)
        self.assertEqual(200, auditor.get("/api/accounts").status_code)
        self.assertEqual(401, self.login()[1].status_code)
        for secret, _, _ in secrets:
            self.assertEqual(401, agent.get("/api/integration/read/accounts", headers={"Authorization": f"Bearer {secret}"}).status_code)
        setUserEnabled(self.connection, self.owner, True)
        self.assertEqual(401, owner.get("/api/accounts").status_code)
        self.assertEqual(302, self.login()[1].status_code)
        for (secret, _, _), expected in zip(secrets, (200, 401, 401)):
            self.assertEqual(expected, agent.get("/api/integration/read/accounts", headers={"Authorization": f"Bearer {secret}"}).status_code)
        self.assertEqual(secrets, [tuple(row) for row in self.connection.execute("SELECT secret, active, revoked FROM scopedintegrationSecrets ORDER BY id")])

    def testIndependentLoginAccessAndPersistenceInExport(self):
        setUserEnabled(self.connection, self.auditor, False)
        setAuditorAccess(self.connection, self.auditor, False)
        setUserEnabled(self.connection, self.auditor, True)
        self.assertEqual(302, self.login("Auditor", "auditor password")[1].status_code)
        setUserEnabled(self.connection, self.auditor, False)
        setAuditorAccess(self.connection, self.auditor, True)
        self.assertEqual(401, self.login("Auditor", "auditor password")[1].status_code)
        snapshot = Path(self.directory.name) / "disabled-export.db"
        ledger.exportLedgerDatabase(self.connection, snapshot)
        ledger.initializeDatabase(snapshot)
        exported = ledger.connectInitialLedgerDatabase(snapshot)
        try:
            self.assertEqual((0, 1), tuple(exported.execute("SELECT u.enabled, a.enabled FROM users u JOIN ledgerAccess a ON a.userId = u.id WHERE u.id = ?", (self.auditor,)).fetchone()))
        finally:
            exported.close()
        self.application = createApp(snapshot)
        self.assertEqual(401, self.login("Auditor", "auditor password")[1].status_code)

    def testLoginStatusRollsBackOnAuditFailure(self):
        self.connection.execute("CREATE TRIGGER failStatusAudit BEFORE INSERT ON auditEvents BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        before = list(self.connection.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            setUserEnabled(self.connection, self.owner, False)
        self.assertEqual(before, list(self.connection.iterdump()))

    def testLastLoginDetailsAreReadOnlyAndIdentifyTimezone(self):
        from datetime import datetime
        timestamp = "2026-01-15T18:23:45+00:00"
        with self.connection:
            self.connection.execute("UPDATE users SET lastLoginAt = ? WHERE id = ?", (timestamp, self.owner))
        before = self.path.read_bytes()
        for userId, expected in ((self.owner, datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)")),
                                 (self.auditor, "Not recorded")):
            with patch("users.input", return_value="0"), patch("builtins.print") as output:
                manageSelectedUser(self.connection, userId)
            self.assertIn(f"Last successful login: {expected}", str(output.call_args_list))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(timestamp, self.connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (self.owner,)).fetchone()[0])
        setUserEnabled(self.connection, self.owner, False)
        self.assertEqual(timestamp, self.connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (self.owner,)).fetchone()[0])

    def testAuditCombinedTransfersAndHistoricalSnapshotValues(self):
        first = ledger.createAccount(self.connection, "Original Checking", "cash")
        second = ledger.createAccount(self.connection, "Original Savings", "cash")
        ledger.bindLedger(self.connection, self.firstLedger, "user", self.owner, "Recorded Owner")
        transfer = ledger.createTransfer(self.connection, "2026-10-21", first, second, "12.34", "Original move")
        ledger.updateTransfer(self.connection, transfer, "2026-10-22", second, first, "5", "Updated move")
        transactionId = self.connection.execute("SELECT id FROM scopedtransactions WHERE transferId = ?", (transfer,)).fetchone()[0]
        ledger.deleteTransaction(self.connection, transactionId)
        ledger.updateAccount(self.connection, first, "Renamed Checking", "cash")
        events = ledger.auditLogPage(self.connection)["rows"]
        transfers = [row for row in events if row["type"] == "Transfer / Payment"]
        self.assertEqual(["Deleted", "Updated", "Created"], [row["action"] for row in transfers])
        self.assertIn("Original Checking", json.dumps(transfers))
        self.assertNotIn("Renamed Checking", json.dumps(transfers))
        updated = transfers[1]
        self.assertIn({"label": "Amount", "value": "$12.34"}, updated["oldValues"])
        self.assertIn({"label": "Amount", "value": "$5.00"}, updated["newValues"])
        self.assertEqual("Recorded Owner", updated["user"])
        self.assertIsNone(transfers[0]["newValues"])
        self.assertIsNone(transfers[2]["oldValues"])
        self.assertEqual([], self.connection.execute("SELECT * FROM scopedtransactions").fetchall())

    def testAuditPaginationRolesIsolationAndReadOnly(self):
        for index in range(55):
            ledger.createCategory(self.connection, f"Category {index}")
        secondOwner = createOwner(self.connection, "Second", "second password", firstName="Second")
        secondLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (secondOwner,)).fetchone()[0]
        ledger.bindLedger(self.connection, secondLedger)
        ledger.createCategory(self.connection, "FOREIGN secret category")
        owner, _ = self.login()
        auditor, _ = self.login("Auditor", "auditor password")
        before = self.path.read_bytes()
        for client in (owner, auditor):
            for size in (10, 50, 100, 250):
                response = client.get(f"/api/audit-log?page=1&perPage={size}")
                self.assertEqual(200, response.status_code)
                self.assertLessEqual(len(response.json["rows"]), size)
                self.assertNotIn("FOREIGN", response.get_data(as_text=True))
            first = client.get("/api/audit-log?page=1&perPage=10").json
            nextPage = client.get("/api/audit-log?page=2&perPage=10").json
            self.assertEqual(11, nextPage["start"])
            self.assertFalse(first["hasPrevious"])
            self.assertTrue(nextPage["hasPrevious"])
            self.assertNotEqual(first["rows"], nextPage["rows"])
            last = client.get("/api/audit-log?page=9999&perPage=10").json
            self.assertFalse(last["hasNext"])
            self.assertEqual(first["total"], last["end"])
            for query in ("page=0", "page=-1", "page=1.1", "page=", "perPage=20", "perPage=", "ledgerId=other", "page=1&page=2"):
                self.assertEqual(400, client.get(f"/api/audit-log?{query}").status_code, query)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(401, self.application.test_client().get("/api/audit-log").status_code)
        ledger.bindLedger(self.connection, self.firstLedger)
        secretId = ledger.createIntegrationSecret(self.connection, "Agent")
        secret = self.connection.execute("SELECT secret FROM scopedintegrationSecrets WHERE id = ?", (secretId,)).fetchone()[0]
        self.assertEqual(403, self.application.test_client().get("/api/audit-log", headers={"Authorization": f"Bearer {secret}"}).status_code)
        self.assertNotIn(secret, owner.get("/api/audit-log").get_data(as_text=True))

    def testAuditLegacyRowsRemainAndRollbackLeavesNoOperation(self):
        with self.connection:
            self.connection.execute("INSERT INTO auditEvents (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, beforeValues, afterValues) VALUES (?, '2026-10-20T12:00:00Z', 'system', 'cli', 'Command line', 'insert', 'transactions', '999', NULL, ?)", (self.firstLedger, json.dumps({"accountId": 1, "amountCents": -100, "transferId": "legacy", "categoryId": None})))
        events = ledger.auditLogPage(self.connection)["rows"]
        self.assertEqual(1, sum(row["type"] == "Transfer row (legacy)" for row in events))
        first = ledger.createAccount(self.connection, "Checking", "cash")
        second = ledger.createAccount(self.connection, "Savings", "cash")
        self.connection.execute("CREATE TRIGGER failSecondSide BEFORE INSERT ON transactions WHEN NEW.amountCents > 0 BEGIN SELECT RAISE(ABORT, 'second side unavailable'); END")
        before = list(self.connection.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.createTransfer(self.connection, "2026-10-21", first, second, "10", "Failure")
        self.assertEqual(before, list(self.connection.iterdump()))

    def testAuditEmptyAndSafeText(self):
        # No events are fabricated for a newly created initial ledger.
        emptyPath = Path(self.directory.name) / "empty.db"
        ledger.initializeDatabase(emptyPath)
        connection = ledger.connectInitialLedgerDatabase(emptyPath)
        try:
            self.assertEqual({"rows": [], "page": 1, "perPage": 50, "total": 0, "start": 0, "end": 0, "hasPrevious": False, "hasNext": False}, ledger.auditLogPage(connection))
        finally:
            connection.close()
        ledger.createCategory(self.connection, '<script>alert("x")</script>')
        owner, _ = self.login()
        response = owner.get("/api/audit-log")
        self.assertIn('<script>alert("x")</script>', json.dumps(response.json).replace('\\"', '"'))

    def testAuditHidesOnlyLegacyIdentityBookkeepingWithoutChangingStoredHistory(self):
        accountId = ledger.createAccount(self.connection, "Synthetic Checking", "cash")
        categoryId = ledger.createCategory(self.connection, "Synthetic Expense")
        transactionId = ledger.createTransaction(self.connection, accountId, "2026-10-21", "Historical example", categoryId, "12.34")
        stored = dict(self.connection.execute("SELECT * FROM scopedtransactions WHERE id = ?", (transactionId,)).fetchone())
        stored.pop("ledgerId")
        unassigned = {**stored, "eventIdentity": None}
        changedAmount = {**stored, "amountCents": -2500}
        # Reproduce the pre-v25 identity trigger, then retain independent real changes.
        with self.connection:
            for before, after in ((unassigned, stored), (unassigned, changedAmount), (stored, stored)):
                self.connection.execute("""INSERT INTO auditEvents
                    (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, beforeValues, afterValues)
                    VALUES (?, '2026-10-21T12:00:00Z', 'system', 'cli', 'Command line', 'update', 'transactions', ?, ?, ?)""",
                    (self.firstLedger, str(transactionId), json.dumps(before), json.dumps(after)))
        beforeDump = list(self.connection.iterdump())
        storedCount = self.connection.execute("SELECT COUNT(*) FROM auditEvents WHERE ledgerId = ?", (self.firstLedger,)).fetchone()[0]
        events = ledger.auditLogPage(self.connection)
        self.assertEqual(storedCount - 1, events["total"])
        updates = [row for row in events["rows"] if row["type"] == "Transaction" and row["action"] == "Updated"]
        self.assertEqual(2, len(updates))
        self.assertTrue(any({"label": "Amount", "value": "$25.00"} in event["newValues"] for event in updates))
        self.assertEqual(beforeDump, list(self.connection.iterdump()))

    def testLegacyAuditCategoryFallbackUsesCurrentLedgerAndPreservesRecordedContext(self):
        expense = ledger.createCategory(self.connection, "Current Expense")
        income = ledger.createCategory(self.connection, "Current Income", reportingRole="income")
        foreignOwner = createOwner(self.connection, "Foreign Owner", "owner password", firstName="Foreign Owner")
        foreignLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (foreignOwner,)).fetchone()[0]
        ledger.bindLedger(self.connection, foreignLedger)
        foreignCategory = ledger.createCategory(self.connection, "Private Foreign Category")
        ledger.bindLedger(self.connection, self.firstLedger)
        examples = [
            {"description": "Purchase", "categoryId": expense, "amountCents": -1234},
            {"description": "Refund", "categoryId": expense, "amountCents": 567},
            {"description": "Income", "categoryId": income, "amountCents": 1234},
            {"description": "Reversal", "categoryId": income, "amountCents": -567},
            {"description": "Recorded", "categoryId": expense, "categoryName": "Historical Name", "reportingRole": "income", "amountCents": 800},
            {"description": "Foreign", "categoryId": foreignCategory, "amountCents": -900},
            {"description": "Missing", "categoryId": 999999, "amountCents": -900},
        ]
        with self.connection:
            for index, snapshot in enumerate(examples):
                self.connection.execute("""INSERT INTO auditEvents
                    (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, afterValues)
                    VALUES (?, '2026-10-21T12:00:00Z', 'system', 'cli', 'Command line', 'insert', 'transactions', ?, ?)""",
                    (self.firstLedger, str(index), json.dumps(snapshot)))
        owner, _ = self.login()
        beforeDump = list(self.connection.iterdump())
        events = owner.get("/api/audit-log").json["rows"]
        displayed = {}
        for event in events:
            if event["type"] == "Transaction":
                values = {item["label"]: item["value"] for item in event["newValues"]}
                displayed[values["Description"]] = values
        for description, amount, category in (("Purchase", "$12.34", "Current Expense"), ("Refund", "-$5.67", "Current Expense"),
                                               ("Income", "$12.34", "Current Income"), ("Reversal", "-$5.67", "Current Income")):
            self.assertEqual(amount, displayed[description]["Amount"])
            self.assertEqual(category, displayed[description]["Category"])
            self.assertNotIn("Category reference", displayed[description])
        self.assertEqual("Historical Name", displayed["Recorded"]["Category"])
        self.assertEqual("$8.00", displayed["Recorded"]["Amount"])
        for description in ("Foreign", "Missing"):
            self.assertEqual("-$9.00", displayed[description]["Ledger amount"])
            self.assertIn("Category reference", displayed[description])
        self.assertNotIn("Private Foreign Category", json.dumps(events))
        self.assertEqual(beforeDump, list(self.connection.iterdump()))

    def testPendingAuditKeepsSubmissionAndReviewSeparateFromCombinedApproval(self):
        account = ledger.createAccount(self.connection, "Checking", "cash")
        category = ledger.createCategory(self.connection, "Shopping")
        integration = ledger.createIntegrationSecret(self.connection, "Example integration")
        ledger.bindLedger(self.connection, self.firstLedger, "integration", integration, "Example integration")
        proposal, _ = ledger.submitPendingEntry(self.connection, integration, {
            "submissionId": "private-retry-identity", "date": "2026-10-21", "accountId": account,
            "categoryId": category, "description": "Original refund", "amount": "-12.34"})
        review = {"date": "2026-10-21", "accountId": account, "categoryId": category,
                  "description": "Corrected refund", "amount": "-5.67", "reviewedRole": "expense", "reviewVersion": 0}
        ledger.bindLedger(self.connection, self.firstLedger, "user", self.owner, "Owner")
        ledger.savePendingReview(self.connection, proposal["id"], review)
        ledger.decidePendingTransactions(self.connection, "approve", [{"id": proposal["id"], **review, "reviewVersion": 1}])
        beforeDump = list(self.connection.iterdump())
        events = ledger.auditLogPage(self.connection)["rows"]
        pending = [event for event in events if event["type"] in {"Pending entry", "Pending transaction"}]
        self.assertEqual(["Approved", "Reviewed", "Submitted"], [event["action"] for event in pending])
        submitted = {item["label"]: item["value"] for item in pending[2]["newValues"]}
        self.assertEqual({"Status": "Pending", "Source": "Integration", "Date": "2026-10-21", "Description": "Original refund",
                          "Account": "Checking", "Category": "Shopping", "Amount": "-$12.34"}, submitted)
        self.assertEqual("Integration: Example integration", pending[2]["user"])
        self.assertIn({"label": "Amount", "value": "-$5.67"}, pending[1]["newValues"])
        self.assertIn({"label": "Description", "value": "Corrected refund"}, pending[0]["newValues"])
        self.assertIn({"label": "Status", "value": "Approved"}, pending[0]["newValues"])
        self.assertIn({"label": "Status", "value": "Pending"}, pending[0]["oldValues"])
        approvedOld = {item["label"]: item["value"] for item in pending[0]["oldValues"]}
        approvedNew = {item["label"]: item["value"] for item in pending[0]["newValues"]}
        for label, value in (("Description", "Corrected refund"), ("Amount", "-$5.67"),
                             ("Account", "Checking"), ("Category", "Shopping"), ("Date", "2026-10-21"),
                             ("Source", "Integration"), ("Pinned", "No")):
            self.assertEqual(value, approvedOld[label])
            self.assertEqual(value, approvedNew[label])
        self.assertIn({"label": "Amount", "value": "-$5.67"}, pending[0]["newValues"])
        self.assertEqual(0, len([event for event in events if event["type"] == "Transaction"]))
        for hidden in ("private-retry-identity", "Original proposal", "Reviewed proposal", "Review version", "Original reporting role", "Source snapshot"):
            self.assertNotIn(hidden, json.dumps(pending))
        self.assertEqual(beforeDump, list(self.connection.iterdump()))

    def testPendingTransferAuditAndMalformedContentRemainSafeAndReadable(self):
        checking = ledger.createAccount(self.connection, "Checking", "cash")
        savings = ledger.createAccount(self.connection, "Savings", "cash")
        proposal, _ = ledger.submitPendingEntry(self.connection, None, {
            "submissionId": "transfer-review", "type": "transfer", "date": "2026-10-21",
            "fromAccountId": checking, "toAccountId": savings, "description": "Move savings", "amount": "25.00"})
        ledger.decidePendingTransactions(self.connection, "reject", [{"id": proposal["id"], "reviewVersion": 0}])
        events = [event for event in ledger.auditLogPage(self.connection)["rows"] if event["type"] == "Pending entry"]
        self.assertEqual(["Rejected", "Submitted"], [event["action"] for event in events])
        values = {item["label"]: item["value"] for item in events[1]["newValues"]}
        self.assertEqual(("Checking", "Savings", "$25.00"), (values["From Account"], values["To Account"], values["Amount"]))
        malformed = {"status": "Pending", "originalContent": "broken", "sourceSnapshot": "private-source", "submissionId": "private-id"}
        self.assertEqual({"Status": "Pending", "Source": "Integration", "Proposal": "Unavailable"}, ledger.auditPendingValues(malformed, {}, {}))
        malformed["originalContent"] = json.dumps({"description": "Recorded", "amount": "invalid", "accountId": 9999, "categoryId": 9999, "secret": "private-secret"})
        values = ledger.auditPendingValues(malformed, {}, {})
        self.assertEqual("Invalid recorded amount", values["Amount"])
        self.assertEqual("Recorded reference 9999", values["Category"])
        self.assertNotIn("private", json.dumps(values))

    def testApprovalGroupingKeepsBatchItemsAndPaginationSeparate(self):
        checking = ledger.createAccount(self.connection, "Checking", "cash")
        savings = ledger.createAccount(self.connection, "Savings", "cash")
        category = ledger.createCategory(self.connection, "Shopping")
        for index in range(12):
            fields = {"submissionId": f"batch-{index}", "date": "2026-10-21", "description": f"Batch {index}", "amount": "10.00"}
            if index < 6:
                fields.update({"accountId": checking, "categoryId": category, "pinned": False})
            else:
                fields.update({"type": "transfer", "fromAccountId": checking, "toAccountId": savings})
            ledger.submitPendingEntry(self.connection, None, fields)
        reviews = [{"id": row["id"], **{field: row[field] for field in ledger.pendingFields(row["kind"])},
                    "reviewedRole": row["currentRole"], "reviewVersion": row["reviewVersion"]} for row in ledger.pendingReviewRows(self.connection)]
        # Final approval may contain unsaved edits; show the actual created ledger snapshot.
        reviews[0].update({"description": "Final approved purchase", "amount": "15.00"})
        ledger.decidePendingTransactions(self.connection, "approve", reviews)
        ledger.createTransaction(self.connection, checking, "2026-10-21", "Final approved purchase", category, "15.00")
        beforeDump = list(self.connection.iterdump())
        first = ledger.auditLogPage(self.connection, "1", "10")
        pages = [ledger.auditLogPage(self.connection, str(page), "10") for page in range(1, (first["total"] + 9) // 10 + 1)]
        events = [row for page in pages for row in page["rows"]]
        self.assertEqual(first["total"], len(events))
        self.assertEqual(12, sum(row["action"] == "Approved" for row in events))
        self.assertEqual(6, sum(row["type"] == "Pending transaction" for row in events))
        transfers = [row for row in events if row["type"] == "Pending transfer"]
        self.assertEqual(6, len(transfers))
        self.assertEqual(1, sum(row["type"] == "Transaction" for row in events))
        self.assertEqual(0, sum(row["type"] == "Transfer / Payment" for row in events))
        for transfer in transfers:
            self.assertIn({"label": "From Account", "value": "Checking"}, transfer["newValues"])
            self.assertIn({"label": "To Account", "value": "Savings"}, transfer["newValues"])
            self.assertIn({"label": "Amount", "value": "$10.00"}, transfer["newValues"])
        final = next(row for row in events if row["type"] == "Pending transaction" and {"label": "Description", "value": "Final approved purchase"} in row["newValues"])
        self.assertIn({"label": "Amount", "value": "$15.00"}, final["newValues"])
        self.assertIn({"label": "Amount", "value": "$10.00"}, final["oldValues"])
        self.assertIn({"label": "Description", "value": "Batch 0"}, final["oldValues"])
        for transfer in transfers:
            old = {item["label"]: item["value"] for item in transfer["oldValues"]}
            new = {item["label"]: item["value"] for item in transfer["newValues"]}
            self.assertEqual("Pending", old.pop("Status"))
            self.assertEqual("Approved", new.pop("Status"))
            self.assertEqual(old, new)
        self.assertEqual(beforeDump, list(self.connection.iterdump()))
        approvedTransaction = self.connection.execute("SELECT transactionId FROM scopedpendingSubmissions WHERE submissionId = 'batch-0'").fetchone()[0]
        approvedTransfer = self.connection.execute("SELECT transferId FROM scopedpendingSubmissions WHERE submissionId = 'batch-6'").fetchone()[0]
        ledger.updateTransaction(self.connection, approvedTransaction, checking, "2026-10-22", "Later edit", category, "20.00")
        ledger.deleteTransaction(self.connection, approvedTransaction)
        ledger.updateTransfer(self.connection, approvedTransfer, "2026-10-22", savings, checking, "30.00", "Later transfer edit")
        transferRow = self.connection.execute("SELECT id FROM scopedtransactions WHERE transferId = ? LIMIT 1", (approvedTransfer,)).fetchone()[0]
        ledger.deleteTransaction(self.connection, transferRow)
        historical = [row for row in ledger.auditLogPage(self.connection, "1", "100")["rows"] if row["action"] == "Approved"]
        self.assertEqual(12, len(historical))
        self.assertIn(final, historical)
        originalTransfer = next(row for row in transfers if {"label": "Description", "value": "Batch 6"} in row["newValues"])
        self.assertIn(originalTransfer, historical)

    def testLegacyApprovalGroupingRequiresUnambiguousLinksRatherThanTimestamps(self):
        def record(recordType, recordId, action, before, after, operation=None, actor="cli"):
            self.connection.execute("""INSERT INTO auditEvents
                (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, beforeValues, afterValues, operationId)
                VALUES (?, '2026-10-21T12:00:00Z', 'system', ?, 'Command line', ?, ?, ?, ?, ?, ?)""",
                (self.firstLedger, actor, action, recordType, str(recordId), json.dumps(before) if before is not None else None,
                 json.dumps(after) if after is not None else None, operation))

        def transaction(recordId, transfer=None, amount=-1000):
            return {"id": recordId, "date": "2026-10-21", "description": f"Recorded {recordId}", "accountId": recordId,
                    "amountCents": amount, "categoryId": None, "transferId": transfer}

        def approval(recordId, transactionId=None, transferId=None, operation=None, actor="cli"):
            before = {"status": "Pending", "kind": "transfer" if transferId else "transaction", "originalContent": "{}"}
            after = {**before, "status": "Approved", "transactionId": transactionId, "transferId": transferId}
            record("pendingSubmissions", recordId, "update", before, after, operation, actor)

        with self.connection:
            record("transactions", 700, "insert", None, transaction(700))
            record("transactions", 701, "insert", None, transaction(701))
            approval(1, transactionId=700)
            for recordId, amount in ((710, -2000), (711, 2000)):
                record("transactions", recordId, "insert", None, {**transaction(recordId, "legacy-transfer", amount), "description": "Legacy move"})
            approval(2, transferId="legacy-transfer")
            # A shared timestamp cannot establish a missing link, shared link, or actor/operation match.
            record("transactions", 720, "insert", None, transaction(720))
            approval(3, transactionId=720)
            approval(4, transactionId=720)
            record("transactions", 730, "insert", None, transaction(730))
            record("transactions", 730, "delete", transaction(730), None)
            record("transactions", 730, "insert", None, {**transaction(730), "description": "Reused ID"})
            approval(5, transactionId=730)
            record("transactions", 740, "insert", None, transaction(740), "other-operation")
            approval(6, transactionId=740, operation="approval-operation")
            record("transactions", 750, "insert", None, transaction(750, "incomplete-transfer"))
            approval(7, transferId="incomplete-transfer")
            record("transactions", 760, "insert", None, transaction(760))
            approval(8, transactionId=760, actor="other-actor")
            record("transactions", 770, "insert", None, transaction(770))
            record("transactions", 770, "update", transaction(770), {**transaction(770), "amountCents": -500})
            approval(9, transactionId=770)
            for recordId, amount in ((780, -1000), (781, 500)):
                record("transactions", recordId, "insert", None, {**transaction(recordId, "invalid-transfer", amount), "description": "Invalid move"})
            approval(10, transferId="invalid-transfer")
        beforeDump = list(self.connection.iterdump())
        storedCount = self.connection.execute("SELECT COUNT(*) FROM auditEvents WHERE ledgerId = ?", (self.firstLedger,)).fetchone()[0]
        result = ledger.auditLogPage(self.connection)
        merged = [row for row in result["rows"] if row["type"] in {"Pending transaction", "Pending transfer"}]
        self.assertEqual(3, len(merged))
        self.assertEqual(storedCount - 4, result["total"])
        self.assertTrue(any({"label": "Description", "value": "Reused ID"} in row["newValues"] for row in merged))
        self.assertEqual(1, sum(row["type"] == "Pending transfer" for row in merged))
        self.assertEqual(beforeDump, list(self.connection.iterdump()))

class LedgerNameTests(LedgerNameFixture, unittest.TestCase):
    def testRenameValidationPermissionsAndPersistence(self):
        ownerClient = self.login()
        auditorClient = self.login("Auditor", "auditor password")
        originalLedger = ownerClient.get("/api/ledger").get_json()
        self.assertEqual(200, auditorClient.get("/api/ledger").status_code)
        self.assertEqual(403, auditorClient.put("/api/ledger", json={"name": "Denied"}).status_code)
        csrf = ownerClient.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, ownerClient.put("/api/ledger", json={"name": "Denied"}).status_code)
        ownerClient.environ_base["HTTP_X_CSRF_TOKEN"] = csrf
        for invalid in (None, "", "  ", "x" * 101, "a\nb", "a\x00b", 123):
            self.assertEqual(400, ownerClient.put("/api/ledger", json={"name": invalid}).status_code)
        for valid in ("x", "x" * 100, "Personal <Finances>"):
            response = ownerClient.put("/api/ledger", json={"name": "  " + valid + "  "})
            self.assertEqual({"id": originalLedger["id"], "name": valid}, response.get_json())
        renameUser(self.connection, self.owner, "Renamed Owner")
        self.assertEqual("Personal <Finances>", auditorClient.get("/api/ledger").get_json()["name"])
        ledger.bindLedger(self.connection, self.ledgerId)
        self.assertEqual("Personal <Finances>", self.connection.execute("SELECT name FROM ledgers WHERE id = ?", (self.ledgerId,)).fetchone()[0])
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess").fetchone()[0])

    def testRenameAuditRollbackIsolationAndDownload(self):
        client = self.login()
        ledger.bindLedger(self.connection, self.ledgerId)
        otherOwner = createOwner(self.connection, "Other", "owner password", firstName="Other")
        otherLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (otherOwner,)).fetchone()[0]
        self.connection.execute("CREATE TRIGGER failRenameAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'ledgers' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
        self.connection.commit()
        self.assertEqual(500, client.put("/api/ledger", json={"name": "Unsaved"}).status_code)
        self.assertEqual("Owner’s Finances", client.get("/api/ledger").get_json()["name"])
        self.connection.execute("DROP TRIGGER failRenameAudit")
        self.connection.commit()
        self.assertEqual(200, client.put("/api/ledger", json={"name": "Other’s Finances", "ledgerId": otherLedger}).status_code)
        self.assertEqual("Other’s Finances", self.connection.execute("SELECT name FROM ledgers WHERE id = ?", (otherLedger,)).fetchone()[0])
        exportedPath = Path(self.directory.name) / "exported.db"
        exportedPath.write_bytes(client.get("/api/backup").data)
        with sqlite3.connect(exportedPath) as exported:
            self.assertEqual([(self.ledgerId, "Other’s Finances")], exported.execute("SELECT * FROM ledgers").fetchall())

    def testApplicationDownloadRetainsSavedName(self):
        client = self.login()
        self.assertEqual(200, client.put("/api/ledger", json={"name": "Portable name"}).status_code)
        response = client.get("/api/package")
        self.assertEqual(200, response.status_code)
        with zipfile.ZipFile(BytesIO(response.data)) as archive:
            exportedPath = Path(self.directory.name) / "package.db"
            exportedPath.write_bytes(archive.read("No-Part-Finance/finance.db"))
        with sqlite3.connect(exportedPath) as exported:
            self.assertEqual("Portable name", exported.execute("SELECT name FROM ledgers").fetchone()[0])

class SharingTests(SharingFixture, unittest.TestCase):
    def testMemberParityIsolationRevocationAndReinvitation(self):
        ownerClient = self.login()
        cash = ledger.createAccount(self.connection, "Cash", "cash", openingBalance="100")
        category = ledger.createCategory(self.connection, "Expense")
        transaction = ledger.createTransaction(self.connection, cash, "2026-10-22", "Owner entry", category, "10")
        member = self.accept(self.invite(client=ownerClient))
        self.assertEqual("member", member.get("/api/session").get_json()["role"])
        self.assertEqual(200, member.delete(f"/api/transactions/{transaction}").status_code)
        self.assertEqual(200, member.get("/api/sharing").status_code)
        self.assertEqual(200, member.get("/api/audit-log").status_code)
        for endpoint in ("/api/backup", "/api/package"):
            self.assertEqual(403, member.get(endpoint).status_code)
        self.assertEqual(403, member.put("/api/ledger", json={"name": "Denied"}).status_code)
        self.assertEqual(403, member.post("/api/invitations", json={"username": "Auditor", "role": "member"}).status_code)
        keyResponse = member.post("/api/integration-secrets", json={"label": "Member key"})
        self.assertEqual(201, keyResponse.status_code)
        keyId = keyResponse.get_json()["id"]
        secret = member.post(f"/api/integration-secrets/{keyId}/reveal").get_json()["secret"]
        self.assertEqual(self.otherOwner, self.connection.execute("SELECT creatorUserId FROM integrationSecrets WHERE id = ?", (keyId,)).fetchone()[0])
        bearer = {"Authorization": "Bearer " + secret}
        self.assertEqual(200, self.application.test_client().get("/api/integration/read/accounts", headers=bearer).status_code)
        self.assertEqual(200, ownerClient.patch(f"/api/sharing/{self.otherOwner}", json={"role": "auditor"}).status_code)
        self.assertEqual(403, member.post("/api/accounts", json={"name": "Denied", "accountClass": "cash"}).status_code)
        self.assertEqual(401, self.application.test_client().get("/api/integration/read/accounts", headers=bearer).status_code)
        self.assertEqual(200, member.get("/api/invitations").status_code)
        self.assertEqual(200, ownerClient.patch(f"/api/sharing/{self.otherOwner}", json={"revoke": True}).status_code)
        self.assertEqual(403, member.get("/api/accounts").status_code)
        self.assertEqual(200, member.get("/api/session").status_code)
        newInvite = self.invite(client=ownerClient)
        self.assertEqual(200, member.post(f"/api/invitations/{newInvite}/decision", json={"decision": "accept"}).status_code)
        self.assertEqual("member", member.get("/api/session").get_json()["role"])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE userId = ? AND ledgerId = ?", (self.otherOwner, self.ledgerId)).fetchone()[0])
        self.assertEqual(200, self.application.test_client().get("/api/integration/read/accounts", headers=bearer).status_code)

    def testInvitationPrivacyValidationCancellationExpiryAndRollback(self):
        ownerClient = self.login()
        other = self.login("Other", "other password")
        invitationId = self.invite(client=ownerClient)
        self.assertEqual(403, other.get("/api/accounts", headers={"X-Ledger-ID": self.ledgerId}).status_code)
        self.assertEqual(403, other.get("/api/audit-log", headers={"X-Ledger-ID": self.ledgerId}).status_code)
        self.assertEqual(400, ownerClient.post(f"/api/invitations/{invitationId}/decision", json={"decision": "accept"}).status_code)
        duplicate = ownerClient.post("/api/invitations", json={"username": "Other", "role": "member"})
        self.assertEqual("Invitation already pending", duplicate.get_json()["error"])
        self.assertEqual(200, ownerClient.post(f"/api/invitations/{invitationId}/cancel").status_code)
        self.assertEqual(409, other.post(f"/api/invitations/{invitationId}/decision", json={"decision": "accept"}).status_code)
        invitationId = self.invite(client=ownerClient)
        self.connection.execute("CREATE TRIGGER failSharingAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'sharingInvitations' AND NEW.action = 'invitation-accepted' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
        self.connection.commit()
        self.assertEqual(500, other.post(f"/api/invitations/{invitationId}/decision", json={"decision": "accept"}).status_code)
        self.assertIsNone(self.connection.execute("SELECT 1 FROM ledgerAccess WHERE userId = ? AND ledgerId = ?", (self.otherOwner, self.ledgerId)).fetchone())
        self.assertEqual("Pending", self.connection.execute("SELECT status FROM sharingInvitations WHERE id = ?", (invitationId,)).fetchone()[0])
        self.connection.execute("DROP TRIGGER failSharingAudit")
        self.connection.commit()
        expiry = self.connection.execute("SELECT expiresAt FROM sharingInvitations WHERE id = ?", (invitationId,)).fetchone()[0]
        with self.assertRaises(ledger.SubmissionConflict):
            ledger.decideSharingInvitation(self.connection, invitationId, self.otherOwner, "accept", now=expiry)
        self.assertEqual([], ledger.recipientInvitations(self.connection, self.otherOwner, now=expiry))
        self.assertEqual(200, other.post(f"/api/invitations/{invitationId}/decision", json={"decision": "decline"}).status_code)

    def testRateLimitsCountFailedLookupsAndSurviveRestart(self):
        ledger.bindLedger(self.connection, self.ledgerId, "user", self.owner, "Owner")
        now = 2000000000
        for offset in range(5):
            with self.assertRaisesRegex(ledger.ValidationError, "User not found"):
                ledger.sendSharingInvitation(self.connection, "Missing", "member", now=now + offset)
        for offset in (5, 50, 599):
            with self.assertRaises(ledger.InvitationRateLimit):
                ledger.sendSharingInvitation(self.connection, "Other", "member", now=now + offset)
        ledger.initializeDatabase(self.path)
        self.assertEqual(5, self.connection.execute("SELECT COUNT(*) FROM invitationAttempts").fetchone()[0])
        ledger.sendSharingInvitation(self.connection, "Other", "member", now=now + 600)
        self.assertEqual(6, self.connection.execute("SELECT COUNT(*) FROM invitationAttempts").fetchone()[0])
        self.connection.execute("DELETE FROM invitationAttempts")
        self.connection.executemany("INSERT INTO invitationAttempts VALUES (?, ?)", [(self.owner, now + index * 601) for index in range(20)])
        self.connection.commit()
        with self.assertRaises(ledger.InvitationRateLimit):
            ledger.checkInvitationLimit(self.connection, now=now + 20 * 601)
        ledger.checkInvitationLimit(self.connection, now=now + 86400)

    def testCreateLedgerByAuditorAndNoLedgerLogin(self):
        auditor = self.login("Auditor", "auditor password")
        created = auditor.post("/api/ledgers", json={"name": "  Independent  "})
        self.assertEqual(201, created.status_code)
        ledgerId = created.get_json()["id"]
        selected = auditor.get("/api/session", headers={"X-Ledger-ID": ledgerId}).get_json()
        self.assertEqual("owner", selected["role"])
        self.assertEqual(2, len(selected["ledgers"]))
        self.assertEqual([], auditor.get("/api/categories", headers={"X-Ledger-ID": ledgerId}).get_json())
        self.assertEqual(1, len(auditor.get("/api/accounts", headers={"X-Ledger-ID": ledgerId}).get_json()))
        self.connection.execute("UPDATE ledgerAccess SET enabled = 0 WHERE userId = ? AND ledgerId = ?", (self.auditor, self.ledgerId))
        self.connection.commit()
        # A different synthetic User retains a Disabled membership and can sign in.
        ledger.bindLedger(self.connection, self.ledgerId)
        noAccessId = createUser(self.connection, "auditor", "No access", "no access password", firstName="No access")
        self.connection.execute("UPDATE ledgerAccess SET enabled = 0 WHERE userId = ?", (noAccessId,))
        self.connection.commit()
        noAccess = self.login("No access", "no access password")
        self.assertIsNone(noAccess.get("/api/session").get_json()["ledgerId"])
        self.assertEqual(403, noAccess.get("/api/accounts").status_code)
        self.assertEqual(200, noAccess.get("/api/invitations").status_code)
        self.assertEqual(201, noAccess.post("/api/ledgers", json={"name": "New start"}).status_code)

    def testIndependentSelectionsAndDuplicateLabels(self):
        member = self.accept(self.invite())
        ledger.renameLedger(self.connection, "Same")
        ledger.bindLedger(self.connection, self.otherLedger)
        ledger.renameLedger(self.connection, "Same")
        member.environ_base.pop("HTTP_X_LEDGER_ID")
        session = member.get("/api/session").get_json()
        self.assertEqual(self.otherLedger, session["ledgerId"])
        self.assertEqual(409, member.get("/api/accounts").status_code)
        self.assertEqual({"Same (Other)", "Same (Owner)"}, {item["label"] for item in session["ledgers"]})
        self.assertEqual(self.ledgerId, member.get("/api/session", headers={"X-Ledger-ID": self.ledgerId}).get_json()["ledgerId"])
        self.assertEqual(self.otherLedger, member.get("/api/session", headers={"X-Ledger-ID": self.otherLedger}).get_json()["ledgerId"])
        self.assertEqual(409, member.get("/api/accounts?ledger=" + self.ledgerId, headers={"X-Ledger-ID": self.otherLedger}).status_code)

    def testBrowserExportsOmitCredentialsAndUnrelatedUsers(self):
        from users import resetPassword
        member = self.accept(self.invite())
        ledger.bindLedger(self.connection, self.ledgerId, "user", self.owner, "Owner")
        keyId = ledger.createIntegrationSecret(self.connection, "Keep metadata")
        secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (keyId,)).fetchone()[0]
        sourceHashes = [row[0] for row in self.connection.execute("SELECT passwordHash FROM users")]
        response = self.login().get("/api/backup")
        self.assertEqual(200, response.status_code)
        for sensitive in [secret, *sourceHashes]:
            self.assertNotIn(sensitive.encode(), response.data)
        exportedPath = Path(self.directory.name) / "credential-free.db"
        exportedPath.write_bytes(response.data)
        exportedApp = createApp(exportedPath)
        originalApplication = self.application
        self.application = exportedApp
        client = exportedApp.test_client()
        csrf = re.search('name="csrfToken" value="([^"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        self.assertEqual(401, client.post("/sign-in", data={"username": "Owner", "password": "owner password", "csrfToken": csrf}).status_code)
        exported = ledger.connectDatabase(exportedPath)
        self.addCleanup(exported.close)
        ledger.bindLedger(exported, self.ledgerId)
        self.assertEqual({self.owner, self.auditor, self.otherOwner}, {row[0] for row in exported.execute("SELECT id FROM users")})
        self.assertEqual(1, exported.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0])
        self.assertEqual(0, exported.execute("SELECT COUNT(*) FROM sharingInvitations").fetchone()[0])
        self.assertTrue(all(row[0] == "" for row in exported.execute("SELECT passwordHash FROM users")))
        resetPassword(exported, self.owner, "replacement password")
        resetClient = self.login("Owner", "replacement password")
        self.assertEqual(400, resetClient.post(f"/api/integration-secrets/{keyId}/state", json={"enabled": True}).status_code)
        self.assertEqual(401, exportedApp.test_client().get("/api/integration/read/accounts", headers={"Authorization": "Bearer " + secret}).status_code)
        self.application = originalApplication
        self.assertEqual(200, self.login().get("/api/session").status_code)

    def testMigrationPreservesLegacyKeysAndAttributesOwner(self):
        from unittest.mock import patch
        legacyPath = Path(self.directory.name) / "legacy.db"
        with patch.object(ledger, "schemaVersion", 26), patch.object(ledger, "migrateToVersion27"):
            ledger.initializeDatabase(legacyPath)
        connection = ledger.connectDatabase(legacyPath)
        try:
            ownerId = createPreProfileOwner(connection, "Legacy", "legacy password")
            ledgerId = connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (ownerId,)).fetchone()[0]
            ledger.bindLedger(connection, ledgerId)
            with connection:
                connection.execute("INSERT INTO integrationSecrets (label, secret) VALUES ('Existing', 'synthetic-old-secret')")
                connection.execute("INSERT INTO integrationSecrets (id, label, secret) VALUES (99, 'Deleted historical key', 'synthetic-deleted-secret')")
                connection.execute("DELETE FROM integrationSecrets WHERE id = 99")
            originalAudit = [tuple(row) for row in connection.execute("SELECT * FROM auditEvents")]
        finally:
            connection.close()
        ledger.initializeDatabase(legacyPath)
        connection = ledger.connectDatabase(legacyPath)
        try:
            self.assertEqual((ownerId, 1, "synthetic-old-secret", 1, 0), tuple(connection.execute("SELECT creatorUserId, creatorMigrationAssigned, secret, active, revoked FROM integrationSecrets").fetchone()))
            self.assertEqual(originalAudit, [tuple(row) for row in connection.execute("SELECT * FROM auditEvents")])
            self.assertEqual(99, connection.execute("SELECT seq FROM sqlite_sequence WHERE name = 'integrationSecrets'").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()

    def testConcurrentInvitationQuotaAndDecision(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier = threading.Barrier(8)
        def attempt(index):
            connection = ledger.connectDatabase(self.path)
            try:
                ledger.bindLedger(connection, self.ledgerId, "user", self.owner, "Owner")
                barrier.wait()
                try:
                    ledger.sendSharingInvitation(connection, "Missing " + str(index), "member", now=2000000000)
                except ledger.InvitationRateLimit:
                    return "limited"
                except ledger.ValidationError:
                    return "counted"
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(8)))
        self.assertEqual(5, results.count("counted"))
        self.assertEqual(3, results.count("limited"))
        self.connection.execute("DELETE FROM invitationAttempts")
        self.connection.commit()
        invitationId = self.invite()
        def acceptOnce(index):
            connection = ledger.connectDatabase(self.path)
            try:
                ledger.bindLedger(connection, self.otherLedger, "user", self.otherOwner, "Other")
                try:
                    ledger.decideSharingInvitation(connection, invitationId, self.otherOwner, "accept")
                    return True
                except ledger.SubmissionConflict:
                    return False
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual([False, True], sorted(executor.map(acceptOnce, range(2))))
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE ledgerId = ? AND userId = ?", (self.ledgerId, self.otherOwner)).fetchone()[0])

    def testMemberFinancialPendingAndTransferIntegrity(self):
        member = self.accept(self.invite())
        cash = ledger.createAccount(self.connection, "Cash", "cash", openingBalance="100")
        savings = ledger.createAccount(self.connection, "Savings", "cash")
        category = ledger.createCategory(self.connection, "Expense")
        self.assertEqual(200, member.get("/pending").status_code)
        self.assertEqual(200, self.login("Auditor", "auditor password").get("/pending").status_code)
        self.assertEqual(200, member.post(f"/api/accounts/{cash}/reconciliation", json={"actualBalance": "150"}).status_code)
        transaction = ledger.createTransaction(self.connection, cash, "2026-10-22", "Owner expense", category, "20")
        snapshot = member.get(f"/api/transactions/{transaction}/pending-snapshot").get_json()["snapshot"]
        moved = member.post(f"/api/transactions/{transaction}/move-to-pending", json={"requestId": "synthetic-move", "snapshot": snapshot})
        self.assertEqual(201, moved.status_code, moved.get_json())
        pending = member.get("/api/pending").get_json()["rows"]
        decisions = [{"id": row["id"], **{key: row[key] for key in ledger.pendingFields(row["kind"])}, "reviewedRole": row["currentRole"], "reviewVersion": row["reviewVersion"]} for row in pending]
        response = member.post("/api/pending/decision", json={"decision": "approve", "rows": decisions})
        self.assertEqual(200, response.status_code, response.get_json())
        transferId = ledger.createTransfer(self.connection, "2026-10-22", cash, savings, "10", "Owner transfer")
        self.assertEqual(200, member.delete(f"/api/transfers/{transferId}").status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions WHERE transferId = ?", (transferId,)).fetchone()[0])

    def testLedgerCreateAndMigrationFailureRollBack(self):
        from unittest.mock import patch
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM ledgers ORDER BY id")]
        self.connection.execute("CREATE TRIGGER failCreationAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'ledgers' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
        self.connection.commit()
        self.assertEqual(500, self.login().post("/api/ledgers", json={"name": "Unsaved"}).status_code)
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM ledgers ORDER BY id")])
        legacyPath = Path(self.directory.name) / "legacy-rollback.db"
        with patch.object(ledger, "schemaVersion", 26), patch.object(ledger, "migrateToVersion27"):
            ledger.initializeDatabase(legacyPath)
        connection = ledger.connectDatabase(legacyPath)
        try:
            owner = createPreProfileOwner(connection, "Legacy", "legacy password")
            # A permitted empty initial schema has no keys; inject a migration failure
            # after the table rebuild to prove rollback restores that exact schema.
            original = connection.iterdump()
            baseline = "\n".join(original)
        finally:
            connection.close()
        originalMigration = ledger.migrateToVersion27
        def failingMigration(connection):
            originalMigration(connection)
            raise RuntimeError("synthetic migration failure")
        with patch.object(ledger, "migrateToVersion27", side_effect=failingMigration):
            with self.assertRaises(RuntimeError):
                ledger.initializeDatabase(legacyPath)
        connection = ledger.connectDatabase(legacyPath)
        try:
            self.assertEqual(26, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(baseline, "\n".join(connection.iterdump()))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
