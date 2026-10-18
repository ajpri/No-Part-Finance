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


class ExternalSimulatorAllocationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.databasePath = Path(self.directory.name) / "explicit-external-allocation.db"
        database.initializeDatabase(self.databasePath)
        self.connection = database.connectDatabase(self.databasePath)
        self.addCleanup(self.connection.close)
        self.cashId = database.createAccount(self.connection, "Fictional Cash", "cash", openingBalance="90.00")
        self.externalId = self.connection.execute("SELECT id FROM accounts WHERE systemIdentity = 'external'").fetchone()[0]
        self.planId = database.createSimulatorPlan(self.connection, "Fictional allocation", "2026-10-17")

    def workspace(self):
        return database.simulatorWorkspace(self.connection, self.planId)["plan"]

    def transfer(self, incoming, amount="50.00", cashId=None):
        cashId = cashId or self.cashId
        return database.saveSimulatorRow(self.connection, self.planId, None, "transfer", self.externalId if incoming else cashId, cashId if incoming else self.externalId, label="Fictional boundary transfer", amount=amount)

    def testBothDirectionsSelectedAndUnselected(self):
        for selected in [False, True]:
            for incoming in [False, True]:
                with self.subTest(selected=selected, incoming=incoming):
                    with self.connection:
                        self.connection.execute("DELETE FROM simulatorRows")
                    if selected:
                        database.saveSimulatorRow(self.connection, self.planId, None, "live", self.cashId)
                    self.transfer(incoming)
                    plan = self.workspace()
                    totals = plan["totals"]
                    expectedContribution = (-5000 if incoming else 5000) if selected else 0
                    self.assertEqual(9000 if selected else 0, totals["startingCashCents"])
                    self.assertEqual(expectedContribution, totals["transfersPaymentsCents"])
                    self.assertEqual((9000 if selected else 0) - expectedContribution, totals["unallocatedMoneyCents"])
                    self.assertEqual((0, 0), (totals["plannedIncomeCents"], totals["plannedSpendingCents"]))
                    self.assertEqual([self.cashId], [row["id"] for row in plan["results"]])
                    self.assertEqual(14000 if incoming else 4000, plan["results"][0]["projectedBalanceCents"])
                    self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testChangingLivePoolAndRefreshPreservesTransfer(self):
        rowId = self.transfer(True)
        before = dict(self.connection.execute("SELECT * FROM simulatorRows WHERE id = ?", (rowId,)).fetchone())
        self.assertEqual(0, self.workspace()["totals"]["unallocatedMoneyCents"])
        liveId = database.saveSimulatorRow(self.connection, self.planId, None, "live", self.cashId)
        self.assertEqual(14000, self.workspace()["totals"]["unallocatedMoneyCents"])
        database.deleteSimulatorRow(self.connection, self.planId, liveId)
        self.assertEqual(0, self.workspace()["totals"]["unallocatedMoneyCents"])
        self.assertEqual(before, dict(self.connection.execute("SELECT * FROM simulatorRows WHERE id = ?", (rowId,)).fetchone()))
        otherConnection = database.connectDatabase(self.databasePath)
        try:
            self.assertEqual(0, database.simulatorWorkspace(otherConnection, self.planId)["plan"]["totals"]["unallocatedMoneyCents"])
        finally:
            otherConnection.close()

    def testMultipleCashAccountsOffsetAndNegativeAllocation(self):
        secondCash = database.createAccount(self.connection, "Second fictional cash", "cash", openingBalance="10.00")
        for cashId in [self.cashId, secondCash]:
            database.saveSimulatorRow(self.connection, self.planId, None, "live", cashId)
        self.transfer(True)
        self.transfer(False, cashId=secondCash)
        self.assertEqual(0, self.workspace()["totals"]["transfersPaymentsCents"])
        self.transfer(False, "150.00")
        self.assertEqual(-5000, self.workspace()["totals"]["unallocatedMoneyCents"])
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (secondCash,))
        self.assertEqual(-5000, self.workspace()["totals"]["unallocatedMoneyCents"])

    def testRecordedTransferIsZeroSumAndExcludedFromPlannedTotals(self):
        database.saveSimulatorRow(self.connection, self.planId, None, "live", self.cashId)
        rowId = self.transfer(True)
        database.recordSimulatorTransfer(self.connection, self.planId, rowId, "2026-10-17", self.externalId, self.cashId, "50.00", "Fictional boundary transfer")
        plan = self.workspace()
        self.assertEqual((14000, 0, 14000), (plan["totals"]["startingCashCents"], plan["totals"]["transfersPaymentsCents"], plan["totals"]["unallocatedMoneyCents"]))
        self.assertEqual((2, 0), tuple(self.connection.execute("SELECT COUNT(*), SUM(amountCents) FROM transactions").fetchone()))
        self.assertEqual((0, 0), (database.dashboardReport(self.connection)["incomeCents"], database.dashboardReport(self.connection)["expensesCents"]))

    def testExternalCreditPaymentRetainsAllocationDeduction(self):
        creditId = database.createAccount(self.connection, "Fictional Card", "credit", "Credit Card", openingBalance="100.00")
        database.saveSimulatorRow(self.connection, self.planId, None, "live", self.cashId)
        database.saveSimulatorRow(self.connection, self.planId, None, "transfer", self.externalId, creditId, label="Fictional card payment", amount="50.00")
        plan = self.workspace()
        self.assertEqual((5000, 4000), (plan["totals"]["transfersPaymentsCents"], plan["totals"]["unallocatedMoneyCents"]))
        self.assertEqual(-5000, next(row for row in plan["results"] if row["id"] == creditId)["projectedBalanceCents"])
        self.assertNotIn(self.externalId, [row["id"] for row in plan["results"]])


