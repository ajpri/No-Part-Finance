from tests.test_support import ownerClient
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests  # Activates production-database isolation before importing the application.
from app import createApp
from database import (
    ValidationError,
    connectDatabase,
    createAccount,
    createCategory,
    createSinkingFundPlan,
    createTemplate,
    initializeDatabase,
    minimumStatus,
    sinkingFundSummary,
    updateAccount,
)
from tests.test_support import connectFixtureDatabase as connectDatabase


class Release26093Tests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.temporaryDirectory.name) / "finance.db"
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)

    def tearDown(self):
        self.connection.close()
        self.temporaryDirectory.cleanup()

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


if __name__ == "__main__":
    unittest.main()
