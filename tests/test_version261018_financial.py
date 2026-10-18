"""Synthetic failure-preservation and financial regression checks for 26.10.18."""
import tests
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database


class UpgradeBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.databasePath = Path(self.directory.name) / "explicit-upgrade.db"
        database.initializeDatabase(self.databasePath)
        with database.connectDatabase(self.databasePath) as connection:
            connection.execute("INSERT INTO accounts (name, accountClass, balanceOffsetCents) VALUES ('Synthetic Cash', 'cash', 12345)")
            connection.execute("INSERT INTO categories (name) VALUES ('Synthetic Expense')")
            connection.execute("INSERT INTO transactions (accountId, date, description, amountCents, categoryId) SELECT a.id, '2026-10-17', 'Synthetic purchase', -321, c.id FROM accounts a, categories c WHERE a.name = 'Synthetic Cash'")
            connection.execute("INSERT INTO users (role, username, usernameKey, passwordHash) VALUES ('owner', 'Fictional', 'fictional', 'fictional-hash')")
            connection.execute("INSERT INTO integrationSecrets (label, secret, active) VALUES ('Fictional revoked', 'fictional-secret', 0)")
            cashId = connection.execute("SELECT id FROM accounts WHERE name = 'Synthetic Cash'").fetchone()[0]
            categoryId = connection.execute("SELECT id FROM categories").fetchone()[0]
            connection.execute("INSERT INTO transactionTemplates (name, accountId, categoryId, enteredAmountCents) VALUES ('Synthetic template', ?, ?, 321)", (cashId, categoryId))
            connection.execute("INSERT INTO flexibleCategoryBudgets (categoryId, monthlyTargetCents) VALUES (?, 5000)", (categoryId,))
            connection.execute("INSERT INTO simulatorPlans (name, planDate) VALUES ('Synthetic plan', '2026-10-17')")
            connection.execute("INSERT INTO simulatorRows (planId, rowType, rowOrder, fromAccountId) VALUES (1, 'live', 0, ?)", (cashId,))
            connection.execute("INSERT INTO pendingSubmissions (integrationId, submissionId, originalContent, reviewContent, reviewVersion, originalRole) VALUES (1, 'synthetic-proposal', '{}', '{}', 4, 'expense')")
            connection.execute("PRAGMA user_version = 20")
        self.originalState = self.snapshot()

    def snapshot(self):
        connection = sqlite3.connect(self.databasePath)
        try:
            return list(connection.iterdump())
        finally:
            connection.close()

    def failingConnection(self, failureMode):
        class FaultConnection(sqlite3.Connection):
            def backup(connection, target, **keywords):
                if failureMode == "empty":
                    return
                if failureMode == "corrupt":
                    super().backup(target, **keywords)
                    target.execute("PRAGMA writable_schema = ON")
                    target.execute("UPDATE sqlite_master SET rootpage = 999999 WHERE name = 'accounts'")
                    target.commit()
                    target.execute("PRAGMA writable_schema = OFF")
                    schemaRevision = target.execute("PRAGMA schema_version").fetchone()[0]
                    target.execute(f"PRAGMA schema_version = {schemaRevision + 1}")
                    return
                if failureMode == "partial":
                    target.execute("CREATE TABLE partial (id INTEGER)")
                    target.commit()
                raise RuntimeError("injected backup interruption")
        connection = sqlite3.connect(self.databasePath, factory=FaultConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def testFailedCandidateNeverRestoresOrMigrates(self):
        for failureMode in ["interrupted", "partial", "empty", "corrupt"]:
            with self.subTest(failureMode=failureMode):
                with patch.object(database, "connectDatabase", return_value=self.failingConnection(failureMode)), patch.object(database, "migrateToVersion21") as migration, patch.object(database.sqlite3, "connect", wraps=sqlite3.connect) as connect:
                    with self.assertRaises((RuntimeError, sqlite3.DatabaseError)):
                        database.initializeDatabase(self.databasePath)
                    migration.assert_not_called()
                    self.assertEqual(1, connect.call_count, "Failed candidate must never be reopened for recovery")
                self.assertEqual(self.originalState, self.snapshot())

    def testBackupCreationFailurePreservesSource(self):
        actualConnect = sqlite3.connect
        def failCreation(path, **keywords):
            if "pre-migration" in str(path):
                raise OSError("injected creation failure")
            return actualConnect(path, **keywords)
        with patch.object(database.sqlite3, "connect", side_effect=failCreation), patch.object(database, "migrateToVersion21") as migration:
            with self.assertRaisesRegex(OSError, "creation failure"):
                database.initializeDatabase(self.databasePath)
            migration.assert_not_called()
        self.assertEqual(self.originalState, self.snapshot())

    def testMigrationFailureRestoresCompleteSnapshot(self):
        def failMigration(connection):
            connection.execute("DELETE FROM transactions")
            connection.execute("UPDATE integrationSecrets SET active = 1")
            connection.commit()
            raise RuntimeError("injected migration failure")
        with patch.object(database, "migrateToVersion21", side_effect=failMigration):
            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                database.initializeDatabase(self.databasePath)
        self.assertEqual(self.originalState, self.snapshot())
        self.assertEqual(1, len(list((self.databasePath.parent / "backups").glob("*.db"))))

    def testPostMigrationVerificationFailureRestoresSnapshot(self):
        class VerifyFault(sqlite3.Connection):
            def execute(connection, statement, *arguments):
                if statement == "PRAGMA foreign_key_check":
                    raise RuntimeError("injected final verification failure")
                return super().execute(statement, *arguments)
        connection = sqlite3.connect(self.databasePath, factory=VerifyFault)
        connection.row_factory = sqlite3.Row
        with patch.object(database, "connectDatabase", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "final verification failure"):
                database.initializeDatabase(self.databasePath)
        self.assertEqual(self.originalState, self.snapshot())

    def testRecoveryFailureReportsBothErrorsAndRetainsVerifiedSnapshot(self):
        actualConnect = sqlite3.connect
        def failRecovery(path, **keywords):
            if str(path).startswith("file:"):
                raise OSError("injected recovery failure")
            return actualConnect(path, **keywords)
        with patch.object(database.sqlite3, "connect", side_effect=failRecovery), patch.object(database, "migrateToVersion21", side_effect=RuntimeError("injected migration failure")):
            with self.assertRaisesRegex(RuntimeError, "migration failure.*recovery failed.*recovery failure.*verified backup retained"):
                database.initializeDatabase(self.databasePath)
        backupPaths = list((self.databasePath.parent / "backups").glob("*.db"))
        self.assertEqual(1, len(backupPaths))
        with actualConnect(backupPaths[0]) as snapshot:
            self.assertEqual(self.originalState, list(snapshot.iterdump()))

    def testSuccessfulUpgradeAndCurrentInitialization(self):
        database.initializeDatabase(self.databasePath)
        database.initializeDatabase(self.databasePath)
        with database.connectDatabase(self.databasePath) as connection:
            self.assertEqual(database.schemaVersion, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(12345, connection.execute("SELECT balanceOffsetCents FROM accounts WHERE name = 'Synthetic Cash'").fetchone()[0])
        self.assertEqual(1, len(list((self.databasePath.parent / "backups").glob("*.db"))))

    def testNewerSchemaRejectedWithoutBackup(self):
        with database.connectDatabase(self.databasePath) as connection:
            connection.execute(f"PRAGMA user_version = {database.schemaVersion + 1}")
        originalState = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            database.initializeDatabase(self.databasePath)
        self.assertEqual(originalState, self.snapshot())
        self.assertFalse((self.databasePath.parent / "backups").exists())

    def testNewDatabaseDoesNotCreateMigrationBackup(self):
        newPath = Path(self.directory.name) / "new-installation" / "explicit-new.db"
        database.initializeDatabase(newPath)
        self.assertFalse((newPath.parent / "backups").exists())


class CategoryRollupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.databasePath = Path(self.directory.name) / "explicit-rollups.db"
        database.initializeDatabase(self.databasePath)
        self.connection = database.connectDatabase(self.databasePath)
        self.addCleanup(self.connection.close)
        self.accountId = database.createAccount(self.connection, "Fictional Cash", "cash")
        self.parentId = database.createCategory(self.connection, "Fictional Parent")
        self.childId = database.createCategory(self.connection, "Fictional Child")

    def addExpense(self, categoryId, amount="10.00", transactionDate="2020-01-01"):
        return database.createTransaction(self.connection, self.accountId, transactionDate, "Fictional purchase", categoryId, amount)

    def testAnyHistoryBlocksCreationAndReparentingWithoutChanges(self):
        self.addExpense(self.parentId)
        self.addExpense(self.parentId, "-10.00")
        with self.connection:
            self.connection.execute("UPDATE categories SET active = 0 WHERE id = ?", (self.parentId,))
        originalState = list(self.connection.iterdump())
        for operation in [lambda: database.createCategory(self.connection, "Another Child", self.parentId), lambda: database.updateCategory(self.connection, self.childId, "Fictional Child", self.parentId)]:
            with self.assertRaisesRegex(database.ValidationError, "transaction history.*empty parent"):
                operation()
            self.assertEqual(originalState, list(self.connection.iterdump()))

    def testAllTemplateAndFlexibleReferencesBlockConversion(self):
        for referenceType in ["template", "flexible"]:
            with self.subTest(referenceType=referenceType):
                with self.connection:
                    if referenceType == "template":
                        self.connection.execute("INSERT INTO transactionTemplates (name, categoryId, active) VALUES ('Archived fictional', ?, 0)", (self.parentId,))
                    else:
                        self.connection.execute("INSERT INTO flexibleCategoryBudgets (categoryId, monthlyTargetCents, active) VALUES (?, 1000, 0)", (self.parentId,))
                with self.assertRaisesRegex(database.ValidationError, "references"):
                    database.createCategory(self.connection, "Blocked child", self.parentId)
                with self.assertRaisesRegex(database.ValidationError, "references"):
                    database.updateCategory(self.connection, self.childId, "Fictional Child", self.parentId)
                with self.connection:
                    self.connection.execute("DELETE FROM transactionTemplates")
                    self.connection.execute("DELETE FROM flexibleCategoryBudgets")

    def testEmptyExistingParentRollupPreservesChildHistory(self):
        transactionId = self.addExpense(self.childId)
        self.addExpense(self.childId, "-2.00")
        database.updateCategory(self.connection, self.childId, "Fictional Child", self.parentId)
        report = database.dashboardReport(self.connection, "custom", "2020-01-01", "2020-01-10")
        self.assertIsNone(report["categoryReportingError"])
        parent = next(row for row in report["categories"] if row["id"] == self.parentId)
        self.assertEqual((800, 10000, 80), (parent["amountCents"], parent["percentageHundredths"], parent["perDayCents"]))
        self.assertEqual(self.childId, self.connection.execute("SELECT categoryId FROM transactions WHERE id = ?", (transactionId,)).fetchone()[0])
        with self.assertRaisesRegex(database.ValidationError, "Rollup"):
            self.addExpense(self.parentId)

    def testLegacyRollupErrorIsIndependentOfPeriodAndCanBeCorrected(self):
        purchaseId = self.addExpense(self.parentId)
        refundId = self.addExpense(self.parentId, "-10.00")
        with self.connection:
            self.connection.execute("UPDATE categories SET parentCategoryId = ? WHERE id = ?", (self.parentId, self.childId))
            self.connection.execute("UPDATE categories SET active = 0 WHERE id = ?", (self.parentId,))
        report = database.dashboardReport(self.connection, "custom", "2026-10-01", "2026-10-17")
        self.assertIn('"Fictional Parent"', report["categoryReportingError"])
        self.assertEqual([], report["categories"])
        self.assertEqual(0, report["expensesCents"])
        self.assertTrue(report["accountsByExpense"])
        database.updateTransaction(self.connection, purchaseId, self.accountId, "2020-01-01", "Fictional purchase", self.childId, "10.00")
        self.assertIsNotNone(database.dashboardReport(self.connection)["categoryReportingError"])
        database.updateTransaction(self.connection, refundId, self.accountId, "2020-01-01", "Fictional refund", self.childId, "-10.00")
        self.assertIsNone(database.dashboardReport(self.connection)["categoryReportingError"])
        self.assertEqual(0, sum(row[0] for row in self.connection.execute("SELECT amountCents FROM transactions")))

    def testWriteLockProtectsHierarchyAndTransactionRevalidation(self):
        originalPrepare = database.prepareTransaction
        def stalePrepare(*arguments, **keywords):
            transactionValues = originalPrepare(*arguments, **keywords)
            # A second connection establishes a rollup after preflight but before the write lock.
            otherConnection = database.connectDatabase(self.databasePath)
            try:
                database.createCategory(otherConnection, "Concurrent child", self.parentId)
            finally:
                otherConnection.close()
            return transactionValues
        with patch.object(database, "prepareTransaction", side_effect=stalePrepare):
            with self.assertRaisesRegex(database.ValidationError, "Rollup"):
                self.addExpense(self.parentId)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        observedStatements = []
        self.connection.set_trace_callback(observedStatements.append)
        database.createCategory(self.connection, "Empty rollup")
        self.assertIn("BEGIN IMMEDIATE", observedStatements)

    def testTemplatePreflightRaceCannotReferenceNewRollup(self):
        originalValues = database.optionalTemplateValues
        def staleTemplateValues(*arguments, **keywords):
            templateValues = originalValues(*arguments, **keywords)
            otherConnection = database.connectDatabase(self.databasePath)
            try:
                database.createCategory(otherConnection, "Concurrent template child", self.parentId)
            finally:
                otherConnection.close()
            return templateValues
        with patch.object(database, "optionalTemplateValues", side_effect=staleTemplateValues):
            with self.assertRaisesRegex(database.ValidationError, "Rollup"):
                database.createTemplate(self.connection, "Stale template", None, None, self.parentId, None)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactionTemplates").fetchone()[0])
