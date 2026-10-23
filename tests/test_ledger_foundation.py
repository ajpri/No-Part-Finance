"""Ledger boundaries are tested with known foreign IDs, never real financial data."""
import tests
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
from app import createApp
from packageApplication import applicationVersion
from users import createUser, passwordHash


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
                self.assertEqual(400, self.client.get("/api/backup?disableIntegrationKeys=true").status_code)
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
