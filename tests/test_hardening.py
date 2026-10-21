from tests.test_support import ownerClient
import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # Activates production-database isolation before importing the application.
from app import createApp, databasePath as configuredApplicationDatabasePath
from database import (
    connectDatabase,
    createAccount,
    createCategory,
    createTransaction,
    initializeDatabase,
    testDatabaseGuardEnabled,
)
from tests.test_support import copyProductionDatabase
from tests.test_support import connectFixtureDatabase as connectDatabase


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.temporaryDirectory.name) / "finance.db"
        initializeDatabase(self.databasePath)
        self.app = createApp(self.databasePath)
        self.client = ownerClient(self.app)

    def tearDown(self):
        self.temporaryDirectory.cleanup()

    def seedTransaction(self):
        connection = connectDatabase(self.databasePath)
        try:
            accountId = createAccount(
                connection, "Checking", "cash", "checking", openingBalance="100.00"
            )
            categoryId = createCategory(connection, "Test Expense")
            createTransaction(
                connection, accountId, "2026-08-19", "Purchase", categoryId, "12.34"
            )
            return accountId, categoryId
        finally:
            connection.close()

    def testBackupIsValidConsistentSQLiteDatabase(self):
        accountId, _ = self.seedTransaction()
        response = self.client.get("/api/backup")
        self.assertEqual(200, response.status_code)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        backupPath = Path(self.temporaryDirectory.name) / "downloaded.db"
        backupPath.write_bytes(response.get_data())
        response.close()
        connection = connectDatabase(backupPath)
        try:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
            balance = connection.execute(
                """
                SELECT a.balanceOffsetCents + COALESCE(SUM(t.amountCents), 0)
                FROM accounts AS a
                LEFT JOIN transactions AS t ON t.accountId = a.id
                WHERE a.id = ? GROUP BY a.id
                """,
                (accountId,),
            ).fetchone()[0]
            self.assertEqual(8766, balance)
        finally:
            connection.close()

    def testDatabaseSurvivesApplicationRestart(self):
        self.seedTransaction()
        firstResponse = self.client.get("/api/accounts")
        firstAccounts = firstResponse.get_json()
        firstResponse.close()
        restartedApp = createApp(self.databasePath)
        restartedClient = ownerClient(restartedApp)
        secondResponse = restartedClient.get("/api/accounts")
        self.assertEqual(firstAccounts, secondResponse.get_json())
        secondResponse.close()
        connection = connectDatabase(self.databasePath)
        try:
            self.assertEqual(26, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()

    def testInvalidWritesFailWithoutPartialFinancialChanges(self):
        accountId, categoryId = self.seedTransaction()
        beforeConnection = connectDatabase(self.databasePath)
        try:
            beforeRows = beforeConnection.execute(
                "SELECT accountId, date, description, amountCents, categoryId, transferId FROM transactions"
            ).fetchall()
        finally:
            beforeConnection.close()
        invalidTransfer = self.client.post("/api/transfers", json={
            "date": "2026-08-19",
            "fromAccountId": accountId,
            "toAccountId": accountId,
            "amount": "25.00",
            "description": "Invalid",
        })
        self.assertEqual(400, invalidTransfer.status_code)
        invalidTransfer.close()
        invalidTransaction = self.client.post("/api/transactions", json={
            "date": "2026-02-30",
            "accountId": accountId,
            "description": "Invalid date",
            "categoryId": categoryId,
            "amount": "10.00",
        })
        self.assertEqual(400, invalidTransaction.status_code)
        invalidTransaction.close()
        afterConnection = connectDatabase(self.databasePath)
        try:
            afterRows = afterConnection.execute(
                "SELECT accountId, date, description, amountCents, categoryId, transferId FROM transactions"
            ).fetchall()
            self.assertEqual([tuple(row) for row in beforeRows], [tuple(row) for row in afterRows])
        finally:
            afterConnection.close()

    def testTotalCreditBalanceNetsPositiveCreditAndExcludesCash(self):
        connection = connectDatabase(self.databasePath)
        try:
            createAccount(connection, "Card Owed", "credit", "Credit Card", openingBalance="100.00", creditLimit="100.00")
            creditAccountId = createAccount(connection, "Overpaid Card", "credit", "Credit Card", creditLimit="200.00")
            cashAccountId = createAccount(
                connection, "Savings", "cash", "savings", openingBalance="999.00"
            )
            categoryId = createCategory(connection, "Refund Category")
            createTransaction(
                connection, creditAccountId, "2026-08-19", "Card credit",
                categoryId, "-25.00",
            )
            createTransaction(
                connection, cashAccountId, "2026-08-19", "Cash activity",
                categoryId, "10.00",
            )
        finally:
            connection.close()
        response = self.client.get("/api/dashboard?period=thisMonth")
        self.assertEqual(200, response.status_code)
        dashboard = response.get_json()
        response.close()
        self.assertEqual(7500, dashboard["totalCreditBalanceCents"])
        self.assertEqual(30000, dashboard["totalCreditLimitCents"])
        self.assertEqual(10000, dashboard["totalRevolvingOwedCents"])


class RealDatabaseIntegrityTests(unittest.TestCase):
    def testRealDatabaseFinancialInvariants(self):
        with tempfile.TemporaryDirectory() as temporaryDirectory:
            databasePath = Path(temporaryDirectory) / "real-data-copy.db"
            copyProductionDatabase(self, databasePath)
            connection = connectDatabase(databasePath)
            try:
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                invalidTransfers = connection.execute(
                    """
                    SELECT transferId
                    FROM transactions
                    WHERE transferId IS NOT NULL
                    GROUP BY transferId
                    HAVING COUNT(*) != 2
                        OR SUM(amountCents) != 0
                        OR SUM(categoryId IS NOT NULL) != 0
                    """
                ).fetchall()
                self.assertEqual([], invalidTransfers)
                invalidOffsets = connection.execute(
                    "SELECT id FROM accounts WHERE typeof(balanceOffsetCents) != 'integer'"
                ).fetchall()
                self.assertEqual([], invalidOffsets)
            finally:
                connection.close()


class ProductionDatabaseIsolationTests(unittest.TestCase):
    def testTestModeRejectsWritableProductionDatabaseConnection(self):
        productionPath = Path(__file__).resolve().parents[1] / "finance.db"
        with self.assertRaisesRegex(RuntimeError, "Tests may not open the production"):
            connectDatabase(productionPath)

    def testTestBootstrapGuardsProductionWithoutChangingNormalDefaultPath(self):
        productionPath = (Path(__file__).resolve().parents[1] / "finance.db").resolve()
        self.assertTrue(testDatabaseGuardEnabled)
        self.assertEqual(productionPath, configuredApplicationDatabasePath.resolve())
        with self.assertRaisesRegex(RuntimeError, "Tests may not open the production"):
            createApp()

    def testImportingApplicationDoesNotInitializeConfiguredDefaultDatabase(self):
        with tempfile.TemporaryDirectory() as temporaryDirectory:
            untouchedPath = Path(temporaryDirectory) / "must-not-exist.db"
            environment = os.environ.copy()
            environment["FINANCE_DATABASE_PATH"] = str(untouchedPath)
            subprocess.run(
                [sys.executable, "-c", "import app"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                check=True,
            )
            self.assertFalse(untouchedPath.exists())

    def testExplicitTestDatabaseNeverFallsBackToProductionPath(self):
        with tempfile.TemporaryDirectory() as temporaryDirectory:
            temporaryPath = Path(temporaryDirectory)
            explicitPath = temporaryPath / "explicit-test.db"
            productionPath = temporaryPath / "production-finance.db"
            with patch("app.databasePath", productionPath):
                createApp(explicitPath)
            self.assertTrue(explicitPath.exists())
            self.assertFalse(productionPath.exists())

    def testProductionCopyUsesSeparateTemporaryDatabaseAndLeavesSourceUnchanged(self):
        with tempfile.TemporaryDirectory() as temporaryDirectory:
            copiedPath = Path(temporaryDirectory) / "copied-production.db"
            copyProductionDatabase(self, copiedPath)
            self.assertTrue(copiedPath.exists())
            writableCopy = connectDatabase(copiedPath)
            try:
                writableCopy.execute("CREATE TABLE isolationProof (id INTEGER)")
                writableCopy.commit()
            finally:
                writableCopy.close()


if __name__ == "__main__":
    unittest.main()
