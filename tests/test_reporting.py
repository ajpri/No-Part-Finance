"""Reporting totals, account guardrails, utilization, and Category rollups."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import initializeTestDatabase
from tests.test_support import ownerClient
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from app import combinedRecentEvents
from database import dashboardReport, reportingPeriod, updateTemplate
from tests.test_support import copyProductionDatabase, createScenarioCategories
from app import createApp
from database import ValidationError, createAccount, createCategory, createSinkingFundPlan, createTemplate, minimumStatus, sinkingFundSummary, updateAccount
import database as ledger
from users import createUser
from tests.test_support import signIn
import database
from tests.test_support import connectFixtureDatabase as connectDatabase

class PeriodTests(unittest.TestCase):
    def testRequiredPeriodBoundariesAndDayCounts(self):
        today = date(2026, 8, 19)
        self.assertEqual(("2026-08-01", "2026-08-31", 19), reportingPeriod("thisMonth", today=today))
        self.assertEqual(("2026-07-01", "2026-07-31", 31), reportingPeriod("lastMonth", today=today))
        self.assertEqual(("2026-01-01", "2026-08-19", 231), reportingPeriod("yearToDate", today=today))
        self.assertEqual(("2026-07-21", "2026-08-19", 30), reportingPeriod("last30Days", today=today))
        self.assertEqual(("2026-02-01", "2026-02-28", 28), reportingPeriod("month:2026-02", today=today))
        self.assertEqual(("2026-02-03", "2026-02-05", 3), reportingPeriod("custom", "2026-02-03", "2026-02-05", today))

    def testLeapYearAndJanuaryTransition(self):
        self.assertEqual(("2024-02-01", "2024-02-29", 29), reportingPeriod("month:2024-02", today=date(2024, 3, 1)))
        self.assertEqual(("2025-12-01", "2025-12-31", 31), reportingPeriod("lastMonth", today=date(2026, 1, 10)))
        self.assertEqual(("2026-01-01", "2026-01-10", 10), reportingPeriod("yearToDate", today=date(2026, 1, 10)))

class RecentEventTests(unittest.TestCase):
    def testValidTransferIsCombinedAndInvalidTransferRowsRemainVisible(self):
        rows = [
            {"id": 8, "date": "2026-08-21", "accountName": "Savings", "description": "Move", "amountCents": 5000, "categoryName": None, "transferId": "valid"},
            {"id": 7, "date": "2026-08-21", "accountName": "Checking", "description": "Move", "amountCents": -5000, "categoryName": None, "transferId": "valid"},
            {"id": 6, "date": "2026-08-20", "accountName": "Checking", "description": "Broken", "amountCents": -1000, "categoryName": None, "transferId": "invalid"},
            {"id": 5, "date": "2026-08-19", "accountName": "Checking", "description": "Food", "amountCents": -2000, "categoryName": "Wants", "transferId": None},
        ]
        for row in rows:
            row["accountId"] = 2 if row["accountName"] == "Savings" else 1
            row["categoryId"] = 3 if row["categoryName"] else None
        events = combinedRecentEvents(rows, 5)
        self.assertEqual(("Checking → Savings", "Transfer", 5000, True, 8), (events[0]["accountName"], events[0]["categoryName"], events[0]["amountCents"], events[0]["combinedTransfer"], events[0]["id"]))
        self.assertEqual("Broken", events[1]["description"])
        self.assertFalse(events[1]["combinedTransfer"])

class ReportingCalculationTests(unittest.TestCase):
    def setUp(self):
        initializeTestDatabase(self, "report.db")
        self.accountId = createAccount(self.connection, "Checking", "cash", "checking")
        self.categoryIds = createScenarioCategories(self.connection)

    def addLedgerRow(self, description, amountCents, categoryName=None, transferId=None):
        self.connection.execute(
            """INSERT INTO transactions
            (accountId, date, description, amountCents, categoryId, transferId)
            VALUES (?, '2026-08-10', ?, ?, ?, ?)""",
            (self.accountId, description, amountCents,
             self.categoryIds.get(categoryName), transferId),
        )
        self.connection.commit()

    def testIncomeExpensesRefundsNetNeedsPercentagesAndPerDay(self):
        self.addLedgerRow("Recurring", -10000, "Recurring Needs")
        self.addLedgerRow("Groceries", -5000, "Groceries/Gas/Household")
        self.addLedgerRow("Refund", 2000, "Groceries/Gas/Household")
        self.addLedgerRow("Wants", -7000, "Wants")
        self.addLedgerRow("Paycheck", 30000, "Income/Reimbursement")
        self.addLedgerRow("Adjustment", 99999)
        self.addLedgerRow("Transfer out", -1000, None, "test-transfer")
        self.addLedgerRow("Transfer in", 1000, None, "test-transfer")

        report = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        self.assertEqual(20000, report["expensesCents"])
        self.assertEqual(30000, report["incomeCents"])
        self.assertEqual(10000, report["netCents"])
        self.assertEqual(6667, report["spendingPercentageHundredths"])
        self.assertEqual(1053, report["perDayCents"])
        categories = {row["name"]: row for row in report["categories"]}
        self.assertEqual(13000, categories["Needs"]["amountCents"])
        self.assertEqual(6500, categories["Needs"]["percentageHundredths"])
        self.assertEqual(684, categories["Needs"]["perDayCents"])
        self.assertEqual(3000, categories["Groceries/Gas/Household"]["amountCents"])
        self.assertEqual(7000, categories["Wants"]["amountCents"])
        accounts = {row["name"]: row for row in report["accountsByExpense"]}
        self.assertEqual(20000, accounts["Checking"]["amountCents"])
        self.assertEqual(10000, accounts["Checking"]["percentageHundredths"])
        incomeAccounts = {row["name"]: row for row in report["accountsByIncome"]}
        self.assertEqual(30000, incomeAccounts["Checking"]["amountCents"])
        self.assertEqual(10000, incomeAccounts["Checking"]["percentageHundredths"])
        netChangeAccounts = {row["name"]: row for row in report["accountsByNetChange"]}
        self.assertEqual(10000, netChangeAccounts["Checking"]["amountCents"])
        self.assertFalse(netChangeAccounts["Checking"]["isZero"])
        self.assertEqual(
            [("Cash Accounts", 10000)],
            [(group["label"], group["amountCents"]) for group in report["netChangeGroups"]],
        )
        balanceChangeAccounts = {row["name"]: row for row in report["accountsByBalanceChange"]}
        self.assertEqual(109999, balanceChangeAccounts["Checking"]["amountCents"])
        self.assertEqual(
            [("Cash Accounts", 109999)],
            [(group["label"], group["amountCents"]) for group in report["balanceChangeGroups"]],
        )

    def testPercentagesUnavailableWhenDenominatorsAreNotPositive(self):
        report = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        self.assertIsNone(report["spendingPercentageHundredths"])
        self.assertTrue(all(row["percentageHundredths"] is None for row in report["categories"]))
        self.assertTrue(all(row["percentageHundredths"] is None for row in report["accountsByExpense"]))
        self.assertTrue(all(row["percentageHundredths"] is None for row in report["accountsByIncome"]))
        self.assertEqual(0, report["accountsByNetChange"][0]["amountCents"])
        self.assertTrue(report["accountsByNetChange"][0]["isZero"])
        self.assertEqual("Cash Accounts", report["netChangeGroups"][0]["label"])
        self.assertEqual(0, report["netChangeGroups"][0]["amountCents"])
        self.assertEqual([], report["accountsByBalanceChange"])
        self.assertEqual([], report["balanceChangeGroups"])

    def testExpenseAccountsIncludeArchivedActivityAndExcludeIncomeTransfersAndOutsideDates(self):
        archivedId = createAccount(self.connection, "Archived Card", "credit", "Credit Card", displayOrder=50)
        self.connection.execute(
            "UPDATE accounts SET active = 0, balanceOffsetCents = 12345 WHERE id = ?", (archivedId,)
        )
        rows = [
            (archivedId, "2026-08-10", "Purchase", -5000, self.categoryIds["Wants"], None),
            (archivedId, "2026-08-11", "Refund", 1000, self.categoryIds["Wants"], None),
            (archivedId, "2026-08-12", "Income", 9000, self.categoryIds["Income/Reimbursement"], None),
            (archivedId, "2026-08-12", "Income reversal", -1000, self.categoryIds["Income/Reimbursement"], None),
            (archivedId, "2026-08-12", "Uncategorized", -6000, None, None),
            (archivedId, "2026-08-13", "Transfer", 3000, None, "transfer"),
            (archivedId, "2026-09-01", "Outside", -7000, self.categoryIds["Wants"], None),
        ]
        self.connection.executemany("INSERT INTO transactions (accountId, date, description, amountCents, categoryId, transferId) VALUES (?, ?, ?, ?, ?, ?)", rows)
        self.connection.commit()
        report = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        archived = next(row for row in report["accountsByExpense"] if row["id"] == archivedId)
        self.assertEqual(4000, archived["amountCents"])
        self.assertEqual(report["expensesCents"], sum(row["amountCents"] for row in report["accountsByExpense"]))
        archivedNetChange = next(row for row in report["accountsByNetChange"] if row["id"] == archivedId)
        self.assertEqual(4000, archivedNetChange["amountCents"])
        self.assertEqual("Credit Cards and Loans", report["netChangeGroups"][1]["label"])
        self.assertEqual(4000, report["netChangeGroups"][1]["amountCents"])
        archivedBalanceChange = next(
            row for row in report["accountsByBalanceChange"] if row["id"] == archivedId
        )
        self.assertEqual(-1000, archivedBalanceChange["amountCents"])
        self.assertEqual("Credit Cards and Loans", report["balanceChangeGroups"][0]["label"])
        self.assertEqual(-1000, report["balanceChangeGroups"][0]["amountCents"])

    def testIncomeAccountsIncludeArchivedIncomeActivityAndExcludeExpenses(self):
        archivedId = createAccount(self.connection, "Archived Income", "cash", "checking", displayOrder=50)
        self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (archivedId,))
        self.connection.executemany(
            "INSERT INTO transactions (accountId, date, description, amountCents, categoryId) VALUES (?, ?, ?, ?, ?)",
            [
                (archivedId, "2026-08-10", "Paycheck", 9000, self.categoryIds["Income/Reimbursement"]),
                (archivedId, "2026-08-11", "Purchase", -4000, self.categoryIds["Wants"]),
            ],
        )
        self.connection.commit()
        report = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        archived = next(row for row in report["accountsByIncome"] if row["id"] == archivedId)
        self.assertEqual(9000, archived["amountCents"])
        self.assertEqual(report["incomeCents"], sum(row["amountCents"] for row in report["accountsByIncome"]))

    def testExpensesByAccountAndCategoryUsesExactRepresentedCategories(self):
        archivedAccountId = createAccount(
            self.connection, "Archived Card", "credit", "Credit Card", displayOrder=50
        )
        offsetAccountId = createAccount(
            self.connection, "Offset Account", "cash", "checking", displayOrder=25
        )
        self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (archivedAccountId,))
        self.connection.execute(
            "UPDATE categories SET active = 0 WHERE id = ?", (self.categoryIds["Experiences"],)
        )
        rows = [
            (self.accountId, "Purchase", -10000, self.categoryIds["Wants"], None),
            (self.accountId, "Refund", 2000, self.categoryIds["Wants"], None),
            (self.accountId, "Groceries", -3000, self.categoryIds["Groceries/Gas/Household"], None),
            (self.accountId, "Archived category", -2500, self.categoryIds["Experiences"], None),
            (offsetAccountId, "Positive category", -1000, self.categoryIds["Wants"], None),
            (offsetAccountId, "Offsetting refund", 1000, self.categoryIds["Giving"], None),
            (offsetAccountId, "Zero purchase", -500, self.categoryIds["Utilities"], None),
            (offsetAccountId, "Zero refund", 500, self.categoryIds["Utilities"], None),
            (archivedAccountId, "Archived activity", -4000, self.categoryIds["Random Needs"], None),
            (archivedAccountId, "Income excluded", 9000, self.categoryIds["Income/Reimbursement"], None),
            (archivedAccountId, "Transfer excluded", -5000, None, "transfer-id"),
        ]
        self.connection.executemany(
            """INSERT INTO transactions
               (accountId, date, description, amountCents, categoryId, transferId)
               VALUES (?, '2026-08-10', ?, ?, ?, ?)""",
            rows,
        )
        self.connection.commit()

        report = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        groups = report["expensesByAccountAndCategory"]
        self.assertEqual(
            [row["id"] for row in report["accountsByExpense"] if row["id"] in {group["id"] for group in groups}],
            [group["id"] for group in groups],
        )
        checking = next(group for group in groups if group["id"] == self.accountId)
        self.assertEqual(13500, checking["amountCents"])
        self.assertEqual(checking["amountCents"], sum(row["amountCents"] for row in checking["categories"]))
        self.assertEqual(
            ["Needs › Groceries/Gas/Household", "Wants", "Experiences"],
            [row["displayName"] for row in checking["categories"]],
        )
        self.assertEqual(8000, next(row for row in checking["categories"] if row["name"] == "Wants")["amountCents"])
        offset = next(group for group in groups if group["id"] == offsetAccountId)
        self.assertEqual(0, offset["amountCents"])
        self.assertEqual([1000, -1000, 0], [row["amountCents"] for row in offset["categories"]])
        self.assertTrue(next(row for row in offset["categories"] if row["name"] == "Utilities")["isZero"])
        archived = next(group for group in groups if group["id"] == archivedAccountId)
        self.assertEqual(4000, archived["amountCents"])
        self.assertEqual(["Needs › Random Needs"], [row["displayName"] for row in archived["categories"]])

        categoryGroups = report["expensesByCategoryAndAccount"]
        groceries = next(group for group in categoryGroups if group["name"] == "Groceries/Gas/Household")
        self.assertEqual("Needs › Groceries/Gas/Household", groceries["displayName"])
        self.assertEqual(3000, groceries["amountCents"])
        self.assertEqual(groceries["amountCents"], sum(row["amountCents"] for row in groceries["accounts"]))
        wants = next(group for group in categoryGroups if group["name"] == "Wants")
        self.assertEqual(
            [self.accountId, offsetAccountId],
            [row["id"] for row in wants["accounts"]],
        )
        self.assertEqual(9000, wants["amountCents"])
        utilities = next(group for group in categoryGroups if group["name"] == "Utilities")
        self.assertEqual(0, utilities["amountCents"])
        self.assertTrue(utilities["accounts"][0]["isZero"])

        self.connection.execute(
            "UPDATE categories SET parentCategoryId = ? WHERE id = ?",
            (self.categoryIds["Needs"], self.categoryIds["Wants"]),
        )
        self.connection.commit()
        reparented = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        checking = next(
            group for group in reparented["expensesByAccountAndCategory"]
            if group["id"] == self.accountId
        )
        self.assertIn("Needs › Wants", [row["displayName"] for row in checking["categories"]])

    def testNetTransactionsExcludesTransfersAndUncategorizedActivity(self):
        creditId = createAccount(
            self.connection, "Card", "credit", "Credit Card", displayOrder=10
        )
        offsetId = createAccount(
            self.connection, "Offset Activity", "cash", "checking", displayOrder=20
        )
        self.connection.executemany(
            """INSERT INTO transactions
               (accountId, date, description, amountCents, categoryId, transferId)
               VALUES (?, '2026-08-10', ?, ?, ?, ?)""",
            [
                (self.accountId, "Payment out", -5000, None, "payment"),
                (creditId, "Payment in", 5000, None, "payment"),
                (creditId, "Purchase", -2000, self.categoryIds["Wants"], None),
                (self.accountId, "Historical adjustment", 300, None, None),
                (offsetId, "Increase", 100, None, None),
                (offsetId, "Decrease", -100, None, None),
            ],
        )
        self.connection.commit()

        report = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        changes = report["accountsByNetChange"]
        self.assertEqual(
            [self.accountId, offsetId, creditId], [row["id"] for row in changes]
        )
        self.assertEqual(0, changes[0]["amountCents"])
        self.assertEqual(0, changes[1]["amountCents"])
        self.assertEqual(-2000, changes[2]["amountCents"])
        self.assertTrue(changes[0]["isZero"])
        self.assertTrue(changes[1]["isZero"])
        netGroups = report["netChangeGroups"]
        self.assertEqual(["Cash Accounts", "Credit Cards and Loans"], [group["label"] for group in netGroups])
        self.assertEqual([0, -2000], [group["amountCents"] for group in netGroups])
        balanceChanges = report["accountsByBalanceChange"]
        self.assertEqual(
            [self.accountId, offsetId, creditId], [row["id"] for row in balanceChanges]
        )
        self.assertEqual([-4700, 0, -3000], [row["amountCents"] for row in balanceChanges])
        self.assertTrue(balanceChanges[1]["isZero"])
        groups = report["balanceChangeGroups"]
        self.assertEqual(["Cash Accounts", "Credit Cards and Loans"], [group["label"] for group in groups])
        self.assertEqual([-4700, -3000], [group["amountCents"] for group in groups])
        self.assertEqual(
            [self.accountId, offsetId], [row["id"] for row in groups[0]["accounts"]]
        )
        self.assertEqual([creditId], [row["id"] for row in groups[1]["accounts"]])

    def testThisMonthIncludesFutureDatedRowsWithoutChangingElapsedDayCount(self):
        self.connection.execute(
            """INSERT INTO transactions (accountId, date, description, amountCents, categoryId)
            VALUES (?, '2026-08-25', 'Future expense', -1900, ?)""",
            (self.accountId, self.categoryIds["Wants"]),
        )
        self.connection.execute(
            """INSERT INTO transactions (accountId, date, description, amountCents, categoryId)
            VALUES (?, '2026-09-01', 'Outside period', -5000, ?)""",
            (self.accountId, self.categoryIds["Wants"]),
        )
        self.connection.commit()
        report = dashboardReport(self.connection, "thisMonth", today=date(2026, 8, 19))
        self.assertEqual(1900, report["expensesCents"])
        self.assertEqual(19, report["dayCount"])
        self.assertEqual(100, report["perDayCents"])

    def testLedgerDatesRespectInclusiveBoundariesAndThisMonthFutureActivity(self):
        cases = [
            ("thisMonth", date(2024, 2, 10), None, None, "2024-02-01", "2024-02-29", 10),
            ("lastMonth", date(2026, 1, 10), None, None, "2025-12-01", "2025-12-31", 31),
            ("yearToDate", date(2024, 3, 1), None, None, "2024-01-01", "2024-03-01", 61),
            ("last30Days", date(2026, 1, 10), None, None, "2025-12-12", "2026-01-10", 30),
            ("custom", date(2024, 3, 1), "2024-02-29", "2024-03-01", "2024-02-29", "2024-03-01", 2),
        ]
        from datetime import timedelta
        for period, today, start, end, firstDate, lastDate, dayCount in cases:
            with self.subTest(period=period):
                self.connection.execute("DELETE FROM transactions")
                outsideBefore = (date.fromisoformat(firstDate) - timedelta(days=1)).isoformat()
                outsideAfter = (date.fromisoformat(lastDate) + timedelta(days=1)).isoformat()
                self.connection.executemany(
                    "INSERT INTO transactions (accountId,date,description,amountCents,categoryId) VALUES (?,?,?,?,?)",
                    [(self.accountId, outsideBefore, "Before", -99999, self.categoryIds["Wants"]),
                     (self.accountId, firstDate, "First expense", -1000, self.categoryIds["Wants"]),
                     (self.accountId, lastDate, "Last refund", 200, self.categoryIds["Wants"]),
                     (self.accountId, lastDate, "Last income", 2000, self.categoryIds["Income/Reimbursement"]),
                     (self.accountId, lastDate, "Last reversal", -500, self.categoryIds["Income/Reimbursement"]),
                     (self.accountId, outsideAfter, "After", 99999, self.categoryIds["Income/Reimbursement"])],
                )
                self.connection.commit()
                report = dashboardReport(self.connection, period, start, end, today)
                self.assertEqual((firstDate, lastDate, dayCount, 800, 1500, 700), tuple(report[key] for key in
                    ("periodStart", "periodEnd", "dayCount", "expensesCents", "incomeCents", "netCents")))
                with patch("app.date") as applicationDate, patch("database.date") as databaseDate:
                    applicationDate.today.return_value = today
                    databaseDate.today.return_value = today
                    client = ownerClient(createApp(self.databasePath))
                    response = client.get("/api/transactions", query_string={
                        "period": period, "startDate": start or "", "endDate": end or "", "includeSummary": "true",
                    })
                    self.assertEqual(200, response.status_code)
                    result = response.get_json()
                    self.assertEqual({"First expense", "Last refund", "Last income", "Last reversal"}, {row["description"] for row in result["transactions"]})
                    self.assertEqual({"expenseCents": 800, "incomeCents": 1500, "transferPaymentCents": 0}, result["summary"])

    def testNegativeTotalsAndNegativeCategoryNetsUseUnavailableDenominators(self):
        self.addLedgerRow("Refund exceeds purchases", 300, "Wants")
        self.addLedgerRow("Income reversal", -200, "Income/Reimbursement")
        report = dashboardReport(self.connection, "custom", "2026-08-10", "2026-08-12")
        self.assertEqual((-300, -200, 100, -100), tuple(report[key] for key in ("expensesCents", "incomeCents", "netCents", "perDayCents")))
        self.assertIsNone(report["spendingPercentageHundredths"])
        self.assertTrue(all(row["percentageHundredths"] is None for row in report["categories"]))
        self.assertTrue(all(row["percentageHundredths"] is None for row in report["accountsByExpense"] + report["accountsByIncome"]))
        self.addLedgerRow("Positive other category", -600, "Giving")
        positiveTotal = dashboardReport(self.connection, "custom", "2026-08-10", "2026-08-12")
        categories = {row["name"]: row for row in positiveTotal["categories"]}
        self.assertEqual((-300, -10000, -100), tuple(categories["Wants"][key] for key in ("amountCents", "percentageHundredths", "perDayCents")))
        self.assertEqual((600, 20000, 200), tuple(categories["Giving"][key] for key in ("amountCents", "percentageHundredths", "perDayCents")))

class HistoryReportingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "history.db"
        self.client = ownerClient(createApp(self.path))
        self.connection = connectDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.cashId = createAccount(self.connection, "Zulu wallet", "cash")
        self.creditId = createAccount(self.connection, "Alpha liability", "credit")
        self.expenseId = createCategory(self.connection, "Salary costs", reportingRole="expense")
        self.incomeId = createCategory(self.connection, "Shopping receipts", reportingRole="income")
        self.connection.executemany(
            "INSERT INTO transactions (accountId,date,description,amountCents,categoryId,transferId) VALUES (?, '2024-02-29', ?, ?, ?, ?)",
            [(self.cashId, "Purchase", -2000, self.expenseId, None),
             (self.cashId, "Refund", 500, self.expenseId, None),
             (self.cashId, "Receipt", 3000, self.incomeId, None),
             (self.cashId, "Reversal", -1000, self.incomeId, None),
             (self.cashId, "History", -100, None, None),
             (self.cashId, "Move", -700, None, "valid"),
             (self.creditId, "Move", 700, None, "valid"),
             (self.cashId, "Invalid categorized link", -200, self.expenseId, "broken")],
        )
        self.connection.commit()

    def history(self, **filters):
        response = self.client.get("/api/transactions", query_string={"period": "all", "includeSummary": "true", **filters})
        self.assertEqual(200, response.status_code)
        return response.get_json()

    def testGroupingPrecedesEveryFilterAndArchivedReferencesRetainSummary(self):
        self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.creditId,))
        self.connection.execute("UPDATE categories SET active = 0 WHERE id = ?", (self.expenseId,))
        self.connection.commit()
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        for accountId in (self.cashId, self.creditId):
            with self.subTest(accountId=accountId):
                result = self.history(accountId=accountId, type="transfers", description="Move", amountMode="exact", amount="7.00")
                self.assertEqual(1, len(result["transactions"]))
                self.assertTrue(result["transactions"][0]["combinedTransfer"])
                self.assertEqual((self.cashId, self.creditId, 700), tuple(result["transactions"][0][key] for key in ("fromAccountId", "toAccountId", "displayAmountCents")))
                self.assertEqual({"expenseCents": 0, "incomeCents": 0, "transferPaymentCents": 700}, result["summary"])
        categorized = self.history(categoryId=self.expenseId)
        self.assertEqual({"Purchase", "Refund"}, {row["description"] for row in categorized["transactions"]})
        self.assertEqual({"expenseCents": 1500, "incomeCents": 0, "transferPaymentCents": 0}, categorized["summary"])
        self.assertEqual([], self.history(categoryId=self.expenseId, type="transfers")["transactions"])
        self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])

    def testAllHistorySortsUseDisplayedEventValuesWithNullCategoriesLast(self):
        expectedOrders = {
            "date": ["Purchase", "Refund", "Receipt", "Reversal", "History", "Move", "Invalid categorized link"],
            "account": ["Purchase", "Refund", "Receipt", "Reversal", "History", "Invalid categorized link", "Move"],
            "description": ["History", "Invalid categorized link", "Move", "Purchase", "Receipt", "Refund", "Reversal"],
            "category": ["Purchase", "Refund", "Invalid categorized link", "Receipt", "Reversal", "Move", "History"],
            "amount": ["Reversal", "Refund", "Invalid categorized link", "History", "Move", "Purchase", "Receipt"],
        }
        # Salary sorts before Shopping, and the combined event's category is Transfer.
        for sort, expected in expectedOrders.items():
            for direction in ("asc", "desc"):
                with self.subTest(sort=sort, direction=direction):
                    result = self.history(sort=sort, direction=direction)
                    if direction == "desc":
                        expectedDescriptions = list(reversed(expected))
                        if sort == "category":
                            expectedDescriptions = [name for name in expectedDescriptions if name != "History"] + ["History"]
                    else:
                        expectedDescriptions = expected
                    self.assertEqual(expectedDescriptions, [row["description"] for row in result["transactions"]])
                    self.assertEqual({"expenseCents": 1500, "incomeCents": 2000, "transferPaymentCents": 700}, result["summary"])

    def testRoleSignsAgreeAcrossHistoryRecentAndPinnedWhileInvalidLinksKeepLedgerSigns(self):
        expected = {"Purchase": 2000, "Refund": -500, "Receipt": 3000, "Reversal": -1000,
                    "History": -100, "Move": 700, "Invalid categorized link": -200}
        history = self.history()
        self.assertEqual(expected, {row["description"]: row["displayAmountCents"] for row in history["transactions"]})
        self.assertEqual(expected, {row["description"]: row["displayAmountCents"] for row in
            combinedRecentEvents([dict(row) for row in self.connection.execute(
                "SELECT t.*, a.name AS accountName, c.name AS categoryName, c.reportingRole AS categoryRole FROM transactions t JOIN accounts a ON a.id=t.accountId LEFT JOIN categories c ON c.id=t.categoryId ORDER BY t.id DESC"
            )], limit=20)})
        for description in ("Purchase", "Refund", "Receipt", "Reversal"):
            self.connection.execute("UPDATE transactions SET pinned = 1 WHERE description = ?", (description,))
        self.connection.commit()
        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertEqual({name: expected[name] for name in ("Purchase", "Refund", "Receipt", "Reversal")},
                         {row["description"]: row["displayAmountCents"] for row in dashboard["quickUpdates"]})
        self.assertEqual({name: expected[name] for name in ("History", "Move", "Invalid categorized link")},
                         {row["description"]: row["displayAmountCents"] for row in dashboard["recentTransactions"]})

    def testEditAmountsAgreeAcrossHistoryRecentAndPinnedWithoutChangingLedger(self):
        expectedOrdinary = {"Purchase": 2000, "Refund": -500, "Receipt": 3000, "Reversal": -1000, "History": -100}
        expectedHistory = {**expectedOrdinary, "Move": -700, "Invalid categorized link": 200}
        # Keep the five ordinary rows newest so each can be edited from Recent.
        with self.connection:
            self.connection.execute("UPDATE transactions SET date = '2020-01-01' WHERE transferId IS NOT NULL")
        for accountId in (self.cashId, self.creditId):
            for archived in (False, True):
                for pinned in (False, True):
                    with self.subTest(accountId=accountId, archived=archived, pinned=pinned):
                        with self.connection:
                            self.connection.execute("UPDATE transactions SET accountId = ?, pinned = ? WHERE transferId IS NULL", (accountId, pinned))
                            self.connection.execute("UPDATE accounts SET active = ? WHERE id IN (?, ?)", (not archived, self.cashId, self.creditId))
                            self.connection.execute("UPDATE categories SET active = ? WHERE id IN (?, ?)", (not archived, self.expenseId, self.incomeId))
                        before = list(self.connection.iterdump())
                        history = self.history()["transactions"]
                        self.assertEqual(expectedHistory, {row["description"]: row["enteredAmountCents"] for row in history})
                        dashboardResponse = self.client.get("/api/dashboard")
                        self.assertEqual(200, dashboardResponse.status_code)
                        dashboard = dashboardResponse.get_json()
                        ordinaryEvents = dashboard["quickUpdates" if pinned else "recentTransactions"]
                        self.assertEqual(expectedOrdinary, {row["description"]: row["enteredAmountCents"] for row in ordinaryEvents})
                        self.assertTrue(all(row["accountId"] == accountId for row in ordinaryEvents))
                        self.assertTrue(all(type(row["enteredAmountCents"]) is int for row in history + ordinaryEvents))
                        if pinned:
                            self.assertEqual({"Move": -700, "Invalid categorized link": 200},
                                             {row["description"]: row["enteredAmountCents"] for row in dashboard["recentTransactions"]})
                        else:
                            self.assertEqual([], dashboard["quickUpdates"])
                        self.assertEqual(before, list(self.connection.iterdump()), "Editing metadata reads must not change financial or audit records")

    def testHistoryEligibilityReusesJoinedMetadataWithoutPerEventQueries(self):
        import app as applicationModule
        with self.connection:
            self.connection.executemany("INSERT INTO transactions (accountId,date,description,amountCents,categoryId) VALUES (?, '2024-02-29', ?, ?, ?)",
                                        [(self.cashId, f"Synthetic entry {index}", -100 if index % 2 else 100, self.expenseId) for index in range(40)])
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.creditId,))
        statements = []
        originalConnect = applicationModule.connectDatabase
        def tracedConnect(*arguments, **keywords):
            connection = originalConnect(*arguments, **keywords)
            connection.set_trace_callback(statements.append)
            return connection
        before = list(self.connection.iterdump())
        with patch.object(applicationModule, "connectDatabase", tracedConnect):
            events = self.history()["transactions"]
        self.assertGreater(len(events), 40)
        self.assertEqual(1, sum("FROM scopedtransactions AS t" in statement for statement in statements))
        self.assertFalse(any("WHERE t.id =" in statement or "WHERE t.transferId =" in statement for statement in statements), "History must not issue one eligibility lookup per event")
        for event in events:
            try:
                database.simulatorLedgerMapping(self.connection, None if event["transferId"] else event["id"], event["transferId"])
                expected = True
            except ValidationError:
                expected = False
            self.assertIs(expected, event["simulatorEligible"])
            self.assertFalse({"accountActive", "categoryActive", "hasChildren"} & event.keys(), "Internal join metadata must not change the history response shape")
        self.assertEqual(before, list(self.connection.iterdump()))

    def testRecentEventsLimitFollowsGroupingPinsAndDeterministicDateOrder(self):
        self.connection.execute("UPDATE transactions SET pinned = 1, date = '2024-03-03' WHERE description = 'Purchase'")
        self.connection.execute("UPDATE transactions SET date = '2024-03-02' WHERE description = 'Refund'")
        self.connection.execute("UPDATE transactions SET date = '2024-03-01' WHERE transferId = 'valid'")
        self.connection.executemany(
            "INSERT INTO transactions (accountId, date, description, amountCents, categoryId) VALUES (?, ?, ?, ?, ?)",
            [(self.cashId, "2024-02-28", "Older with higher ID", -300, self.expenseId),
             (self.cashId, "2024-02-29", "Newest ID on tied date", -400, self.expenseId)],
        )
        self.connection.commit()
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")]
        expectedDescriptions = ["Refund", "Move", "Newest ID on tied date", "Invalid categorized link", "History"]

        for refresh in range(2):
            with self.subTest(refresh=refresh):
                response = self.client.get("/api/dashboard")
                self.assertEqual(200, response.status_code)
                dashboard = response.get_json()
                recentEvents = dashboard["recentTransactions"]
                self.assertEqual(5, len(recentEvents))
                self.assertEqual(expectedDescriptions, [event["description"] for event in recentEvents])
                self.assertEqual(["Purchase"], [event["description"] for event in dashboard["quickUpdates"]])
                self.assertEqual([False, True, False, False, False], [event["combinedTransfer"] for event in recentEvents])
                self.assertEqual((self.cashId, self.creditId, 700), tuple(recentEvents[1][key] for key in ("fromAccountId", "toAccountId", "displayAmountCents")))
                eventOrder = [(event["date"], event["id"]) for event in recentEvents]
                self.assertEqual(sorted(eventOrder, reverse=True), eventOrder)

        self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions ORDER BY id")])

class TemplateTests(unittest.TestCase):
    def setUp(self):
        initializeTestDatabase(self, "templates.db")
        self.accountId = createAccount(self.connection, "Checking", "cash", "checking")
        self.categoryId = createScenarioCategories(self.connection)["Wants"]

    def testTemplateStoresEntryConventionAndNeverCreatesTransaction(self):
        beforeCount = self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        templateId = createTemplate(
            self.connection, "Lunch", self.accountId, "Cafe", self.categoryId, "12.34", 2
        )
        template = self.connection.execute(
            "SELECT * FROM transactionTemplates WHERE id = ?", (templateId,)
        ).fetchone()
        self.assertEqual(1234, template["enteredAmountCents"])
        self.assertEqual(beforeCount, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        updateTemplate(
            self.connection, templateId, "Lunch", self.accountId, "Cafe Updated",
            self.categoryId, "-2.00", 3, False,
        )
        updated = self.connection.execute(
            "SELECT enteredAmountCents, active FROM transactionTemplates WHERE id = ?", (templateId,)
        ).fetchone()
        self.assertEqual((-200, 0), tuple(updated))

    def testPartialTemplateCanBeCreatedUpdatedAndDeletedWithoutTransactions(self):
        templateId = createTemplate(self.connection, "Amount later", None, None, None, None, 7)
        template = self.connection.execute("SELECT * FROM transactionTemplates WHERE id = ?", (templateId,)).fetchone()
        self.assertEqual((None, None, None, None, 7), (template["accountId"], template["description"], template["categoryId"], template["enteredAmountCents"], template["displayOrder"]))
        from database import deleteTemplate
        deleteTemplate(self.connection, templateId)
        self.assertIsNone(self.connection.execute("SELECT id FROM transactionTemplates WHERE id = ?", (templateId,)).fetchone())
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testTemplateAmountWhenPresentMustBeNonzero(self):
        from database import ValidationError
        with self.assertRaisesRegex(ValidationError, "nonzero"):
            createTemplate(self.connection, "Zero", None, None, None, "0.00", 0)

class ImportedReportingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporaryDirectory.cleanup)
        self.databasePath = Path(self.temporaryDirectory.name) / "finance-copy.db"
        copyProductionDatabase(self, self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        self.client = ownerClient(createApp(self.databasePath), copiedDatabase=True)

    def tearDown(self):
        self.connection.close()

    def testRealImportedThisMonthReportMatchesIndependentSql(self):
        response = self.client.get("/api/dashboard?period=thisMonth")
        self.assertEqual(200, response.status_code)
        dashboard = response.get_json()
        periodStart, periodEnd, _ = reportingPeriod("thisMonth")
        rawRows = self.connection.execute(
            """
            SELECT c.reportingRole, SUM(t.amountCents) AS amountCents
            FROM transactions AS t
            JOIN categories AS c ON c.id = t.categoryId
            WHERE t.transferId IS NULL AND t.date BETWEEN ? AND ?
            GROUP BY c.id
            """,
            (periodStart, periodEnd),
        ).fetchall()
        income = sum(row["amountCents"] for row in rawRows if row["reportingRole"] == "income")
        expenses = -sum(row["amountCents"] for row in rawRows if row["reportingRole"] == "expense")
        self.assertEqual(income, dashboard["incomeCents"])
        self.assertEqual(expenses, dashboard["expensesCents"])
        self.assertEqual(income - expenses, dashboard["netCents"])
        self.assertEqual(date.today().day, dashboard["dayCount"])
        recentRows = [dict(row) for row in self.connection.execute(
            """SELECT t.*, a.name AS accountName, c.name AS categoryName
               FROM transactions t JOIN accounts a ON a.id = t.accountId
               LEFT JOIN categories c ON c.id = t.categoryId
               WHERE t.pinned = 0 ORDER BY t.date DESC, t.id DESC"""
        )]
        self.assertEqual(min(5, len(combinedRecentEvents(recentRows, len(recentRows)))), len(dashboard["recentTransactions"]))
        recentOrder = [(row["date"], row["id"]) for row in dashboard["recentTransactions"]]
        self.assertEqual(sorted(recentOrder, reverse=True), recentOrder)
        for category in dashboard["categories"]:
            children = [row for row in dashboard["categories"] if row["parentCategoryId"] == category["id"]]
            if children:
                self.assertEqual(sum(row["amountCents"] for row in children), category["amountCents"])

class DashboardUiTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporaryDirectory.cleanup)
        self.databasePath = Path(self.temporaryDirectory.name) / "dashboard-ui.db"
        self.client = ownerClient(createApp(self.databasePath))

    def testDashboardUiContainsRequiredPlainControls(self):
        htmlResponse = self.client.get("/")
        cssResponse = self.client.get("/styles.css")
        html = htmlResponse.get_data(as_text=True)
        css = cssResponse.get_data(as_text=True)
        htmlResponse.close()
        cssResponse.close()
        self.assertLess(html.index("Cash Accounts"), html.index("Credit Cards and Loans"))
        self.assertLess(html.index("Credit Cards and Loans"), html.index("Transaction Breakdown"))
        self.assertLess(html.index("Transaction Breakdown"), html.index("Pinned Transactions"))
        self.assertLess(html.index("Pinned Transactions"), html.index("Recent Transactions"))
        self.assertIn('id="totalCreditBalance"', html)
        self.assertIn('id="totalCreditUtilization"', html)
        self.assertLess(html.index("Transaction Breakdown"), html.index("Recent Transactions"))
        self.assertLess(html.index("Template"), html.index("transactionDate"))
        self.assertIn('id="dashboardStartDate" type="date" disabled', html)
        self.assertIn('id="dashboardEndDate" type="date" disabled', html)
        self.assertIn('id="dashboardCustomDates" class="customDateControls" hidden', html)
        self.assertIn("Hide zeros", html)
        self.assertIn("Expense by Category", html)
        self.assertIn("Expense by Account", html)
        self.assertIn("Expense Details by Category", html)
        self.assertIn("Expense Details by Account", html)
        self.assertIn("Income by Account", html)
        self.assertIn("Net Transactions by Account", html)
        self.assertIn("Balance Change by Account", html)
        self.assertLess(html.index("Expense by Category</option>"), html.index("Expense Details by Category"))
        self.assertLess(html.index("Expense Details by Category"), html.index("Expense by Account</option>"))
        self.assertLess(html.index("Expense by Account</option>"), html.index("Expense Details by Account"))
        self.assertLess(html.index("Expense Details by Account"), html.index("Income by Account"))
        self.assertLess(html.index("Income by Account"), html.index("Net Transactions by Account"))
        self.assertLess(html.index("Net Transactions by Account"), html.index("Balance Change by Account"))
        self.assertIn('<label>Breakdown <select id="dashboardBreakdown">', html)
        self.assertLess(html.index('id="dashboardBreakdown"'), html.index('id="dashboardPeriod"'))
        self.assertLess(html.index('id="dashboardPeriod"'), html.index('id="dashboardStartDate"'))
        self.assertLess(html.index('id="dashboardStartDate"'), html.index('id="dashboardEndDate"'))
        self.assertNotIn('data-breakdown-view=', html)
        self.assertNotIn("<h3>26.10</h3>", html)
        self.assertIn("<h3>Earlier versions</h3>", html)
        self.assertIn('data-breakdown-panel="expenseCategory"', html)
        self.assertIn('data-breakdown-panel="expenseAccount" hidden', html)
        self.assertIn('data-breakdown-panel="expenseAccountCategory" hidden', html)
        self.assertIn('<th>Account / Category</th><th class="amount">Amount</th>', html)
        self.assertIn('data-breakdown-panel="expenseCategoryAccount" hidden', html)
        self.assertIn('<th>Category / Account</th><th class="amount">Amount</th>', html)
        self.assertIn('data-breakdown-panel="incomeAccount" hidden', html)
        self.assertIn('data-breakdown-panel="netChangeAccount" hidden', html)
        self.assertIn('<th>Account</th><th class="amount">Net Change</th>', html)
        self.assertIn('data-breakdown-panel="balanceChangeAccount" hidden', html)
        self.assertIn('<th>Account</th><th class="amount">Balance Change</th>', html)
        self.assertIn("Bulk Entry", html)
        self.assertIn("Reconcile Balance", html)
        self.assertIn("Download Backup", html)
        self.assertIn("Currently calculated balance", html)
        self.assertIn('id="actualBalance"', html)
        self.assertNotIn('id="adjustmentDate"', html)
        self.assertNotIn('id="openingDate"', html)
        self.assertNotIn("#recentTransactionRows tr:nth-child(n+4)", css)
        self.assertIn("[hidden] { display: none !important; }", css)
        self.assertIn("#expenseCategoryTable { min-width: 34rem; }", css)
        self.assertNotIn("#expenseCategoryTable tr {", css)
        self.assertNotIn("#expenseCategoryTable thead { display: none; }", css)
        self.assertNotIn("th:nth-child(4), td:nth-child(4) { display: none; }", css)
        javascriptResponse = self.client.get("/app.js")
        javascript = javascriptResponse.get_data(as_text=True)
        javascriptResponse.close()
        self.assertIn('document.querySelector("#dashboardBreakdown").addEventListener("change"', javascript)
        self.assertIn("function renderDashboardExpenseAccountsAndCategories", javascript)
        self.assertIn("function renderDashboardExpenseCategoriesAndAccounts", javascript)
        self.assertIn("function renderDashboardNetChangeAccounts", javascript)
        self.assertIn("for (const group of dashboard.netChangeGroups)", javascript)
        self.assertIn("function renderDashboardBalanceChangeAccounts", javascript)
        self.assertGreaterEqual(javascript.count("!account.isZero"), 2)
        self.assertIn("dollars(account.amountCents, true)", javascript)
        self.assertIn("for (const group of dashboard.balanceChangeGroups)", javascript)
        self.assertIn("const visibleAccounts = group.accounts.filter", javascript)
        self.assertIn("if (!visibleAccounts.length) continue", javascript)
        self.assertIn("groupHeading.textContent = group.label", javascript)
        self.assertIn("groupAmount.textContent = dollars(group.amountCents, true)", javascript)
        self.assertIn("const visibleCategories = account.categories.filter", javascript)
        self.assertIn("const visibleAccounts = category.accounts.filter", javascript)
        self.assertIn("!category.isZero", javascript)
        self.assertIn("!account.isZero", javascript)
        self.assertIn("if (!visibleCategories.length) continue", javascript)
        self.assertIn("if (!visibleAccounts.length) continue", javascript)
        self.assertGreaterEqual(javascript.count('className = "expenseDetailGroup"'), 2)
        self.assertGreaterEqual(javascript.count('className = "expenseDetailChild"'), 2)
        self.assertIn(".expenseDetailGroup th, .expenseDetailGroup td { background: var(--background); font-weight: 600; }", css)
        self.assertIn(".expenseDetailChild td:first-child { padding-left: 1.5rem; }", css)
        self.assertIn('window.history.pushState({}, "", ledgerUrl(transactionFilterUrlFromForm()))', javascript)
        self.assertIn('window.addEventListener("popstate"', javascript)
        self.assertIn('urlFilters.set("account", `id:${account.id}`)', javascript)
        self.assertIn('urlFilters.set("category", `id:${category.id}`)', javascript)
        self.assertIn('urlFilters.set("search", search)', javascript)
        self.assertIn("function localCalendarDate", javascript)
        self.assertIn("currentDate.getFullYear()", javascript)
        self.assertIn("currentDate.getMonth()", javascript)
        self.assertIn("currentDate.getDate()", javascript)
        self.assertNotIn("toISOString()", javascript)
        self.assertIn('document.querySelector("#transactionAccount").value = ""', javascript)
        self.assertIn('document.querySelector("#transactionDescription").value = ""', javascript)
        self.assertIn('document.querySelector("#transactionCategory").value = ""', javascript)
        self.assertIn('document.querySelector("#transactionAmount").value = ""', javascript)
        self.assertIn("function updateTransferPresentation", javascript)
        self.assertIn('document.querySelector("#transferToAccount").addEventListener("change"', javascript)
        self.assertIn("item.dataset.accountClass = row.accountClass", javascript)
        self.assertIn('dataset.accountClass === "credit"', javascript)
        self.assertIn('<button id="transferSubmitButton" class="positiveAction" type="submit">Save</button>', html)
        self.assertIn('document.querySelector("#transferSubmitButton").textContent = isPayment ? "Make Payment" : "Save"', javascript)
        self.assertIn('destination?.defaultTransferDescription', javascript)
        self.assertIn("children.some((item) => item.amountCents !== 0)", javascript)
        self.assertIn("hideZero && account.amountCents === 0", javascript)
        self.assertIn('document.querySelector("#dashboardCustomDates").hidden = !custom', javascript)
        self.assertIn("pointerStartedOnBackdrop", javascript)
        self.assertIn("data-sort", html)
        self.assertIn("Release History", html)
        self.assertIn("About the Name", html)
        self.assertNotIn("gradient", css.lower())

class MinimumAndPlanningReportTests(unittest.TestCase):
    def setUp(self):
        initializeTestDatabase(self, "finance.db")

    def testCashMinimumValidationDefaultsMigrationAndClassChange(self):
        accountId = createAccount(
            self.connection, "Checking", "cash", "checking", openingBalance="1000.00",
            minimumBalance="1000.00",
        )
        account = self.connection.execute("SELECT * FROM accounts WHERE id = ?", (accountId,)).fetchone()
        self.assertEqual((100000, 25), (account["minimumBalanceCents"], account["minimumCautionPercent"]))
        zeroMinimumId = createAccount(self.connection, "No minimum", "cash", minimumBalance="0")
        zeroMinimum = self.connection.execute("SELECT * FROM accounts WHERE id = ?", (zeroMinimumId,)).fetchone()
        self.assertEqual((None, None), (zeroMinimum["minimumBalanceCents"], zeroMinimum["minimumCautionPercent"]))
        with self.assertRaises(ValidationError):
            createAccount(self.connection, "Bad", "cash", minimumBalance="-0.01")
        updateAccount(self.connection, accountId, "Checking", "cash", "checking", minimumBalance="0.00", minimumCautionPercent="25")
        account = self.connection.execute("SELECT * FROM accounts WHERE id = ?", (accountId,)).fetchone()
        self.assertEqual((None, None), (account["minimumBalanceCents"], account["minimumCautionPercent"]))
        updateAccount(self.connection, accountId, "Checking", "cash", "checking", minimumBalance="1000.00")
        updateAccount(self.connection, accountId, "Checking", "credit", "Other")
        account = self.connection.execute("SELECT * FROM accounts WHERE id = ?", (accountId,)).fetchone()
        self.assertEqual((None, None), (account["minimumBalanceCents"], account["minimumCautionPercent"]))

    def testMinimumThresholdUsesIntegerCeilingAndExactBoundaries(self):
        self.assertEqual("red", minimumStatus(100001, 25, 100000)["state"])
        threshold = minimumStatus(100001, 25, 125002)["greenThresholdCents"]
        self.assertEqual(125002, threshold)
        self.assertEqual("yellow", minimumStatus(100001, 25, 125001)["state"])
        self.assertEqual("green", minimumStatus(100001, 25, 125002)["state"])
        self.assertEqual("At minimum", minimumStatus(100001, 0, 100001)["label"])

    def testDashboardAlertsAndBackendHelper(self):
        createAccount(self.connection, "Low", "cash", openingBalance="50.00", minimumBalance="100.00", minimumCautionPercent="0")
        self.connection.close()
        client = ownerClient(createApp(self.databasePath))
        dashboard = client.get("/api/dashboard").get_json()
        self.assertEqual(["Low"], [item["accountName"] for item in dashboard["minimumBalanceAlerts"]])
        helper = client.post("/api/accounts/minimum-helper", json={"minimumBalance": "1000.00", "minimumCautionPercent": "25"}).get_json()
        self.assertEqual("Green: $1,250.00 and above", helper["lines"][0])
        clearedHelper = client.post("/api/accounts/minimum-helper", json={"minimumBalance": "0", "minimumCautionPercent": "25"}).get_json()
        self.assertEqual({"minimumBalanceCents": None, "minimumCautionPercent": None, "lines": []}, clearedHelper)
        self.connection = connectDatabase(self.databasePath)

    def testSinkingFundTargetAcceptsPastAndFutureSelectedDates(self):
        accountId = createAccount(self.connection, "Checking", "cash")
        categoryId = createCategory(self.connection, "Annual", reportingRole="expense")
        templateId = createTemplate(self.connection, "Insurance", accountId, "Insurance", categoryId, "1200.00")
        self.connection.execute("INSERT INTO applicationSettings (key, value) VALUES ('paychecksPerYear', '24')")
        self.connection.commit()
        createSinkingFundPlan(self.connection, templateId, "yearly", "2026-12-31")
        past = sinkingFundSummary(self.connection, date(2026, 1, 1))["plans"][0]["reserveTargetCents"]
        future = sinkingFundSummary(self.connection, date(2026, 12, 31))["plans"][0]["reserveTargetCents"]
        self.assertLess(past, future)
        self.assertEqual(120000, future)

    def testSelectedSinkingDateDoesNotChangeComingUpOrOverview(self):
        accountId = createAccount(self.connection, "Checking", "cash")
        categoryId = createCategory(self.connection, "Annual", reportingRole="expense")
        templateId = createTemplate(self.connection, "Insurance", accountId, "Insurance", categoryId, "1200.00")
        self.connection.execute("INSERT INTO applicationSettings (key, value) VALUES ('paychecksPerYear', '24')")
        self.connection.commit()
        createSinkingFundPlan(self.connection, templateId, "yearly", date.today().isoformat())
        self.connection.close()
        client = ownerClient(createApp(self.databasePath))
        normal = client.get("/api/budget").get_json()
        selected = client.get("/api/budget?date=2030-01-15").get_json()
        self.assertEqual(normal["comingUp"], selected["comingUp"])
        self.assertEqual(normal["monthlyRequirementCents"], selected["monthlyRequirementCents"])
        self.assertEqual(normal["totalMonthlyContributionCents"], selected["totalMonthlyContributionCents"])
        self.connection = connectDatabase(self.databasePath)

    def testProjectedUtilizationBadgeUsesPercentageOnlyForVisibleLabel(self):
        cashId = createAccount(self.connection, "Cash", "cash", openingBalance="100.00")
        cardId = createAccount(self.connection, "Card", "credit", "Credit Card", creditLimit="100.00", openingBalance="30.00")
        self.connection.execute("INSERT INTO simulatorPlans (name, planDate) VALUES ('Plan', '2026-08-27')")
        planId = self.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.connection.execute("INSERT INTO simulatorRows (planId, rowType, rowOrder, fromAccountId) VALUES (?, 'live', 1, ?)", (planId, cashId))
        self.connection.execute("INSERT INTO simulatorRows (planId, rowType, rowOrder, fromAccountId, toAccountId, label, amountCents) VALUES (?, 'transfer', 2, ?, ?, 'Payment', 1)", (planId, cashId, cardId))
        self.connection.commit()
        from database import simulatorWorkspace
        card = next(item for item in simulatorWorkspace(self.connection, planId)["plan"]["results"] if item["id"] == cardId)
        self.assertEqual("29.99%", card["projectedUtilization"]["label"])
        self.assertIn("projected utilization", card["projectedUtilization"]["accessibleLabel"])

class DashboardUtilizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.directory.name) / "synthetic-dashboard.db"
        self.backupsPath = Path(self.directory.name) / "backups"
        self.backupsPath.mkdir()
        self.application = createApp(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        integrationId = ledger.createIntegrationSecret(self.connection, "Example Agent")
        self.secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (integrationId,)).fetchone()[0]
        self.headers = {"Authorization": "Bearer " + self.secret}
        self.agent = self.application.test_client()
        self.owner = self.application.test_client()
        self.auditor = self.application.test_client()
        signIn(self.owner)
        signIn(self.auditor, "Auditor", "auditor password")
        self.cardId = ledger.createAccount(self.connection, "Example Card", "credit", "Credit Card", openingBalance="299.96", creditLimit="1000.00", utilizationWarning="30.00")

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def snapshot(self):
        return tuple(self.connection.iterdump())

    def dashboard(self):
        return self.agent.get("/api/integration/read/dashboard", headers=self.headers).get_json()

    def testExactCurrentAndProjectedThresholdsAcrossAllReadPermissions(self):
        planId = ledger.createSimulatorPlan(self.connection, "Example plan", "2026-10-17")
        # Equal planned inflow/outflow includes the card in Results without changing its balance.
        ledger.saveSimulatorRow(self.connection, planId, None, "spend", self.cardId, label="Example spend", amount="0.01")
        ledger.saveSimulatorRow(self.connection, planId, None, "income", toAccountId=self.cardId, label="Example income", amount="0.01")
        for owed, state, label in (
            (29996, "green", "30.00%"), (30000, "yellow", "30.00%"),
            (30001, "yellow", "30.00%"), (49996, "yellow", "50.00%"),
            (50000, "red", "50.00%"), (50001, "red", "50.00%"),
            (0, "green", "0.00%"), (-1000, "green", "0.00%"),
            (125000, "red", "125.00%"),
        ):
            with self.subTest(owed=owed):
                with self.connection:
                    self.connection.execute("UPDATE accounts SET balanceOffsetCents = ? WHERE id = ?", (-owed, self.cardId))
                before = self.snapshot()
                for client, dashboardPath, simulatorPath, headers in (
                    (self.owner, "/api/dashboard", "/api/simulator", {}),
                    (self.auditor, "/api/dashboard", "/api/simulator", {}),
                    (self.agent, "/api/integration/read/dashboard", "/api/integration/read/simulator", self.headers),
                ):
                    response = client.get(dashboardPath, headers=headers)
                    self.assertEqual(200, response.status_code)
                    dashboard = response.get_json()
                    account = dashboard["accounts"][0]
                    self.assertEqual(-owed, account["balanceCents"])
                    self.assertEqual((state, label), (account["utilization"]["state"], account["utilization"]["label"]))
                    self.assertEqual((state, label), (dashboard["totalUtilization"]["state"], dashboard["totalUtilization"]["label"]))
                    self.assertEqual("Example Card utilization: " + label, account["utilization"]["accessibleLabel"])
                    self.assertEqual("Total revolving utilization: " + label, dashboard["totalUtilization"]["accessibleLabel"])
                    projected = client.get(simulatorPath, query_string={"plan": planId}, headers=headers).get_json()["plan"]["results"][0]
                    self.assertEqual(-owed, projected["projectedBalanceCents"])
                    self.assertEqual((state, label), (projected["projectedUtilization"]["state"], projected["projectedUtilization"]["label"]))
                    self.assertEqual(owed >= 30000, bool(dashboard["creditUtilizationAlerts"]))
                self.assertEqual(before, self.snapshot())

    def testAggregateUsesEligibleSummedDebtAndLimitsAndPreservesWarningConfiguration(self):
        secondCard = ledger.createAccount(self.connection, "Second Card", "credit", "Credit Card", openingBalance="700.00", creditLimit="1000.00")
        overpaid = ledger.createAccount(self.connection, "Overpaid Card", "credit", "Credit Card", openingBalance="0.00", creditLimit="1000.00")
        with self.connection:
            self.connection.execute("UPDATE accounts SET balanceOffsetCents = 1000 WHERE id = ?", (overpaid,))
        archived = ledger.createAccount(self.connection, "Archived Card", "credit", "Credit Card", openingBalance="999.00", creditLimit="1000.00")
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (archived,))
        ineligible = [
            ledger.createAccount(self.connection, "Example Cash", "cash", openingBalance="50.00"),
            ledger.createAccount(self.connection, "Example Loan", "credit", "Personal Loan", openingBalance="100.00"),
            ledger.createAccount(self.connection, "Example Other", "credit", "Other", openingBalance="100.00"),
            ledger.createAccount(self.connection, "Unlimited Card", "credit", "Credit Card", openingBalance="100.00"),
        ]
        before = self.snapshot()
        dashboard = self.dashboard()
        accounts = {account["id"]: account for account in dashboard["accounts"]}
        self.assertNotIn(archived, accounts)
        self.assertFalse(any(account["accountClass"] == "system" for account in accounts.values()))
        self.assertEqual((300000, 99996, "yellow", "33.33%"), (dashboard["totalCreditLimitCents"], dashboard["totalRevolvingOwedCents"], dashboard["totalUtilization"]["state"], dashboard["totalUtilization"]["label"]))
        self.assertEqual("red", accounts[secondCard]["utilization"]["state"])
        self.assertEqual("0.00%", accounts[overpaid]["utilization"]["label"])
        for accountId in ineligible:
            self.assertIsNone(accounts[accountId]["utilization"])
        self.assertEqual([], dashboard["creditUtilizationAlerts"])
        self.assertEqual(3000, accounts[self.cardId]["utilizationWarningBasisPoints"])
        self.assertEqual(0, dashboard["incomeCents"])
        self.assertEqual(0, dashboard["expensesCents"])
        self.assertEqual(before, self.snapshot())

    def testNoEligibleCardsReturnsNullAndUnauthenticatedReadsRemainProtected(self):
        with self.connection:
            self.connection.execute("UPDATE accounts SET creditLimitCents = NULL, utilizationWarningBasisPoints = NULL WHERE id = ?", (self.cardId,))
        before = self.snapshot()
        dashboard = self.dashboard()
        self.assertIsNone(dashboard["accounts"][0]["utilization"])
        self.assertIsNone(dashboard["totalUtilization"])
        signedOut = self.application.test_client()
        self.assertEqual(401, signedOut.get("/api/dashboard").status_code)
        self.assertEqual(401, signedOut.get("/api/integration/read/dashboard").status_code)
        self.assertEqual(401, signedOut.get("/api/integration/read/simulator").status_code)
        self.assertEqual(before, self.snapshot())

    def testDashboardRequiredContractFieldsAndEventGrouping(self):
        import json
        contract = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())
        dashboardShape = contract["paths"]["/api/integration/read/dashboard"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        cashId = ledger.createAccount(self.connection, "Example Cash", "cash", openingBalance="100.00")
        archivedId = ledger.createAccount(self.connection, "Archived Cash", "cash")
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (archivedId,))
        expenseId = ledger.createCategory(self.connection, "Example Expense")
        incomeId = ledger.createCategory(self.connection, "Example Income", reportingRole="income")
        pinnedIds = []
        for categoryId, amount in ((expenseId, "10.00"), (expenseId, "-2.00"), (incomeId, "20.00"), (incomeId, "-3.00")):
            pinnedIds.append(ledger.createTransaction(self.connection, cashId, "2026-10-01", "Example pinned", categoryId, amount, True))
        ordinaryId = ledger.createTransaction(self.connection, cashId, "2026-10-01", "Example ordinary", expenseId, "4.00")
        transferId = ledger.createTransfer(self.connection, "2026-10-01", cashId, self.cardId, "5.00", "Example payment")
        with self.connection:
            invalidId = self.connection.execute("INSERT INTO transactions (accountId,date,description,amountCents,transferId) VALUES (?, '2026-10-01', 'Invalid linked fixture', -123, 'invalid-fixture')", (cashId,)).lastrowid
        before = self.snapshot()
        reference = None
        for client, path, headers in ((self.owner, "/api/dashboard", {}), (self.auditor, "/api/dashboard", {}), (self.agent, "/api/integration/read/dashboard", self.headers)):
            response = client.get(path, headers=headers)
            self.assertEqual(200, response.status_code)
            report = response.get_json()
            for collection in ("accounts", "recentTransactions", "quickUpdates"):
                shape = dashboardShape["properties"][collection]["items"]
                for item in report[collection]:
                    self.assertTrue(set(shape["required"]) <= item.keys())
                    for field in shape["required"]:
                        if shape["properties"][field].get("type") == "boolean":
                            self.assertIs(type(item[field]), bool, field)
            self.assertEqual({cashId, self.cardId}, {account["id"] for account in report["accounts"]})
            self.assertTrue(all(account["ordinaryEligible"] and account["transferEligible"] for account in report["accounts"]))
            self.assertEqual(set(pinnedIds), {event["id"] for event in report["quickUpdates"]})
            self.assertTrue(all(event["combinedTransfer"] is False for event in report["quickUpdates"]))
            self.assertEqual([1000, -200, 2000, -300], [next(item["displayAmountCents"] for item in report["quickUpdates"] if item["id"] == pinnedId) for pinnedId in pinnedIds])
            self.assertTrue(next(event for event in report["recentTransactions"] if event["transferId"] == transferId)["combinedTransfer"])
            self.assertFalse(next(event for event in report["recentTransactions"] if event["id"] == ordinaryId)["combinedTransfer"])
            invalidEvent = next(event for event in report["recentTransactions"] if event["id"] == invalidId)
            self.assertFalse(invalidEvent["combinedTransfer"])
            self.assertEqual(-123, invalidEvent["displayAmountCents"])
            self.assertNotIn(self.secret, response.get_data(as_text=True))
            self.assertNotIn("passwordHash", response.get_data(as_text=True))
            self.assertEqual(reference or report, report)
            reference = report
        catalog = self.agent.get("/api/integration/read/accounts", headers=self.headers).get_json()
        archived = next(account for account in catalog if account["id"] == archivedId)
        external = next(account for account in catalog if account["accountClass"] == "system")
        self.assertFalse(archived["ordinaryEligible"])
        self.assertFalse(archived["transferEligible"])
        self.assertFalse(external["ordinaryEligible"])
        self.assertTrue(external["transferEligible"])
        self.assertNotIn("balanceCents", external)
        self.assertEqual(before, self.snapshot())

    def testDashboardEmptyCollectionsRemainReadOnly(self):
        with self.connection:
            self.connection.execute("DELETE FROM accounts WHERE id = ?", (self.cardId,))
        before = self.snapshot()
        for client, path, headers in ((self.owner, "/api/dashboard", {}), (self.auditor, "/api/dashboard", {}), (self.agent, "/api/integration/read/dashboard", self.headers)):
            report = client.get(path, headers=headers).get_json()
            for collection in ("accounts", "recentTransactions", "quickUpdates"):
                self.assertEqual([], report[collection])
        self.assertEqual(before, self.snapshot())

class CategoryRollupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.databasePath = Path(self.directory.name) / "explicit-rollups.db"
        database.initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
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
            otherConnection = connectDatabase(self.databasePath)
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
            otherConnection = connectDatabase(self.databasePath)
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


if __name__ == "__main__":
    unittest.main()
