"""Release acceptance uses guarded, synthetic databases only."""
import tests

import re
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

import database as ledger
from app import createApp
from users import createOwner, createUser, renameUser


class LedgerNameTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.owner = createOwner(self.connection, "Owner", "owner password")
        self.ledgerId = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.owner,)).fetchone()[0]
        ledger.bindLedger(self.connection, self.ledgerId)
        self.auditor = createUser(self.connection, "auditor", "Auditor", "auditor password")
        self.application = createApp(self.path)

    def login(self, username="Owner", password="owner password"):
        client = self.application.test_client()
        token = re.search('name="csrfToken" value="([^"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        self.assertEqual(302, client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token}).status_code)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client

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
        otherOwner = createOwner(self.connection, "Other", "owner password")
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


class SharingTests(unittest.TestCase):
    login = LedgerNameTests.login

    def setUp(self):
        LedgerNameTests.setUp(self)
        self.otherOwner = createOwner(self.connection, "Other", "other password")
        self.otherLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.otherOwner,)).fetchone()[0]
        ledger.bindLedger(self.connection, self.ledgerId, "user", self.owner, "Owner")

    def invite(self, username="Other", role="member", client=None):
        client = client or self.login()
        client.environ_base["HTTP_X_LEDGER_ID"] = self.ledgerId
        response = client.post("/api/invitations", json={"username": username, "role": role})
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()["id"]

    def accept(self, invitationId):
        client = self.login("Other", "other password")
        self.assertEqual(200, client.post(f"/api/invitations/{invitationId}/decision", json={"decision": "accept"}).status_code)
        client.environ_base["HTTP_X_LEDGER_ID"] = self.ledgerId
        return client

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
        noAccessId = createUser(self.connection, "auditor", "No access", "no access password")
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
            ownerId = createOwner(connection, "Legacy", "legacy password")
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
            owner = createOwner(connection, "Legacy", "legacy password")
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


class FullInstallationPackagingTests(unittest.TestCase):
    setUp = SharingTests.setUp
    login = LedgerNameTests.login
    invite = SharingTests.invite
    accept = SharingTests.accept

    def testCompleteSnapshotAndLinuxLauncherPreserveEveryLedgerAndCredential(self):
        import os
        import subprocess
        from packageApplication import createApplicationPackage
        from users import setUserEnabled
        self.accept(self.invite())
        ledger.bindLedger(self.connection, self.ledgerId, "user", self.owner, "Owner")
        cash = ledger.createAccount(self.connection, "Synthetic cash", "cash", openingBalance="123.45")
        savings = ledger.createAccount(self.connection, "Synthetic savings", "cash")
        category = ledger.createCategory(self.connection, "Synthetic expense")
        ledger.createTransaction(self.connection, cash, "2026-10-22", "Synthetic expense", category, "12.34")
        ledger.createTransfer(self.connection, "2026-10-22", cash, savings, "10", "Synthetic transfer")
        keys = [ledger.createIntegrationSecret(self.connection, label) for label in ("Enabled", "Disabled", "Revoked")]
        ledger.setIntegrationState(self.connection, keys[1], False)
        with self.connection:
            self.connection.execute("UPDATE integrationSecrets SET active = 0, revoked = 1 WHERE id = ?", (keys[2],))
        ledger.submitPendingEntry(self.connection, None, {"submissionId": "synthetic-package-pending", "date": "2026-10-22", "accountId": cash, "description": "Synthetic pending", "categoryId": category, "amount": "1.23", "pinned": False})
        recipient = createOwner(self.connection, "Recipient", "recipient password")
        self.invite("Recipient")
        setUserEnabled(self.connection, self.auditor, False)
        secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (keys[0],)).fetchone()[0]
        baseline = list(self.connection.iterdump())
        root = Path(self.directory.name)
        projectRoot = Path(__file__).resolve().parents[1]
        result = createApplicationPackage(repositoryPath=projectRoot, databasePath=self.path, outputPath=root / "full-output")
        with zipfile.ZipFile(result["archivePath"]) as archive:
            archive.extractall(root / "extracted")
        extractedRoot = root / "extracted" / "No-Part-Finance"
        packagedPath = extractedRoot / "finance.db"
        packaged = ledger.connectDatabase(packagedPath)
        try:
            self.assertEqual(baseline, list(packaged.iterdump()))
            self.assertEqual(3, packaged.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0])
            self.assertEqual(4, packaged.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            self.assertEqual(2, packaged.execute("SELECT COUNT(*) FROM sharingInvitations").fetchone()[0])
        finally:
            packaged.close()
        originalApplication = self.application
        self.application = createApp(packagedPath)
        self.assertEqual(200, self.login().get("/api/session").status_code)
        self.assertEqual(200, self.login("Other", "other password").get("/api/session").status_code)
        self.assertEqual(200, self.application.test_client().get("/api/integration/read/accounts", headers={"Authorization": "Bearer " + secret}).status_code)
        self.application = originalApplication
        # Refresh the disposable extracted source using supported SQLite backup,
        # then run the real Linux wrapper. Its DB override must remain ignored.
        packaged = sqlite3.connect(packagedPath)
        try:
            self.connection.backup(packaged)
        finally:
            packaged.close()
        completed = subprocess.run(["sh", str(extractedRoot / "packageLinux.sh"), "--output", str(root / "linux-output")], cwd=root,
                                   env={**os.environ, "FINANCE_DATABASE_PATH": str(root / "unused-override.db")}, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("complete unencrypted financial database", completed.stdout)
        self.assertIn("credentials excluded", completed.stdout)
        linuxArchive = next((root / "linux-output").glob("*.zip"))
        with zipfile.ZipFile(linuxArchive) as archive:
            snapshotPath = root / "linux-snapshot.db"
            snapshotPath.write_bytes(archive.read("No-Part-Finance/finance.db"))
        with sqlite3.connect(snapshotPath) as snapshot:
            self.assertEqual(baseline, list(snapshot.iterdump()))
        self.assertFalse((root / "unused-override.db").exists())
        self.assertEqual(baseline, list(self.connection.iterdump()))
        windowsScript = (projectRoot / "packageWindows.bat").read_text()
        linuxScript = (projectRoot / "packageLinux.sh").read_text()
        self.assertIn("packageApplication.py %*", windowsScript)
        self.assertIn('packageApplication.py "$@"', linuxScript)
        self.assertNotIn("--no-db", windowsScript)
        self.assertNotIn("--no-db", linuxScript)
