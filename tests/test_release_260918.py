from tests.test_support import useHistoricalSchema21
from tests.test_support import ownerClient
import tempfile
import unittest
from pathlib import Path

import tests
from app import createApp
from database import (
    ValidationError, connectDatabase, createAccount, createCategory, createTransaction,
    createTransfer, updateTransfer, deleteTransaction, reconcileAccountBalance,
    createTemplate, createBulkTransactions, createTransactionWithReimbursement,
    createSimulatorPlan, saveSimulatorRow, simulatorWorkspace, dashboardReport, initializeDatabase,
)
from tests.test_support import connectFixtureDatabase as connectDatabase


class ExternalSystemAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.temporaryDirectory.name) / "finance.db"
        self.app = createApp(self.databasePath)
        self.client = ownerClient(self.app)
        self.connection = connectDatabase(self.databasePath)
        self.cashId = createAccount(self.connection, "Checking", "cash")
        self.categoryId = createCategory(self.connection, "Expense")
        self.externalId = self.connection.execute(
            "SELECT id FROM accounts WHERE systemIdentity = 'external'"
        ).fetchone()[0]

    def tearDown(self):
        self.connection.close()
        self.temporaryDirectory.cleanup()

    def testExternalIsReadOnlyTransferBoundary(self):
        external = self.connection.execute(
            "SELECT name, accountClass, accountSubtype, active, balanceOffsetCents FROM accounts WHERE id = ?",
            (self.externalId,),
        ).fetchone()
        self.assertEqual(("External", "system", None, 1, 0), tuple(external))
        transfer = self.client.post("/api/transfers", json={
            "date": "2026-09-18", "fromAccountId": self.cashId, "toAccountId": self.externalId,
            "amount": "12.34", "description": "Outside transfer",
        })
        self.assertEqual(201, transfer.status_code)
        rows = self.connection.execute(
            "SELECT accountId, amountCents FROM transactions WHERE transferId = ? ORDER BY amountCents",
            (transfer.get_json()["transferId"],),
        ).fetchall()
        self.assertEqual([(self.cashId, -1234), (self.externalId, 1234)], [tuple(row) for row in rows])
        self.assertEqual(0, sum(row[1] for row in rows))
        with self.assertRaisesRegex(ValidationError, "only be used for transfers"):
            createTransaction(self.connection, self.externalId, "2026-09-18", "No", self.categoryId, "1.00")
        self.assertEqual(400, self.client.patch(f"/api/accounts/{self.externalId}", json={}).status_code)
        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertNotIn(self.externalId, [account["id"] for account in dashboard["accounts"]])
        listed = self.client.get("/api/accounts").get_json()
        self.assertEqual("system", next(account for account in listed if account["id"] == self.externalId)["accountClass"])
        javascript = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertIn('selected.accountClass!=="system"', javascript)
        self.assertIn('account.accountClass !== "system"', javascript)

    def testOrdinaryEditsCannotMoveCategorizedOrUncategorizedHistoryToExternal(self):
        transactionId = createTransaction(self.connection, self.cashId, "2026-10-01", "Recorded cost", self.categoryId, "1.00")
        historicalId = self.connection.execute(
            "INSERT INTO transactions (accountId, date, description, amountCents) VALUES (?, '2020-01-01', 'Balance Adjustment', 100)",
            (self.cashId,),
        ).lastrowid
        self.connection.commit()
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        for rowId, categoryId in ((transactionId, self.categoryId), (historicalId, None)):
            with self.subTest(categoryId=categoryId):
                response = self.client.patch(f"/api/transactions/{rowId}", json={
                    "accountId": self.externalId, "date": "2026-10-01", "description": "Moved to boundary",
                    "categoryId": categoryId, "amount": "1.00",
                })
                self.assertEqual(400, response.status_code)
                self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])

    def testExternalTransfersBothDirectionsEditDeleteAndReportingExclusion(self):
        creditId = createAccount(self.connection, "Independent debt", "credit", "Personal Loan", openingBalance="20.00")
        reportBefore = dashboardReport(self.connection, "custom", "2026-10-01", "2026-10-02")
        for trackedId in (self.cashId, creditId):
            for fromId, toId in ((self.externalId, trackedId), (trackedId, self.externalId)):
                with self.subTest(fromId=fromId, toId=toId):
                    transferId = createTransfer(self.connection, "2026-10-01", fromId, toId, "12.34", "Boundary movement")
                    updateTransfer(self.connection, transferId, "2026-10-02", toId, fromId, "5.00", "Reversed movement")
                    rows = self.connection.execute("SELECT * FROM transactions WHERE transferId = ? ORDER BY amountCents", (transferId,)).fetchall()
                    self.assertEqual([(toId, -500), (fromId, 500)], [(row["accountId"], row["amountCents"]) for row in rows])
                    self.assertEqual(2, len(rows))
                    self.assertTrue(all(row["categoryId"] is None and row["date"] == "2026-10-02" and row["description"] == "Reversed movement" for row in rows))
                    expectedTrackedBalance = (0 if trackedId == self.cashId else -2000) + (500 if fromId == trackedId else -500)
                    accounts = {row["id"]: row for row in self.client.get("/api/accounts").get_json()}
                    self.assertEqual(expectedTrackedBalance, accounts[trackedId]["balanceCents"])
                    self.assertEqual(-500 if fromId == trackedId else 500, accounts[self.externalId]["balanceCents"])
                    dashboard = self.client.get("/api/dashboard?period=custom&startDate=2026-10-01&endDate=2026-10-02").get_json()
                    self.assertNotIn(self.externalId, [account["id"] for account in dashboard["accounts"]])
                    reportAfter = dashboardReport(self.connection, "custom", "2026-10-01", "2026-10-02")
                    for key in ("expensesCents", "incomeCents", "netCents", "categories", "accountsByExpense", "accountsByIncome", "accountsByNetChange"):
                        self.assertEqual(reportBefore[key], reportAfter[key])
                    self.assertEqual(-expectedTrackedBalance if trackedId == creditId else 2000, dashboard["totalCreditBalanceCents"])
                    deleteTransaction(self.connection, rows[1]["id"])
                    self.assertEqual([], self.connection.execute("SELECT * FROM transactions WHERE transferId = ?", (transferId,)).fetchall())
        with self.assertRaises(ValidationError):
            createTransfer(self.connection, "2026-10-01", self.externalId, self.externalId, "1.00")
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def testExternalRejectsNonTransferWritesAndRemainsUniqueAfterRestart(self):
        incomeId = createCategory(self.connection, "Returned funds", reportingRole="income")
        self.connection.execute("UPDATE categories SET offerReimbursement = 1 WHERE id = ?", (self.categoryId,))
        self.connection.execute("INSERT INTO applicationSettings (key, value) VALUES ('reimbursementCategoryId', ?)", (str(incomeId),))
        self.connection.commit()
        planId = createSimulatorPlan(self.connection, "Boundary fixture", "2026-10-01")
        for operation in ("reconcile", "template", "bulk", "reimbursement", "live", "income", "spend", "buffer", "payoff"):
            with self.subTest(operation=operation):
                with self.assertRaises(ValidationError):
                    if operation == "reconcile":
                        reconcileAccountBalance(self.connection, self.externalId, "1.00")
                    elif operation == "template":
                        createTemplate(self.connection, "Boundary preset", self.externalId, "No", self.categoryId, "1.00")
                    elif operation == "bulk":
                        createBulkTransactions(self.connection, [{"date": "2026-10-01", "accountId": self.externalId,
                            "description": "No", "categoryId": self.categoryId, "amount": "1.00"}])
                    elif operation == "reimbursement":
                        createTransactionWithReimbursement(self.connection, self.cashId, "2026-10-01", "Shared cost", self.categoryId,
                            "1.00", False, self.externalId, "2026-10-01", "Returned", "1.00")
                    else:
                        saveSimulatorRow(self.connection, planId, None, operation,
                            fromAccountId=self.cashId if operation == "payoff" else self.externalId,
                            toAccountId=self.externalId, label="No", amount="1.00")
                self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
                self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM simulatorRows").fetchone()[0])
        for name, accountClass in (("External", "cash"), (" external ", "credit"), ("Boundary", "system")):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    createAccount(self.connection, name, accountClass, openingBalance="1.00")
        self.assertEqual(400, self.client.post(f"/api/accounts/{self.externalId}/reconciliation", json={"actualBalance": "1.00"}).status_code)
        for accountClass, active in (("cash", True), ("system", False)):
            self.assertEqual(400, self.client.patch(f"/api/accounts/{self.externalId}", json={
                "name": "Different boundary", "accountClass": accountClass, "active": active, "minimumBalance": "1.00",
            }).status_code)
        transferId = createTransfer(self.connection, "2026-10-01", self.externalId, self.cashId, "5.00")
        saveSimulatorRow(self.connection, planId, None, "transfer", self.externalId, self.cashId, label="Projected boundary", amount="3.00")
        workspace = simulatorWorkspace(self.connection, planId)
        self.assertNotIn(self.externalId, [account["id"] for account in workspace["plan"]["results"]])
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        self.connection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        self.assertEqual([(self.externalId, "external", 0)], [tuple(row) for row in self.connection.execute(
            "SELECT id, systemIdentity, balanceOffsetCents FROM accounts WHERE accountClass = 'system'"
        )])
        self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM transactions WHERE transferId = ?", (transferId,)).fetchone()[0])

    def testUpgradeRefusesUserOwnedExternalWithoutChangingHistory(self):
        createTransaction(self.connection, self.cashId, "2026-10-01", "Existing activity", self.categoryId, "1.00")
        self.connection.execute("DELETE FROM accounts WHERE id = ?", (self.externalId,))
        self.connection.execute("INSERT INTO accounts (name, accountClass, balanceOffsetCents) VALUES ('External', 'cash', 12345)")
        useHistoricalSchema21(self.connection)
        self.connection.execute("PRAGMA user_version = 17")
        self.connection.commit()
        accountsBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM accounts ORDER BY id")]
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        self.connection.close()
        try:
            with self.assertRaisesRegex(RuntimeError, "Rename the existing user-owned account named External before upgrading"):
                initializeDatabase(self.databasePath)
        finally:
            self.connection = connectDatabase(self.databasePath)
        self.assertEqual(accountsBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM accounts ORDER BY id")])
        self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])
        self.assertEqual(17, self.connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])
