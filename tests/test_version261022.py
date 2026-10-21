"""Release acceptance uses only guarded synthetic databases and export paths."""
import tests

import json
import re
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import database as ledger
from app import createApp, runApplication
from users import (createOwner, createUser, renameUser, resetPassword, setAuditorAccess,
                   manageUsers, selectRecord)


class UserProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.owner = createOwner(self.connection, "Owner", "owner password")
        self.firstLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.owner,)).fetchone()[0]
        ledger.bindLedger(self.connection, self.firstLedger)
        self.auditor = createUser(self.connection, "auditor", "Auditor", "auditor password")
        self.otherAuditor = createUser(self.connection, "auditor", "Other Auditor", "auditor password")
        self.application = createApp(self.path)

    def login(self, username="Owner", password="owner password", client=None):
        client = client or self.application.test_client()
        token = re.search('name="csrfToken" value="([^"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        response = client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token})
        if response.status_code == 302:
            client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client, response

    def testProvisioningPreservesInitialAndCreatesIndependentLedger(self):
        account = ledger.createAccount(self.connection, "Local", "cash", openingBalance="12.34")
        secondOwner = createOwner(self.connection, "Second", "second password")
        secondLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (secondOwner,)).fetchone()[0]
        self.assertNotEqual(self.firstLedger, secondLedger)
        ledger.bindLedger(self.connection, secondLedger)
        self.assertEqual([("External", "system")], [tuple(row) for row in self.connection.execute("SELECT name, accountClass FROM scopedaccounts")])
        for table in ledger.ledgerTables:
            if table != "accounts":
                self.assertEqual(0, self.connection.execute(f"SELECT COUNT(*) FROM scoped{table}").fetchone()[0], table)
        ledger.bindLedger(self.connection, self.firstLedger)
        self.assertEqual(1234, self.connection.execute("SELECT balanceOffsetCents FROM scopedaccounts WHERE id = ?", (account,)).fetchone()[0])
        self.assertEqual(self.firstLedger, self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.owner,)).fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())

    def testProvisioningAndChangesRollbackWithAuditFailure(self):
        original = list(self.connection.iterdump())
        self.connection.execute("CREATE TRIGGER failAudit BEFORE INSERT ON auditEvents BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        baseline = list(self.connection.iterdump())
        for operation in (
            lambda: createOwner(self.connection, "Second", "second password"),
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
        self.assertEqual(401, self.login("Auditor", "auditor password")[1].status_code)
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
                       lambda: createOwner(self.connection, "STRASSE", "owner password")):
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
            createUser(self.connection, "auditor", f"Person {index:02}", "auditor password")
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
        responses = ["2", "Second", "3", "2", "Second Auditor", "1", "s", "Second Auditor", "1", "1", "Renamed Auditor", "0", "0"]
        with patch("users.input", side_effect=responses), patch("users.getpass.getpass", return_value="valid password"), patch("builtins.print") as output:
            manageUsers(self.path)
        self.assertIn("User management does not start the server", str(output.call_args_list))
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM users WHERE username = 'Renamed Auditor'").fetchone())
        self.assertNotIn("valid password", str(output.call_args_list))

    def testMultipleLedgerBrowserIntegrationAndPortableDownloads(self):
        secondOwner = createOwner(self.connection, "Second", "second password")
        secondLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (secondOwner,)).fetchone()[0]
        ledger.bindLedger(self.connection, secondLedger)
        createUser(self.connection, "auditor", "Second Auditor", "auditor password")
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
        self.connection.execute("ALTER TABLE ledgerAccess DROP COLUMN enabled")
        self.connection.execute("PRAGMA user_version = 22")
        self.connection.commit()
        before = list(self.connection.iterdump())
        def failUpgrade(connection):
            ledger.migrateToVersion23(connection)
            raise RuntimeError("synthetic migration failure")
        originalMigration = ledger.migrateToVersion23
        def failingMigration(connection):
            originalMigration(connection)
            raise RuntimeError("synthetic migration failure")
        with patch("database.migrateToVersion23", side_effect=failingMigration):
            with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                ledger.initializeDatabase(self.path)
        self.assertEqual(before, list(self.connection.iterdump()))
        ledger.initializeDatabase(self.path)
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE enabled = 1").fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
