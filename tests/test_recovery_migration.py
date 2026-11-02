"""Recovery migration, backup restoration, export, and deletion on synthetic files."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # Enable the in-process production-database guard first.
import database as ledger
from cliDeletion import deleteTarget
from users import createUser


recoveryTables = ("recoveryEligibility", "passwordRecoveryLinks", "passwordRecoveryAttempts")


class RecoveryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="npf-recovery-migration-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "legacy30.db"
        with patch.object(ledger, "schemaVersion", 30):
            ledger.initializeDatabase(self.path)
        connection = ledger.connectInitialLedgerDatabase(self.path)
        try:
            self.owner = createUser(connection, "owner", "verified@example.test", "synthetic owner passphrase", firstName="Verified")
            self.userIds = {}
            for name in ("manual", "unconsumed", "changed", "case", "no-proof", "non-email"):
                username = name if name == "non-email" else name + "@example.test"
                self.userIds[name] = createUser(connection, "auditor", username, "synthetic auditor passphrase", firstName=name)
            self.ledgerId = connection.ledgerId
            self.account = ledger.createAccount(connection, "Historical Cash", "cash", openingBalance="123.45")
            self.category = ledger.createCategory(connection, "Historical Expense", reportingRole="expense")
            ledger.createTransaction(connection, self.account, "2026-10-31", "Historical purchase", self.category, "12.34")
            with connection:
                connection.execute("UPDATE users SET acceptanceMethod = 'email-code' WHERE id = ?", (self.owner,))
                for name, userId in self.userIds.items():
                    connection.execute("UPDATE users SET acceptanceMethod = ? WHERE id = ?", ("manual" if name == "manual" else "email-code", userId))
                challenges = [(self.owner, "verified@example.test", 2),
                    (self.userIds["manual"], "manual@example.test", 2),
                    (self.userIds["unconsumed"], "unconsumed@example.test", None),
                    (self.userIds["changed"], "old@example.test", 2),
                    (self.userIds["case"], "CASE@example.test", 2),
                    (self.userIds["non-email"], "non-email", 2)]
                for userId, address, consumedAt in challenges:
                    connection.execute("INSERT INTO verificationChallenges (userId, purpose, address, verifier, issuedAt, expiresAt, consumedAt, firstName, lastName) VALUES (?, 'registration', ?, 'synthetic verifier', 1, 100, ?, 'Historical', '')", (userId, address, consumedAt))
                for userId in (self.owner, self.userIds["manual"]):
                    connection.execute("INSERT INTO pushSubscriptions (userId, ledgerId, endpoint, subscription, lastProposalId) VALUES (?, ?, ?, ?, 0)", (userId, self.ledgerId, f"https://push.example.test/{userId}", '{"synthetic":"subscription"}'))
        finally:
            connection.close()

    def snapshot(self, path=None):
        connection = ledger.connectDatabase(path or self.path)
        try:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}
        finally:
            connection.close()

    def assertIntegrity(self, connection, version):
        self.assertEqual(version, connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def testMigrationPreservesHistoryPushAndBackfillsOnlyExactConsumedProof(self):
        before = self.snapshot()
        ledger.initializeDatabase(self.path)
        after = self.snapshot()
        for table, rows in before.items():
            if table == "pushSubscriptions":
                self.assertEqual([row + (None, "Unknown client") for row in rows], after[table], table)
            else:
                self.assertEqual(rows, after[table], table)
        connection = ledger.connectDatabase(self.path)
        try:
            self.assertEqual([(self.owner, "verified@example.test", "email-code")], [tuple(row) for row in connection.execute("SELECT * FROM recoveryEligibility")])
            self.assertIntegrity(connection, 31)
        finally:
            connection.close()
        backups = list((self.root / "backups").glob("pre-migration-v30-to-v31-*.db"))
        self.assertEqual(1, len(backups))
        self.assertEqual(before, self.snapshot(backups[0]))
        with sqlite3.connect(backups[0]) as backup:
            self.assertIntegrity(backup, 30)
        ledger.initializeDatabase(self.path)
        self.assertEqual(after, self.snapshot(), "Repeated initialization changes no historical state")
        self.assertEqual(1, len(list((self.root / "backups").glob("*.db"))))

    def testFailedMigrationRestoresVerifiedBackupWithoutPartialTables(self):
        before = self.snapshot()
        originalMigration = ledger.migratePasswordRecovery
        def failAfterCommittedMigration(connection):
            originalMigration(connection)
            connection.execute("CREATE TABLE incompleteRecoveryMigration (value TEXT)")
            connection.commit()
            raise RuntimeError("synthetic recovery migration failure")
        with patch.object(ledger, "migratePasswordRecovery", side_effect=failAfterCommittedMigration):
            with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "synthetic recovery migration failure"):
                ledger.initializeDatabase(self.path)
        self.assertEqual(before, self.snapshot())
        with sqlite3.connect(self.path) as connection:
            self.assertIntegrity(connection, 30)
            for table in (*recoveryTables, "incompleteRecoveryMigration"):
                self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone())
        backups = list((self.root / "backups").glob("pre-migration-v30-to-v31-*.db"))
        self.assertEqual(1, len(backups))
        self.assertEqual(before, self.snapshot(backups[0]))

    def testCombinedUpgradeRollsBackNotificationChangesWithRecoveryFailure(self):
        before = self.snapshot()
        connection = ledger.connectDatabase(self.path)
        try:
            with patch.object(ledger, "migratePasswordRecovery", side_effect=RuntimeError("combined upgrade failure")):
                with self.assertRaisesRegex(RuntimeError, "combined upgrade failure"):
                    ledger.migrateToVersion31(connection)
            self.assertIntegrity(connection, 30)
            self.assertNotIn("installationId", {row[1] for row in connection.execute("PRAGMA table_info(pushSubscriptions)")})
        finally:
            connection.close()
        self.assertEqual(before, self.snapshot())

    def seedRecoveryState(self, connection):
        with connection:
            for userId in (self.owner, self.userIds["manual"]):
                username = connection.execute("SELECT username FROM users WHERE id = ?", (userId,)).fetchone()[0]
                connection.execute("INSERT OR REPLACE INTO recoveryEligibility VALUES (?, ?, 'cli-trust')", (userId, username))
                connection.execute("INSERT INTO passwordRecoveryLinks VALUES (?, ?, ?, 1, 100, 1000, NULL)", (userId, f"PRIVATE_RECOVERY_DIGEST_{userId}", username))
                connection.execute("INSERT INTO passwordRecoveryAttempts VALUES (?, 'PRIVATE_RECOVERY_IP', 100)", (username,))

    def testBrowserExportOmitsRecoveryStateAndLeavesSourceUnchanged(self):
        ledger.initializeDatabase(self.path)
        connection = ledger.connectInitialLedgerDatabase(self.path)
        try:
            self.seedRecoveryState(connection)
            before = self.snapshot()
            sourceHash = hashlib.sha256(self.path.read_bytes()).digest()
            destination = self.root / "browser-export.db"
            ledger.exportLedgerDatabase(connection, destination)
            self.assertEqual(sourceHash, hashlib.sha256(self.path.read_bytes()).digest())
            self.assertEqual(before, self.snapshot())
            with sqlite3.connect(destination) as exported:
                self.assertIntegrity(exported, 31)
                for table in recoveryTables:
                    self.assertEqual(0, exported.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                self.assertEqual(before["transactions"], exported.execute("SELECT * FROM transactions ORDER BY rowid").fetchall())
                self.assertTrue(all(row[0] == "" for row in exported.execute("SELECT passwordHash FROM users")))
            self.assertNotIn(b"PRIVATE_RECOVERY", destination.read_bytes())
        finally:
            connection.close()

    def testCliUserDeletionClearsOnlyTargetsRecoveryAndPreservesFinancialHistory(self):
        ledger.initializeDatabase(self.path)
        connection = ledger.connectInitialLedgerDatabase(self.path)
        try:
            self.seedRecoveryState(connection)
            before = self.snapshot()
        finally:
            connection.close()
        target = self.userIds["manual"]
        backupPath = deleteTarget(self.path, "user", target, str(target), self.root / "deletion-backups")
        connection = ledger.connectDatabase(self.path)
        try:
            for table in ("recoveryEligibility", "passwordRecoveryLinks", "pushSubscriptions", "ledgerAccess", "verificationChallenges"):
                self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table} WHERE userId = ?", (target,)).fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM recoveryEligibility WHERE userId = ?", (self.owner,)).fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM passwordRecoveryLinks WHERE userId = ?", (self.owner,)).fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM pushSubscriptions WHERE userId = ?", (self.owner,)).fetchone()[0])
            self.assertEqual((1, "Deleted user"), tuple(connection.execute("SELECT deleted, username FROM users WHERE id = ?", (target,)).fetchone()))
            self.assertIntegrity(connection, 31)
        finally:
            connection.close()
        after = self.snapshot()
        for table in ("transactions", "accounts", "categories", "ledgers"):
            self.assertEqual(before[table], after[table], table)
        self.assertTrue(all(row in after["auditEvents"] for row in before["auditEvents"]))
        self.assertEqual(before, self.snapshot(backupPath))
        with sqlite3.connect(backupPath) as backup:
            self.assertIntegrity(backup, 31)
