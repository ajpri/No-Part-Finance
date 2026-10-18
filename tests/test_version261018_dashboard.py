import tests  # Enable the production database guard before application imports.
import tempfile
import unittest
from pathlib import Path

from app import createApp
import database as ledger
from users import createUser
from tests.test_release_2610 import signIn


class DashboardUtilizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.directory.name) / "synthetic-dashboard.db"
        self.backupsPath = Path(self.directory.name) / "backups"
        self.backupsPath.mkdir()
        self.application = createApp(self.databasePath)
        self.connection = ledger.connectDatabase(self.databasePath)
        createUser(self.connection, "owner", "Owner", "owner password")
        createUser(self.connection, "auditor", "Auditor", "auditor password")
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
