"""Integer-cent financial operations, including the External transfer boundary."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import initializeTestDatabase
from tests.test_support import useHistoricalSchema21
from tests.test_support import ownerClient
import sqlite3
import tempfile
import unittest
from pathlib import Path
from app import createApp
from database import deleteCategory, updateAccount, updateCategory, updateTransaction
from tests.test_support import copyProductionDatabase, createScenarioCategories
from database import ValidationError, createAccount, createCategory, createTransaction, createTransfer, updateTransfer, deleteTransaction, reconcileAccountBalance, createTemplate, createBulkTransactions, createTransactionWithReimbursement, createSimulatorPlan, saveSimulatorRow, simulatorWorkspace, dashboardReport, initializeDatabase
from tests.test_support import connectFixtureDatabase as connectDatabase

class MoneyOperationTests(unittest.TestCase):
    def setUp(self):
        initializeTestDatabase(self, "test.db")
        categoryIds = createScenarioCategories(self.connection)
        self.cashAccountId = createAccount(
            self.connection, "Checking", "cash", "checking", openingBalance="500.00"
        )
        self.creditAccountId = createAccount(
            self.connection, "Card", "credit", openingBalance="153.00"
        )
        self.wantsCategoryId = categoryIds["Wants"]
        self.incomeCategoryId = categoryIds["Income/Reimbursement"]
        self.passThroughCategoryId = categoryIds["Pass-Through"]

    def balance(self, accountId):
        return self.connection.execute(
            """
            SELECT a.balanceOffsetCents + COALESCE(SUM(t.amountCents), 0)
            FROM accounts AS a
            LEFT JOIN transactions AS t ON t.accountId = a.id
            WHERE a.id = ?
            GROUP BY a.id
            """,
            (accountId,),
        ).fetchone()[0]

    def testOpeningBalancesInitializeOffsetsWithoutTransactions(self):
        self.assertEqual(50000, self.balance(self.cashAccountId))
        self.assertEqual(-15300, self.balance(self.creditAccountId))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        accountColumns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(accounts)")
        }
        self.assertNotIn("currentBalance", accountColumns)
        self.assertIn("balanceOffsetCents", accountColumns)

    def testBothAccountClassesUseReportingRolesForAllEntrySignsThroughApi(self):
        expenseId = createCategory(self.connection, "Salary label", reportingRole="expense")
        incomeId = createCategory(self.connection, "Purchase label", reportingRole="income")
        client = ownerClient(createApp(self.databasePath))
        entries = [(expenseId, "12.34", -1234), (expenseId, "-2.34", 234),
                   (incomeId, "20.00", 2000), (incomeId, "-3.00", -300)]
        for accountId in (self.cashAccountId, self.creditAccountId):
            for categoryId, enteredAmount, storedCents in entries:
                with self.subTest(accountId=accountId, enteredAmount=enteredAmount, categoryId=categoryId):
                    payload = {"accountId": accountId, "date": "2026-10-01",
                               "description": "Role-defined entry", "categoryId": categoryId, "amount": enteredAmount}
                    response = client.post("/api/transactions", json=payload)
                    self.assertEqual(201, response.status_code)
                    transactionId = response.get_json()["id"]
                    self.assertEqual(storedCents, self.connection.execute(
                        "SELECT amountCents FROM transactions WHERE id = ?", (transactionId,)
                    ).fetchone()[0])
                    # Exercise edit conversion separately from creation with a different signed amount.
                    changedAmount = "-1.00" if enteredAmount.startswith("-") else "1.00"
                    self.assertEqual(200, client.patch(
                        f"/api/transactions/{transactionId}", json={**payload, "amount": changedAmount}
                    ).status_code)
                    changedCents = -100 if changedAmount.startswith("-") else 100
                    if categoryId == expenseId:
                        changedCents = -changedCents
                    self.assertEqual(changedCents, self.connection.execute(
                        "SELECT amountCents FROM transactions WHERE id = ?", (transactionId,)
                    ).fetchone()[0])
                    self.assertEqual(200, client.patch(f"/api/transactions/{transactionId}", json=payload).status_code)
        accounts = {account["id"]: account for account in client.get("/api/accounts").get_json()}
        self.assertEqual((50700, -14600), tuple(accounts[accountId]["balanceCents"]
                         for accountId in (self.cashAccountId, self.creditAccountId)))
        report = dashboardReport(self.connection, "custom", "2026-10-01", "2026-10-01")
        self.assertEqual((2000, 3400, 1400), tuple(report[key] for key in ("expensesCents", "incomeCents", "netCents")))

    def testSyntheticLegacyRowsSurviveReconciliationRestartAndUpgrade(self):
        self.connection.executemany(
            "INSERT INTO transactions (accountId, date, description, amountCents) VALUES (?, ?, ?, ?)",
            [(self.cashAccountId, "2020-01-01", "Opening Balance", 12345),
             (self.cashAccountId, "2020-01-02", "Balance Adjustment", -45),
             (self.creditAccountId, "2020-01-01", "Opening Balance", -20000),
             (self.creditAccountId, "2020-01-02", "Balance Adjustment", 50)],
        )
        self.connection.commit()
        createTransaction(self.connection, self.cashAccountId, "2020-01-03", "Recorded cost", self.wantsCategoryId, "10.00")
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        self.assertEqual((61300, -35250), (self.balance(self.cashAccountId), self.balance(self.creditAccountId)))
        reportBefore = dashboardReport(self.connection, "custom", "2020-01-01", "2020-01-31")
        self.assertEqual((1000, 0, -1000), tuple(reportBefore[key] for key in ("expensesCents", "incomeCents", "netCents")))
        reconcileAccountBalance(self.connection, self.cashAccountId, "300.00")
        reconcileAccountBalance(self.connection, self.creditAccountId, "180.00")
        offsetsBefore = [tuple(row) for row in self.connection.execute("SELECT id, balanceOffsetCents FROM accounts WHERE accountClass != 'system' ORDER BY id")]
        self.connection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])
        # Synthetic pre-External configuration exercises the supported upgrade with a temporary sibling backup.
        self.connection.execute("DELETE FROM accounts WHERE systemIdentity = 'external'")
        useHistoricalSchema21(self.connection)
        self.connection.execute("PRAGMA user_version = 17")
        self.connection.commit()
        self.connection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        self.assertEqual([row[1:] for row in ledgerBefore], [tuple(row)[1:] for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])
        self.assertEqual(offsetsBefore, [tuple(row) for row in self.connection.execute("SELECT id, balanceOffsetCents FROM accounts WHERE accountClass != 'system' ORDER BY id")])
        self.assertEqual((30000, -18000), (self.balance(self.cashAccountId), self.balance(self.creditAccountId)))
        self.assertEqual(reportBefore, dashboardReport(self.connection, "custom", "2020-01-01", "2020-01-31"))
        backupPaths = list((self.databasePath.parent / "backups").glob("pre-migration-v17-to-v31-*.db"))
        self.assertEqual(1, len(backupPaths))
        backupConnection = sqlite3.connect(backupPaths[0])
        try:
            self.assertEqual([row[1:] for row in ledgerBefore], backupConnection.execute("SELECT * FROM transactions ORDER BY id").fetchall())
            self.assertEqual(17, backupConnection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual("ok", backupConnection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            backupConnection.close()
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def testExpenseIncomeAndRefundSignConversion(self):
        createTransaction(self.connection, self.cashAccountId, "2026-01-02", "Meal", self.wantsCategoryId, "11.73")
        createTransaction(self.connection, self.cashAccountId, "2026-01-03", "Paycheck", self.incomeCategoryId, "1519.66")
        createTransaction(self.connection, self.cashAccountId, "2026-01-04", "Refund", self.wantsCategoryId, "-17.27")
        rows = self.connection.execute(
            "SELECT description, amountCents FROM transactions WHERE categoryId IS NOT NULL ORDER BY id"
        ).fetchall()
        self.assertEqual([("Meal", -1173), ("Paycheck", 151966), ("Refund", 1727)], [tuple(row) for row in rows])

    def testPassThroughReimbursementCreatesTwoOrdinaryTransactionsAtomically(self):
        expenseId, reimbursementId = createTransactionWithReimbursement(
            self.connection, self.cashAccountId, "2026-08-20", "Shared dinner",
            self.passThroughCategoryId, "24.50", True, self.creditAccountId,
            "2026-08-22", "Friend reimbursement", "20.00",
        )
        rows = self.connection.execute(
            """SELECT t.id, t.accountId, t.amountCents, t.transferId, t.pinned, c.name
            FROM transactions AS t JOIN categories AS c ON c.id = t.categoryId
            WHERE t.id IN (?, ?) ORDER BY t.id""",
            (expenseId, reimbursementId),
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual(
            [(self.cashAccountId, -2450, None, 1, "Pass-Through"),
             (self.creditAccountId, 2000, None, 0, "Income/Reimbursement")],
            [(row["accountId"], row["amountCents"], row["transferId"], row["pinned"], row["name"]) for row in rows],
        )

    def testPassThroughReimbursementFailureCannotLeavePartialPair(self):
        with self.assertRaises(ValidationError):
            createTransactionWithReimbursement(
                self.connection, self.cashAccountId, "2026-08-20", "Shared dinner",
                self.passThroughCategoryId, "24.50", False, self.cashAccountId,
                "2026-08-20", "", "24.50",
            )
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.connection.execute(
            """CREATE TRIGGER failReimbursement BEFORE INSERT ON transactions
            WHEN NEW.description = 'Forced reimbursement failure'
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END"""
        )
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            createTransactionWithReimbursement(
                self.connection, self.cashAccountId, "2026-08-20", "Shared dinner",
                self.passThroughCategoryId, "24.50", False, self.cashAccountId,
                "2026-08-20", "Forced reimbursement failure", "24.50",
            )
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testBulkEntryIgnoresBlankRowsAndUsesNormalSignsAtomically(self):
        created = createBulkTransactions(self.connection, [
            {"date": "2026-08-20", "accountId": "", "description": "", "categoryId": "", "amount": ""},
            {"date": "", "accountId": "", "description": "", "categoryId": "", "amount": ""},
            {"date": "2026-08-20", "accountId": self.cashAccountId, "description": "Expense", "categoryId": self.wantsCategoryId, "amount": "10.00"},
            {"date": "2026-08-21", "accountId": self.creditAccountId, "description": "Income", "categoryId": self.incomeCategoryId, "amount": "20.00"},
            {"date": "2026-08-22", "accountId": self.cashAccountId, "description": "Refund", "categoryId": self.wantsCategoryId, "amount": "-3.00"},
            {"date": "2026-08-23", "accountId": self.cashAccountId, "description": "Pass", "categoryId": self.passThroughCategoryId, "amount": "4.00"},
        ])
        self.assertEqual(4, created)
        rows = self.connection.execute("SELECT description, amountCents, transferId FROM transactions ORDER BY id").fetchall()
        self.assertEqual([("Expense", -1000, None), ("Income", 2000, None), ("Refund", 300, None), ("Pass", -400, None)], [tuple(row) for row in rows])

    def testBulkEntryReportsRowsAndWritesNothingOnValidationOrDatabaseFailure(self):
        with self.assertRaisesRegex(ValidationError, "Row 2: Description is required"):
            createBulkTransactions(self.connection, [
                {"date": "2026-08-20", "accountId": self.cashAccountId, "description": "Valid", "categoryId": self.wantsCategoryId, "amount": "1.00"},
                {"date": "2026-08-20", "accountId": self.cashAccountId, "description": "", "categoryId": self.wantsCategoryId, "amount": "2.00"},
            ])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.connection.execute("""CREATE TRIGGER failBulk BEFORE INSERT ON transactions WHEN NEW.description = 'Fail' BEGIN SELECT RAISE(ABORT, 'forced'); END""")
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            createBulkTransactions(self.connection, [
                {"date": "2026-08-20", "accountId": self.cashAccountId, "description": "First", "categoryId": self.wantsCategoryId, "amount": "1.00"},
                {"date": "2026-08-20", "accountId": self.cashAccountId, "description": "Fail", "categoryId": self.wantsCategoryId, "amount": "2.00"},
            ])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testBulkEntryRejectsTransfersAndArchivedObjects(self):
        with self.assertRaisesRegex(ValidationError, "transfers are not supported"):
            createBulkTransactions(self.connection, [{"date": "2026-08-20", "fromAccountId": 1, "toAccountId": 2, "amount": "1.00"}])
        updateAccount(self.connection, self.cashAccountId, "Checking", "cash", "checking", 0, False)
        with self.assertRaisesRegex(ValidationError, "Row 1: Account is archived"):
            createBulkTransactions(self.connection, [{"date": "2026-08-20", "accountId": self.cashAccountId, "description": "No", "categoryId": self.wantsCategoryId, "amount": "1.00"}])

    def testPinningChangesNoAccountingAndCanBeRemovedByNormalEdit(self):
        transactionId = createTransaction(
            self.connection, self.cashAccountId, "2026-09-10", "Future pinned expense",
            self.wantsCategoryId, "10.00", True,
        )
        balanceBefore = self.balance(self.cashAccountId)
        row = self.connection.execute(
            "SELECT amountCents, pinned FROM transactions WHERE id = ?", (transactionId,)
        ).fetchone()
        self.assertEqual((-1000, 1), tuple(row))
        dashboard = ownerClient(createApp(self.databasePath)).get("/api/dashboard").get_json()
        self.assertEqual([transactionId], [row["id"] for row in dashboard["quickUpdates"]])
        self.assertNotIn(transactionId, [row["id"] for row in dashboard["recentTransactions"]])
        updateTransaction(
            self.connection, transactionId, self.cashAccountId, "2026-09-10",
            "Future pinned expense", self.wantsCategoryId, "10.00", False,
        )
        self.assertEqual(balanceBefore, self.balance(self.cashAccountId))
        self.assertEqual(0, self.connection.execute(
            "SELECT pinned FROM transactions WHERE id = ?", (transactionId,)
        ).fetchone()[0])
        dashboard = ownerClient(createApp(self.databasePath)).get("/api/dashboard").get_json()
        self.assertEqual([], dashboard["quickUpdates"])
        self.assertIn(transactionId, [row["id"] for row in dashboard["recentTransactions"]])

    def testPersonalLoanUsesCreditMathAndTransfers(self):
        loanId = createAccount(self.connection, "Personal Loan", "credit", "Personal Loan", openingBalance="1000.00")
        self.assertEqual(-100000, self.balance(loanId))
        transferId = createTransfer(
            self.connection, "2026-08-20", self.cashAccountId, loanId, "100.00", "Personal Loan Payment"
        )
        rows = self.connection.execute(
            "SELECT amountCents FROM transactions WHERE transferId = ?", (transferId,)
        ).fetchall()
        self.assertEqual(0, sum(row["amountCents"] for row in rows))
        self.assertEqual(-90000, self.balance(loanId))

    def testMalformedTransferPairsStayVisibleAndCannotBeEditedOrSent(self):
        client = ownerClient(createApp(self.databasePath))
        planId = createSimulatorPlan(self.connection, "Malformed transfer checks", "2024-02-29")
        mutations = [
            "DELETE FROM transactions WHERE id = ?",
            "UPDATE transactions SET amountCents = 0 WHERE transferId = ?",
            "UPDATE transactions SET amountCents = -999 WHERE id = ?",
            "UPDATE transactions SET accountId = ? WHERE transferId = ?",
            "UPDATE transactions SET date = '2024-03-01' WHERE id = ?",
            "UPDATE transactions SET description = 'Different description' WHERE id = ?",
            "UPDATE transactions SET categoryId = ? WHERE id = ?",
            "INSERT INTO transactions (accountId,date,description,amountCents,transferId) VALUES (?, '2024-02-29', 'Extra row', 100, ?)",
        ]
        for index, statement in enumerate(mutations):
            with self.subTest(mutation=index):
                transferId = createTransfer(self.connection, "2024-02-29", self.cashAccountId, self.creditAccountId, "10.00", "Historical pair")
                sourceId = self.connection.execute("SELECT id FROM transactions WHERE transferId = ? AND amountCents < 0", (transferId,)).fetchone()[0]
                arguments = {
                    0: (sourceId,), 1: (transferId,), 2: (sourceId,),
                    3: (self.cashAccountId, transferId), 4: (sourceId,), 5: (sourceId,),
                    6: (self.wantsCategoryId, sourceId), 7: (self.cashAccountId, transferId),
                }[index]
                with self.connection:
                    self.connection.execute(statement, arguments)
                before = list(self.connection.iterdump())
                history = client.get("/api/transactions?period=all&type=transfers").get_json()
                events = [event for event in history if event["transferId"] == transferId]
                storedRows = self.connection.execute("SELECT * FROM transactions WHERE transferId = ?", (transferId,)).fetchall()
                self.assertEqual(len(storedRows), len(events))
                self.assertTrue(all(not event["combinedTransfer"] and not event["simulatorEligible"] for event in events))
                self.assertTrue(all(event["displayAmountCents"] == event["amountCents"] for event in events))
                responses = [
                    client.get(f"/api/transfers/{transferId}"),
                    client.patch(f"/api/transfers/{transferId}", json={"date": "2024-02-29", "fromAccountId": self.cashAccountId, "toAccountId": self.creditAccountId, "amount": "10.00", "description": "Changed"}),
                    client.get("/api/simulator/send-preview", query_string={"transferId": transferId}),
                    client.post("/api/simulator/send", json={"planId": planId, "transferId": transferId}),
                    client.get(f"/api/transfers/{transferId}/pending-snapshot"),
                ]
                self.assertEqual([400] * len(responses), [response.status_code for response in responses])
                self.assertEqual(before, list(self.connection.iterdump()), "Malformed history must never be repaired or partly rewritten by these operations")

    def testCreditCardPaymentIsAZeroSumTransfer(self):
        transferId = createTransfer(
            self.connection, "2026-02-01", self.cashAccountId, self.creditAccountId, "100.00", "Card Payment"
        )
        rows = self.connection.execute(
            "SELECT accountId, amountCents, categoryId FROM transactions WHERE transferId = ? ORDER BY amountCents",
            (transferId,),
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual(0, sum(row["amountCents"] for row in rows))
        self.assertEqual([None, None], [row["categoryId"] for row in rows])
        self.assertEqual(-10000, rows[0]["amountCents"])
        self.assertEqual(10000, rows[1]["amountCents"])

    def testTransferCreationRollsBackIfEitherSideFails(self):
        self.connection.execute(
            """
            CREATE TRIGGER failPositiveTransfer BEFORE INSERT ON transactions
            WHEN NEW.transferId IS NOT NULL AND NEW.amountCents > 0
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END
            """
        )
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            createTransfer(self.connection, "2026-02-01", self.cashAccountId, self.creditAccountId, "10.00")
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions WHERE transferId IS NOT NULL").fetchone()[0])

    def testTransferEditIsAtomicAndDeleteRemovesBothRows(self):
        transferId = createTransfer(self.connection, "2026-02-01", self.cashAccountId, self.creditAccountId, "10.00")
        updateTransfer(self.connection, transferId, "2026-02-02", self.cashAccountId, self.creditAccountId, "25.00", "Payment")
        rows = self.connection.execute(
            "SELECT * FROM transactions WHERE transferId = ?", (transferId,)
        ).fetchall()
        self.assertEqual(0, sum(row["amountCents"] for row in rows))
        self.assertEqual({-2500, 2500}, {row["amountCents"] for row in rows})
        deleteTransaction(self.connection, rows[0]["id"])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions WHERE transferId = ?", (transferId,)).fetchone()[0])

    def testFailedTransferEditPreservesOriginalPair(self):
        transferId = createTransfer(self.connection, "2026-02-01", self.cashAccountId, self.creditAccountId, "10.00")
        originalRows = self.connection.execute(
            "SELECT accountId, date, description, amountCents FROM transactions WHERE transferId = ? ORDER BY amountCents",
            (transferId,),
        ).fetchall()
        self.connection.execute(
            """
            CREATE TRIGGER failEditedTransfer BEFORE INSERT ON transactions
            WHEN NEW.transferId IS NOT NULL AND NEW.description = 'Fail'
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END
            """
        )
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            updateTransfer(self.connection, transferId, "2026-02-03", self.cashAccountId, self.creditAccountId, "20.00", "Fail")
        currentRows = self.connection.execute(
            "SELECT accountId, date, description, amountCents FROM transactions WHERE transferId = ? ORDER BY amountCents",
            (transferId,),
        ).fetchall()
        self.assertEqual([tuple(row) for row in originalRows], [tuple(row) for row in currentRows])

    def testLateTransferEditAndDeleteFailuresPreserveLedgerBalancesAndExecutionLink(self):
        transferId = createTransfer(self.connection, "2026-10-01", self.cashAccountId, self.creditAccountId, "10.00")
        sourceId = self.connection.execute("SELECT id FROM transactions WHERE transferId = ? AND amountCents < 0", (transferId,)).fetchone()[0]
        planId = createSimulatorPlan(self.connection, "Rollback fixture", "2026-10-01")
        rowId = saveSimulatorRow(self.connection, planId, None, "transfer", self.cashAccountId, self.creditAccountId, label="Planned payment", amount="10.00")
        self.connection.execute("UPDATE simulatorRows SET executed = 1, executedTransactionId = ? WHERE id = ?", (sourceId, rowId))
        self.connection.execute("""CREATE TRIGGER failSecondTransferInsert BEFORE INSERT ON transactions
            WHEN NEW.transferId IS NOT NULL AND NEW.description = 'Late failure' AND NEW.amountCents > 0
            BEGIN SELECT RAISE(ABORT, 'second insert failed'); END""")
        self.connection.commit()
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        simulatorBefore = tuple(self.connection.execute("SELECT * FROM simulatorRows WHERE id = ?", (rowId,)).fetchone())
        balancesBefore = (self.balance(self.cashAccountId), self.balance(self.creditAccountId))
        for operation in ("edit", "delete"):
            with self.subTest(operation=operation):
                if operation == "delete":
                    self.connection.execute("""CREATE TRIGGER failSecondTransferDelete BEFORE DELETE ON transactions
                        WHEN OLD.transferId IS NOT NULL AND OLD.amountCents > 0
                          AND NOT EXISTS (SELECT 1 FROM transactions WHERE transferId = OLD.transferId AND amountCents < 0)
                        BEGIN SELECT RAISE(ABORT, 'second delete failed'); END""")
                    self.connection.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "second .* failed"):
                    if operation == "edit":
                        updateTransfer(self.connection, transferId, "2026-10-02", self.creditAccountId, self.cashAccountId, "20.00", "Late failure")
                    else:
                        deleteTransaction(self.connection, sourceId)
                self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])
                self.assertEqual(simulatorBefore, tuple(self.connection.execute("SELECT * FROM simulatorRows WHERE id = ?", (rowId,)).fetchone()))
                self.assertEqual(balancesBefore, (self.balance(self.cashAccountId), self.balance(self.creditAccountId)))
                self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def testTransferAccountChangesAndDestinationSideDeletionRestoreBalances(self):
        newCashId = createAccount(self.connection, "Different purse", "cash", openingBalance="200.00")
        newCreditId = createAccount(self.connection, "Different liability", "credit", "Other", openingBalance="20.00")
        transferId = createTransfer(self.connection, "2026-10-01", self.cashAccountId, self.creditAccountId, "10.00")
        updateTransfer(self.connection, transferId, "2026-10-02", newCashId, newCreditId, "25.00", "Changed endpoints")
        rows = self.connection.execute("SELECT * FROM transactions WHERE transferId = ? ORDER BY amountCents", (transferId,)).fetchall()
        self.assertEqual([(newCashId, -2500), (newCreditId, 2500)], [(row["accountId"], row["amountCents"]) for row in rows])
        self.assertTrue(all(row["categoryId"] is None and row["date"] == "2026-10-02" and row["description"] == "Changed endpoints" for row in rows))
        self.assertEqual((50000, -15300, 17500, 500), tuple(self.balance(accountId) for accountId in (self.cashAccountId, self.creditAccountId, newCashId, newCreditId)))
        deleteTransaction(self.connection, rows[1]["id"])
        self.assertEqual([], self.connection.execute("SELECT * FROM transactions WHERE transferId = ?", (transferId,)).fetchall())
        self.assertEqual((20000, -2000), (self.balance(newCashId), self.balance(newCreditId)))

    def testInvalidTransferCreationAndEditsLeaveOriginalLedgerAndBalancesUnchanged(self):
        transferId = createTransfer(self.connection, "2026-10-01", self.cashAccountId, self.creditAccountId, "10.00")
        archivedId = createAccount(self.connection, "Unavailable purse", "cash")
        updateAccount(self.connection, archivedId, "Unavailable purse", "cash", active=False)
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        balancesBefore = (self.balance(self.cashAccountId), self.balance(self.creditAccountId))
        invalidTransfers = [(self.cashAccountId, self.cashAccountId, "1.00", "2026-10-01"),
                            (self.cashAccountId, self.creditAccountId, "0", "2026-10-01"),
                            (self.cashAccountId, self.creditAccountId, "-1.00", "2026-10-01"),
                            (self.cashAccountId, self.creditAccountId, "1.001", "2026-10-01"),
                            (self.cashAccountId, self.creditAccountId, "1.00", "2026-02-30"),
                            (self.cashAccountId, archivedId, "1.00", "2026-10-01"),
                            (self.cashAccountId, 999999, "1.00", "2026-10-01")]
        for fromId, toId, amount, transactionDate in invalidTransfers:
            for operation in ("create", "edit"):
                with self.subTest(operation=operation, toId=toId, amount=amount, date=transactionDate):
                    with self.assertRaises(ValidationError):
                        if operation == "create":
                            createTransfer(self.connection, transactionDate, fromId, toId, amount)
                        else:
                            updateTransfer(self.connection, transferId, transactionDate, fromId, toId, amount)
                    self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])
                    self.assertEqual(balancesBefore, (self.balance(self.cashAccountId), self.balance(self.creditAccountId)))

    def testOrdinaryTransactionCanBeEditedAndDeleted(self):
        transactionId = createTransaction(
            self.connection, self.cashAccountId, "2026-01-02", "Meal", self.wantsCategoryId, "10.00"
        )
        updateTransaction(
            self.connection, transactionId, self.cashAccountId, "2026-01-03", "Refund", self.wantsCategoryId, "-4.00"
        )
        row = self.connection.execute("SELECT * FROM transactions WHERE id = ?", (transactionId,)).fetchone()
        self.assertEqual(("2026-01-03", "Refund", 400), (row["date"], row["description"], row["amountCents"]))
        deleteTransaction(self.connection, transactionId)
        self.assertIsNone(self.connection.execute("SELECT id FROM transactions WHERE id = ?", (transactionId,)).fetchone())

    def testUncategorizedHistoricalTransactionCanRemainUncategorizedWhenEdited(self):
        cursor = self.connection.execute(
            """INSERT INTO transactions (accountId, date, description, amountCents)
            VALUES (?, ?, ?, ?)""",
            (self.cashAccountId, "2026-01-02", "Imported adjustment", -2437),
        )
        self.connection.commit()
        updateTransaction(
            self.connection, cursor.lastrowid, self.cashAccountId, "2026-01-03",
            "Imported adjustment corrected", None, "-24.38",
        )
        row = self.connection.execute("SELECT * FROM transactions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        self.assertEqual((-2438, None), (row["amountCents"], row["categoryId"]))

    def testBalanceReconciliationUpdatesOffsetWithoutCreatingTransaction(self):
        createTransaction(self.connection, self.cashAccountId, "2026-02-01", "Meal", self.wantsCategoryId, "10.00")
        transactionCount = self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        cashOffset, cashDifference = reconcileAccountBalance(
            self.connection, self.cashAccountId, "600.00"
        )
        self.assertEqual((61000, 11000), (cashOffset, cashDifference))
        self.assertEqual(60000, self.balance(self.cashAccountId))
        creditOffset, creditDifference = reconcileAccountBalance(
            self.connection, self.creditAccountId, "100.00"
        )
        self.assertEqual((-10000, 5300), (creditOffset, creditDifference))
        self.assertEqual(-10000, self.balance(self.creditAccountId))
        self.assertEqual(
            transactionCount,
            self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        )

    def testArchivedEntitiesPreserveHistoryAndCannotReceiveNewTransactions(self):
        transactionId = createTransaction(
            self.connection, self.cashAccountId, "2026-01-02", "Meal", self.wantsCategoryId, "10.00"
        )
        updateAccount(self.connection, self.cashAccountId, "Checking", "cash", "checking", 0, False)
        wants = self.connection.execute("SELECT * FROM categories WHERE id = ?", (self.wantsCategoryId,)).fetchone()
        updateCategory(self.connection, self.wantsCategoryId, wants["name"], None, wants["displayOrder"], False)
        self.assertIsNotNone(self.connection.execute("SELECT id FROM transactions WHERE id = ?", (transactionId,)).fetchone())
        updateTransaction(
            self.connection, transactionId, self.cashAccountId, "2026-01-04",
            "Historical edit", self.wantsCategoryId, "2.00",
        )
        historicalRow = self.connection.execute(
            "SELECT date, description, amountCents FROM transactions WHERE id = ?", (transactionId,)
        ).fetchone()
        self.assertEqual(("2026-01-04", "Historical edit", -200), tuple(historicalRow))
        with self.assertRaises(ValidationError):
            createTransaction(self.connection, self.cashAccountId, "2026-01-03", "No", self.incomeCategoryId, "1.00")

    def testCategoryHierarchyIsLimitedToOneLevel(self):
        parentId = createCategory(self.connection, "Parent", None, 100)
        childId = createCategory(self.connection, "Child", parentId, 101)
        with self.assertRaises(ValidationError):
            createCategory(self.connection, "Grandchild", childId, 102)
        with self.assertRaises(ValidationError):
            createTransaction(self.connection, self.cashAccountId, "2026-01-02", "No rollup", parentId, "1.00")

    def testConfigurableReimbursementAndSafeCategoryDeletion(self):
        customId = createCategory(self.connection, "Shared Expense", None, 90, True)
        expenseId, reimbursementId = createTransactionWithReimbursement(
            self.connection, self.cashAccountId, "2026-08-22", "Shared", customId, "10.00", False,
            self.cashAccountId, "2026-08-22", "Paid back", "10.00",
        )
        self.assertNotEqual(expenseId, reimbursementId)
        with self.assertRaisesRegex(ValidationError, "transactions reference"):
            deleteCategory(self.connection, customId)
        unusedId = createCategory(self.connection, "Unused", None, 91)
        templateId = createTemplate(self.connection, "Uses unused", None, None, unusedId, None)
        deleteCategory(self.connection, unusedId)
        self.assertIsNone(self.connection.execute("SELECT categoryId FROM transactionTemplates WHERE id = ?", (templateId,)).fetchone()[0])
        with self.assertRaisesRegex(ValidationError, "another Reimbursement Category"):
            deleteCategory(self.connection, self.incomeCategoryId)

    def testAccountValidationErrorsPreserveStateAndExistingPrecedence(self):
        def snapshot():
            return tuple(tuple(tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY id"))
                         for table in ("accounts", "transactions", "auditEvents"))

        invalidCases = (
            ({"name": None}, "Account name is required"),
            ({"name": " \t "}, "Account name is required"),
            ({"name": "  eXtErNaL  "}, "External is reserved for the built-in System Account"),
            ({"accountClass": "system"}, "Account class must be cash or credit"),
            ({"accountClass": "Cash"}, "Account class must be cash or credit"),
            ({"accountClass": None}, "Account class must be cash or credit"),
            ({"accountSubtype": "Credit Card"}, "Cash account subtype is invalid"),
            ({"accountClass": "credit", "accountSubtype": "checking"}, "Credit account subtype is invalid"),
            ({"accountClass": "credit", "accountSubtype": "credit card"}, "Credit account subtype is invalid"),
            ({"name": "Checking"}, "Account name must be unique"),
            ({"name": "", "accountClass": "invalid", "pinnedForSelection": "true"}, "Pin in account selectors must be Boolean"),
            ({"name": "", "accountClass": "invalid"}, "Account name is required"),
            ({"name": "External", "accountClass": "invalid"}, "External is reserved for the built-in System Account"),
            ({"accountClass": "invalid", "accountSubtype": "invalid"}, "Account class must be cash or credit"),
            ({"name": "", "displayOrder": "invalid"}, "Account name is required"),
        )
        before = snapshot()
        for operation in ("create", "edit"):
            for overrides, message in invalidCases:
                fields = {"name": "Validation candidate", "accountClass": "cash", **overrides}
                with self.subTest(operation=operation, fields=fields):
                    with self.assertRaises(ValidationError) as raised:
                        if operation == "create":
                            createAccount(self.connection, **fields)
                        else:
                            updateAccount(self.connection, self.creditAccountId, **fields)
                    self.assertEqual(message, str(raised.exception))
                    self.assertEqual(before, snapshot())
        externalId = self.connection.execute("SELECT id FROM accounts WHERE systemIdentity = 'external'").fetchone()[0]
        with self.assertRaises(ValidationError) as raised:
            updateAccount(self.connection, externalId, "", "invalid", pinnedForSelection="true")
        self.assertEqual("System Accounts are managed by No Part Finance and cannot be modified", str(raised.exception))
        self.assertEqual(before, snapshot())
        updateAccount(self.connection, self.cashAccountId, "Checking", "cash", active=False)
        archivedSnapshot = snapshot()
        with self.assertRaises(ValidationError) as raised:
            updateAccount(self.connection, self.cashAccountId, "", "invalid", active=False, pinnedForSelection=True)
        self.assertEqual("Archived Accounts cannot be pinned", str(raised.exception))
        self.assertEqual(archivedSnapshot, snapshot())

    def testAccountFieldNormalizationPreservesOpeningOffsetsAndExistingHistory(self):
        createTransaction(self.connection, self.cashAccountId, "2028-02-29", "Preserved purchase", self.wantsCategoryId, "1.00")
        historyBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        validCases = (
            ("cash", None), ("cash", "checking"), ("cash", "savings"), ("cash", "other"),
            ("credit", None), ("credit", "Credit Card"), ("credit", "Personal Loan"), ("credit", "Other"),
            ("cash", ""), ("credit", False), ("cash", 0),
        )
        for index, (accountClass, subtype) in enumerate(validCases):
            with self.subTest(accountClass=accountClass, subtype=subtype):
                createdId = createAccount(self.connection, f"  Created {index}  ", accountClass, subtype, openingBalance="12.34")
                created = self.connection.execute("SELECT * FROM accounts WHERE id = ?", (createdId,)).fetchone()
                self.assertEqual((f"Created {index}", accountClass, subtype or None), (created["name"], created["accountClass"], created["accountSubtype"]))
                self.assertEqual(-1234 if accountClass == "credit" else 1234, created["balanceOffsetCents"])
                updateAccount(self.connection, self.cashAccountId, f"  Edited {index}  ", accountClass, subtype)
                edited = self.connection.execute("SELECT * FROM accounts WHERE id = ?", (self.cashAccountId,)).fetchone()
                self.assertEqual((f"Edited {index}", accountClass, subtype or None), (edited["name"], edited["accountClass"], edited["accountSubtype"]))
                self.assertEqual(50000, edited["balanceOffsetCents"])
                self.assertEqual(49900, self.balance(self.cashAccountId))
                self.assertEqual(historyBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])

    def testCreditLimitValidationAndSubtypeChangeClearsIt(self):
        cardId = createAccount(self.connection, "Limited Card", "credit", "Credit Card", creditLimit="1000.00")
        self.assertEqual(100000, self.connection.execute("SELECT creditLimitCents FROM accounts WHERE id = ?", (cardId,)).fetchone()[0])
        updateAccount(self.connection, cardId, "Limited Card", "credit", "Personal Loan", 0, True, "1000.00")
        self.assertIsNone(self.connection.execute("SELECT creditLimitCents FROM accounts WHERE id = ?", (cardId,)).fetchone()[0])
        with self.assertRaisesRegex(ValidationError, "greater than zero"):
            createAccount(self.connection, "Bad Limit", "credit", "Credit Card", creditLimit="0")

class ImportedDatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporaryDirectory.cleanup)
        self.databasePath = Path(self.temporaryDirectory.name) / "imported-copy.db"
        copyProductionDatabase(self, self.databasePath)
        self.app = createApp(self.databasePath)
        self.client = ownerClient(self.app, copiedDatabase=True)

    def testImportedHistoryLoadsAndFiltersWithoutMutation(self):
        allTransactions = self.client.get("/api/transactions?period=all")
        self.assertEqual(200, allTransactions.status_code)
        connection = connectDatabase(self.databasePath)
        try:
            expectedCount = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            ledgerBefore = [tuple(row) for row in connection.execute("SELECT * FROM transactions ORDER BY id")]
            history = connection.execute("SELECT accountId, description FROM transactions ORDER BY id LIMIT 1").fetchone()
        finally:
            connection.close()
        self.assertLessEqual(len(allTransactions.get_json()), expectedCount)
        accountResponse = self.client.get("/api/accounts")
        self.assertEqual(200, accountResponse.status_code)
        if history is None:
            self.skipTest("History filtering requires at least one ledger row")
        filtered = self.client.get("/api/transactions", query_string={
            "period": "all", "accountId": history["accountId"], "description": history["description"],
        })
        self.assertEqual(200, filtered.status_code)
        self.assertTrue(filtered.get_json())
        self.assertTrue(all(history["description"].lower() in row["description"].lower() for row in filtered.get_json()))
        self.assertTrue(all(history["accountId"] in {row["accountId"], row.get("toAccountId")} for row in filtered.get_json()))
        connection = connectDatabase(self.databasePath)
        try:
            self.assertEqual(ledgerBefore, [tuple(row) for row in connection.execute("SELECT * FROM transactions ORDER BY id")])
        finally:
            connection.close()

    def testApiAddsRefundAndPreservesImportedDatabaseOriginal(self):
        accounts = self.client.get("/api/accounts").get_json()
        categories = self.client.get("/api/categories").get_json()
        accountId = next((account["id"] for account in accounts if account["active"] and account["accountClass"] in {"cash", "credit"}), None)
        categoryId = next((category["id"] for category in categories if category["active"] and category["reportingRole"] == "expense" and not category["hasChildren"]), None)
        if accountId is None or categoryId is None:
            self.skipTest("Refund creation requires an active user-owned Account and selectable Expense Category")
        response = self.client.post("/api/transactions", json={
            "date": "2026-08-19", "accountId": accountId, "description": "Integration Refund",
            "categoryId": categoryId, "amount": "-5.00",
        })
        self.assertEqual(201, response.status_code)
        connection = connectDatabase(self.databasePath)
        try:
            amountCents = connection.execute(
                "SELECT amountCents FROM transactions WHERE id = ?", (response.get_json()["id"],)
            ).fetchone()[0]
            self.assertEqual(500, amountCents)
        finally:
            connection.close()

    def testImportedHistoryIsPreservedWhenReconciliationUsesOffset(self):
        accounts = self.client.get("/api/accounts").get_json()
        target = next((account for account in accounts if account["accountClass"] in {"cash", "credit"}), None)
        if target is None:
            self.skipTest("Reconciliation requires a user-owned Account")
        connection = connectDatabase(self.databasePath)
        try:
            ledgerBefore = [tuple(row) for row in connection.execute("SELECT * FROM transactions ORDER BY id")]
            transactionBalance = connection.execute("SELECT COALESCE(SUM(amountCents), 0) FROM transactions WHERE accountId = ?", (target["id"],)).fetchone()[0]
        finally:
            connection.close()
        response = self.client.post(
            f"/api/accounts/{target['id']}/reconciliation",
            json={"actualBalance": "100.00"},
        )
        self.assertEqual(200, response.status_code)
        connection = connectDatabase(self.databasePath)
        try:
            self.assertEqual(ledgerBefore, [tuple(row) for row in connection.execute("SELECT * FROM transactions ORDER BY id")])
            expectedBalance = -10000 if target["accountClass"] == "credit" else 10000
            self.assertEqual(
                expectedBalance - transactionBalance,
                connection.execute(
                    "SELECT balanceOffsetCents FROM accounts WHERE id = ?", (target["id"],)
                ).fetchone()[0],
            )
        finally:
            connection.close()

class IndependentConfigurationTests(unittest.TestCase):
    def setUp(self):
        initializeTestDatabase(self, "independent.db")
        self.connection.executemany(
            "INSERT INTO accounts (id, name, accountClass, balanceOffsetCents) VALUES (?, ?, ?, ?)",
            [(101, "財布", "cash", 10000), (307, "Different liability", "credit", -2000)],
        )
        self.connection.executemany(
            "INSERT INTO categories (id, name, reportingRole, parentCategoryId, offerReimbursement) VALUES (?, ?, ?, ?, ?)",
            [(501, "Daily costs", "expense", None, 0), (509, "Income/Reimbursement", "expense", 501, 1),
             (601, "Second rollup", "expense", None, 0), (607, "Salary", "expense", 601, 0),
             (701, "Needs", "income", None, 0), (709, "Another income", "income", None, 0)],
        )
        self.connection.execute("INSERT INTO applicationSettings (key, value) VALUES ('reimbursementCategoryId', '709')")
        self.connection.commit()

    def ledgerSnapshot(self):
        return [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]

    def testRenamesAndArchivesPreserveFinancialMeaningAndStableReferencesThroughRestart(self):
        for accountId, categoryId, amount in [(101, 509, "50.00"), (101, 509, "-20.00"),
                                               (307, 607, "10.00"), (101, 701, "100.00"),
                                               (101, 701, "-10.00"), (307, 709, "5.00")]:
            createTransaction(self.connection, accountId, "2026-10-01", "Independent entry", categoryId, amount)
        transferId = createTransfer(self.connection, "2026-10-01", 101, 307, "25.00")
        templateId = createTemplate(self.connection, "Independent preset", 101, "Preset", 509, "12.00")
        ledgerBefore = self.ledgerSnapshot()
        templateBefore = tuple(self.connection.execute("SELECT * FROM transactionTemplates WHERE id = ?", (templateId,)).fetchone())

        def assertFinancialResults():
            report = dashboardReport(self.connection, "custom", "2026-10-01", "2026-10-01")
            self.assertEqual((4000, 9500, 5500), tuple(report[key] for key in ("expensesCents", "incomeCents", "netCents")))
            categoryAmounts = {category["id"]: category["amountCents"] for category in report["categories"]}
            self.assertEqual({501: 3000, 509: 3000, 601: 1000, 607: 1000}, categoryAmounts)
            rollups = {category["id"]: category for category in report["categories"] if category["id"] in {501, 601}}
            self.assertEqual((7500, 2500), (rollups[501]["percentageHundredths"], rollups[601]["percentageHundredths"]))
            self.assertEqual((3000, 1000), (rollups[501]["perDayCents"], rollups[601]["perDayCents"]))
            balances = dict(self.connection.execute("SELECT a.id, a.balanceOffsetCents + COALESCE(SUM(t.amountCents), 0) FROM accounts a LEFT JOIN transactions t ON t.accountId = a.id GROUP BY a.id"))
            self.assertEqual((13500, 0), (balances[101], balances[307]))
            self.assertEqual((2, 0), tuple(self.connection.execute("SELECT COUNT(*), SUM(amountCents) FROM transactions WHERE transferId = ?", (transferId,)).fetchone()))
            self.assertEqual(ledgerBefore, self.ledgerSnapshot())
            self.assertEqual(templateBefore, tuple(self.connection.execute("SELECT * FROM transactionTemplates WHERE id = ?", (templateId,)).fetchone()))
            self.assertEqual("709", self.connection.execute("SELECT value FROM applicationSettings WHERE key = 'reimbursementCategoryId'").fetchone()[0])
            self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])

        assertFinancialResults()
        updateAccount(self.connection, 101, "Renamed cash", "cash", active=False)
        updateAccount(self.connection, 307, "Renamed credit", "credit")
        for categoryId, role, parentId in [(501, "expense", None), (509, "expense", 501),
                                            (601, "expense", None), (607, "expense", 601),
                                            (701, "income", None), (709, "income", None)]:
            updateCategory(self.connection, categoryId, f"Renamed {categoryId}", parentId,
                           active=categoryId != 701, offerReimbursement=categoryId == 509, reportingRole=role)
        assertFinancialResults()
        self.connection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        assertFinancialResults()
        self.assertEqual(("Renamed cash", 0), tuple(self.connection.execute("SELECT name, active FROM accounts WHERE id = 101").fetchone()))
        self.assertEqual(("Renamed 701", 0), tuple(self.connection.execute("SELECT name, active FROM categories WHERE id = 701").fetchone()))

    def testRenamedReimbursementDestinationRetainsItsIdAndFutureSelectionDoesNotRewriteHistory(self):
        updateCategory(self.connection, 709, "返金", reportingRole="income")
        expenseId, reimbursementId = createTransactionWithReimbursement(
            self.connection, 101, "2026-10-01", "Shared cost", 509, "8.00", False,
            307, "2026-10-01", "Returned", "3.00",
        )
        self.assertEqual([(expenseId, 509, -800), (reimbursementId, 709, 300)],
                         [tuple(row) for row in self.connection.execute("SELECT id, categoryId, amountCents FROM transactions ORDER BY id")])
        client = ownerClient(createApp(self.databasePath))
        response = client.put("/api/settings/reimbursement-category", json={"categoryId": 701})
        self.assertEqual(200, response.status_code)
        ledgerBefore = self.ledgerSnapshot()
        _, newReimbursementId = createTransactionWithReimbursement(
            self.connection, 101, "2026-10-02", "Second cost", 509, "4.00", False,
            101, "2026-10-02", "Second return", "2.00",
        )
        self.assertEqual(ledgerBefore, self.ledgerSnapshot()[:2])
        self.assertEqual((701, 200), tuple(self.connection.execute("SELECT categoryId, amountCents FROM transactions WHERE id = ?", (newReimbursementId,)).fetchone()))

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


if __name__ == "__main__":
    unittest.main()