class UtilizationThresholdTests(unittest.TestCase):
    def testExactThresholdsAndRoundedLabels(self):
        for owedCents, expectedState, expectedLabel in [
            (0, "green", "0.00%"), (-100, "green", "0.00%"),
            (29995, "green", "30.00%"), (29996, "green", "30.00%"),
            (30000, "yellow", "30.00%"), (30001, "yellow", "30.00%"),
            (49996, "yellow", "50.00%"), (50000, "red", "50.00%"),
            (50001, "red", "50.00%"), (125000, "red", "125.00%"),
        ]:
            with self.subTest(owedCents=owedCents):
                status = database.utilizationStatus(owedCents, 100000, "Fictional utilization")
                self.assertEqual((expectedState, expectedLabel), (status["state"], status["label"]))
                self.assertEqual(f"Fictional utilization: {expectedLabel}", status["accessibleLabel"])
        self.assertIsNone(database.utilizationStatus(100, None, "Unavailable"))
        self.assertIsNone(database.utilizationStatus(0, 0, "Unavailable"))
        self.assertEqual("green", database.utilizationStatus(29996000000000000000000, 100000000000000000000000, "Large exact")["state"])

    def testSimulatorUsesSameExactClassificationAtProjectedBoundary(self):
        with tempfile.TemporaryDirectory() as directory:
            databasePath = Path(directory) / "explicit-utilization.db"
            database.initializeDatabase(databasePath)
            connection = database.connectDatabase(databasePath)
            try:
                planId = database.createSimulatorPlan(connection, "Fictional limits", "2026-10-17")
                cardId = database.createAccount(connection, "Fictional Card", "credit", "Credit Card", openingBalance="299.96", creditLimit="1000.00", utilizationWarning="30.00")
                database.saveSimulatorRow(connection, planId, None, "spend", cardId, label="Small fictional charge", amount="200.00")
                result = database.simulatorWorkspace(connection, planId)["plan"]["results"][0]
                self.assertEqual((-49996, "50.00%", "yellow"), (result["projectedBalanceCents"], result["projectedUtilization"]["label"], result["projectedUtilization"]["state"]))
                expected = database.utilizationStatus(49996, 100000, "Fictional Card projected utilization")
                self.assertEqual(expected, result["projectedUtilization"])
                with connection:
                    connection.execute("UPDATE simulatorRows SET amountCents = 20004 WHERE planId = ?", (planId,))
                self.assertEqual("red", database.simulatorWorkspace(connection, planId)["plan"]["results"][0]["projectedUtilization"]["state"])
                self.assertEqual(3000, connection.execute("SELECT utilizationWarningBasisPoints FROM accounts WHERE id = ?", (cardId,)).fetchone()[0])
            finally:
                connection.close()

    def testIneligibleProjectedAccountsHaveNoBadge(self):
        with tempfile.TemporaryDirectory() as directory:
            databasePath = Path(directory) / "explicit-ineligible-utilization.db"
            database.initializeDatabase(databasePath)
            connection = database.connectDatabase(databasePath)
            try:
                planId = database.createSimulatorPlan(connection, "Fictional ineligible", "2026-10-17")
                for name, accountClass, subtype in [("Fictional Cash", "cash", None), ("Fictional Loan", "credit", "Personal Loan"), ("Fictional Other", "credit", "Other"), ("Fictional Unlimited", "credit", "Credit Card")]:
                    accountId = database.createAccount(connection, name, accountClass, subtype)
                    database.saveSimulatorRow(connection, planId, None, "spend", accountId, label="Fictional charge", amount="10.00")
                self.assertTrue(all(row["projectedUtilization"] is None for row in database.simulatorWorkspace(connection, planId)["plan"]["results"]))
            finally:
                connection.close()
