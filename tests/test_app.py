from tests.test_support import ownerClient
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import tests  # Activates production-database isolation before importing the application.
from app import createApp, runApplication
from database import connectDatabase, createAccount, createTransaction
from tests.test_support import createScenarioCategories
from tests.test_support import connectFixtureDatabase as connectDatabase


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        databasePath = Path(self.temporaryDirectory.name) / "nested" / "finance.db"
        self.app = createApp(databasePath)
        self.client = ownerClient(self.app)
        self.databasePath = databasePath
        connection = connectDatabase(self.databasePath)
        try:
            createScenarioCategories(connection)
        finally:
            connection.close()

    def tearDown(self):
        self.temporaryDirectory.cleanup()

    def testMissingDatabaseIsCreated(self):
        self.assertTrue(self.databasePath.exists())

    def testEmptyDatabaseGuidesSetupWithoutCreatingCategories(self):
        emptyPath = Path(self.temporaryDirectory.name) / "empty.db"
        emptyClient = ownerClient(createApp(emptyPath))
        self.assertEqual([], emptyClient.get("/api/categories").get_json())
        htmlResponse = emptyClient.get("/")
        javascriptResponse = emptyClient.get("/app.js")
        html = htmlResponse.get_data(as_text=True)
        javascript = javascriptResponse.get_data(as_text=True)
        htmlResponse.close()
        javascriptResponse.close()
        self.assertIn('href="/settings#accounts"', html)
        self.assertIn('href="/settings#categories"', html)
        self.assertIn("Templates, reimbursement, Credit Accounts, Budget, and Simulator are optional", html)
        self.assertIn("Ask the Owner", html)
        self.assertIn("activeAccountsWith().length === 0", javascript)
        self.assertIn("selectableCategoriesWith().length === 0", javascript)
        self.assertNotIn("accounts.length > 0 && categories.length > 0", javascript)

    def testFreshOrdinaryEntryPreservesOpeningBalanceSignsAndRestart(self):
        emptyPath = Path(self.temporaryDirectory.name) / "first-use.db"
        application = createApp(emptyPath)
        client = ownerClient(application)
        accountResponse = client.post("/api/accounts", json={"name": "Example Checking", "accountClass": "cash", "openingBalance": "500.00"})
        self.assertEqual(201, accountResponse.status_code)
        accountId = accountResponse.get_json()["id"]
        report = client.get("/api/dashboard").get_json()
        self.assertEqual(50000, report["accounts"][0]["balanceCents"])
        self.assertEqual([], report["recentTransactions"])
        self.assertEqual(0, report["incomeCents"])
        self.assertEqual(0, report["expensesCents"])
        expenseResponse = client.post("/api/categories", json={"name": "Groceries", "reportingRole": "expense"})
        self.assertEqual(201, expenseResponse.status_code)
        expenseId = expenseResponse.get_json()["id"]
        enteredDate = date.today().isoformat()
        expenseResponse = client.post("/api/transactions", json={"accountId": accountId, "date": enteredDate, "description": "Example food", "categoryId": expenseId, "amount": "12.50"})
        self.assertEqual(201, expenseResponse.status_code)
        incomeResponse = client.post("/api/categories", json={"name": "Paycheck", "reportingRole": "income"})
        self.assertEqual(201, incomeResponse.status_code)
        incomeId = incomeResponse.get_json()["id"]
        incomeResponse = client.post("/api/transactions", json={"accountId": accountId, "date": enteredDate, "description": "Example pay", "categoryId": incomeId, "amount": "100.00"})
        self.assertEqual(201, incomeResponse.status_code)
        restartedClient = ownerClient(createApp(emptyPath))
        report = restartedClient.get("/api/dashboard").get_json()
        self.assertEqual(58750, report["accounts"][0]["balanceCents"])
        self.assertEqual(1250, report["expensesCents"])
        self.assertEqual(10000, report["incomeCents"])
        self.assertEqual(8750, report["netCents"])
        self.assertEqual([], restartedClient.get("/api/templates").get_json())
        connection = connectDatabase(emptyPath)
        try:
            self.assertEqual([-1250, 10000], [row[0] for row in connection.execute("SELECT amountCents FROM transactions ORDER BY id")])
            self.assertEqual(50000, connection.execute("SELECT balanceOffsetCents FROM accounts WHERE id = ?", (accountId,)).fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()

    def testMinimalIndependentConfigurationSupportsAllOrdinaryEntrySigns(self):
        minimalPath = Path(self.temporaryDirectory.name) / "minimal-independent.db"
        application = createApp(minimalPath)
        client = ownerClient(application)
        connection = connectDatabase(minimalPath)
        try:
            def assertOptionalConfigurationAbsent():
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM accounts WHERE accountClass = 'credit'").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transactionTemplates").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM sinkingFundPlans").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM flexibleCategoryBudgets").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM expectedIncomePlans").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM plannedCreditPayments").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM simulatorPlans").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM simulatorRows").fetchone()[0])
                self.assertIsNone(connection.execute("SELECT value FROM applicationSettings WHERE key = 'reimbursementCategoryId'").fetchone())

            assertOptionalConfigurationAbsent()
            self.assertEqual([], client.get("/api/categories").get_json())
            self.assertEqual([], client.get("/api/dashboard").get_json()["recentTransactions"])
            response = client.post("/api/accounts", json={"name": "財布 <fictional>", "accountClass": "cash", "openingBalance": "100.00"})
            self.assertEqual(201, response.status_code)
            accountId = response.get_json()["id"]
            categoryIds = {}
            for role, name in [("expense", "Salary-shaped label"), ("income", "Shopping-shaped label")]:
                response = client.post("/api/categories", json={"name": name, "reportingRole": role})
                self.assertEqual(201, response.status_code)
                categoryIds[role] = response.get_json()["id"]
            expectedLedger = []
            expectedDisplay = []
            for index, (role, amount, ledgerCents, displayCents) in enumerate([
                ("expense", "12.50", -1250, 1250),
                ("expense", "-2.50", 250, -250),
                ("income", "40.00", 4000, 4000),
                ("income", "-5.00", -500, -500),
            ]):
                response = client.post("/api/transactions", json={"accountId": accountId, "date": "2026-10-01", "description": f"Independent ordinary entry {index}", "categoryId": categoryIds[role], "amount": amount})
                self.assertEqual(201, response.status_code, response.get_json())
                expectedLedger.append(ledgerCents)
                expectedDisplay.append(displayCents)
            assertOptionalConfigurationAbsent()
            restarted = ownerClient(createApp(minimalPath))
            report = restarted.get("/api/dashboard?period=custom&startDate=2026-10-01&endDate=2026-10-01").get_json()
            self.assertEqual((1000, 3500, 2500), tuple(report[key] for key in ("expensesCents", "incomeCents", "netCents")))
            self.assertEqual(12500, next(account for account in report["accounts"] if account["id"] == accountId)["balanceCents"])
            historyResponse = restarted.get("/api/transactions?period=all")
            self.assertEqual(200, historyResponse.status_code)
            events = sorted(historyResponse.get_json(), key=lambda event: event["id"])
            self.assertEqual(expectedDisplay, [event["displayAmountCents"] for event in events])
            self.assertEqual(expectedLedger, [row[0] for row in connection.execute("SELECT amountCents FROM transactions ORDER BY id")])
            self.assertEqual(10000, connection.execute("SELECT balanceOffsetCents FROM accounts WHERE id = ?", (accountId,)).fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            assertOptionalConfigurationAbsent()
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()

    def testBasicPagesAndAssetsLoad(self):
        for path in ("/", "/transactions", "/settings", "/styles.css", "/app.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(200, response.status_code)
                response.close()

    def testHealthReportsSchemaVersion(self):
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"applicationVersion": "26.10.30", "schemaVersion": 30, "status": "ok"},
            response.get_json(),
        )

    def testApplicationIdentityIsExposedFromOneVersionValue(self):
        details = self.client.get("/api/application").get_json()
        self.assertEqual({"name": "No Part Finance", "version": "26.10.30"}, details)
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        response.close()
        self.assertIn("<title>No Part Finance</title>", html)
        self.assertIn('id="applicationVersion"', html)

    def testBudgetUsesCompactSortableTableAndEditOnlyDeletion(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-budget-sort="nextDate" data-label="Next Date"', html)
        for sortKey, label in (("fund", "Fund"), ("amount", "Amount"), ("setAside", "Set Aside"), ("targetToday", "Target on Selected Date")):
            self.assertIn(f'data-budget-sort="{sortKey}" data-label="{label}"', html)
        for removedColumn in ('data-label="Frequency"', 'data-label="Payment Account"', 'data-label="Category"'):
            self.assertNotIn(removedColumn, html)
        self.assertNotIn('id="sinkingFundDisplayOrder"', html)
        self.assertIn('urlFilters.get("sort")) ? urlFilters.get("sort") : "nextDate"', javascript)
        self.assertIn('frequency.className = "secondaryText"', javascript)
        self.assertIn('Monthly ${dollars(plan.monthlyContributionCents)}', javascript)
        self.assertIn('Per paycheck ${plan.perPaycheckContributionCents', javascript)
        self.assertIn("actions.append(create, edit);", javascript)
        self.assertNotIn("actions.append(create, edit, remove);", javascript)
        self.assertIn('id="deleteSinkingFundButton"', html)
        self.assertIn('window.confirm(`Permanently delete Budget plan', javascript)

    def testMonthlyRecurringPlansHaveSeparateCompactSortableTable(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        self.assertIn("Monthly Recurring Expenses", html)
        for sortKey, label in (("nextDate", "Next Date"), ("expense", "Expense"), ("amount", "Amount")):
            self.assertIn(f'data-monthly-sort="{sortKey}" data-label="{label}"', html)
        self.assertIn('<option value="monthly">Monthly</option>', html)
        self.assertIn('plan.frequency === "monthly"', javascript)
        self.assertIn('plan.accountName, plan.categoryName', javascript)
        self.assertLess(html.index("Monthly Recurring Expenses"), html.index("<h2>Sinking Funds</h2>"))
        self.assertLess(html.index('id="totalMonthlyRecurring"'), html.index('id="totalReserveTarget"'))

    def testFlexibleMonthlyPlanningLayoutAndExistingWorkflowPrefills(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        self.assertLess(html.index("Monthly Recurring Expenses"), html.index("Flexible Category Budgets"))
        self.assertIn("Monthly Requirement", html)
        self.assertIn("Category</th><th class=\"amount\">Monthly Target</th><th class=\"amount\">Three-Month Suggestion", html)
        self.assertIn('id="suggestionRounding"', html)
        self.assertIn('document.querySelector("#transactionDate").value = localCalendarDate()', javascript)
        self.assertIn('document.querySelector("#transferToAccount").value = String(item.accountId)', javascript)
        self.assertIn('document.querySelector("#transferDate").value = localCalendarDate()', javascript)
        self.assertIn('document.querySelector("#requirementIncome").textContent = dollars(budget.totalExpectedIncomeCents)', javascript)

    def testSmartSuggestionsAndBudgetOrganizationAreExposed(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        for controlId in ("monthlySuggestionWindow", "quarterlySuggestionWindow", "annualSuggestionWindow", "showAllTemplates"):
            self.assertIn(f'id="{controlId}"', html)
        self.assertIn('appendGroup("Suggested", suggested, true)', javascript)
        self.assertIn("/api/budget/template-suggestions?date=", javascript)
        self.assertIn("Coming Up — Next 7 Days", html)
        self.assertIn('id="comingUpRows"', html)
        self.assertIn('create.textContent = "+ Transaction"', javascript)
        self.assertIn('action.textContent = "+ Transfer"', javascript)
        self.assertIn('.positiveAction { background: #34a853;', styles)
        for fragment in ("monthly-recurring", "flexible", "payments-income", "sinking-funds"):
            self.assertIn(f'href="#{fragment}"', html)
        self.assertIn(".monthlyRequirement strong { font-size: 1.75rem", styles)
        self.assertNotIn("Upcoming Annual", html + javascript)

    def testVersionNextBudgetRefinementsAreRendered(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="templatePrefillAmount" type="checkbox" checked', html)
        self.assertIn("template.prefillAmount && template.enteredAmountCents", javascript)
        self.assertEqual(1, html.count('id="newSinkingFundButton"'))
        self.assertNotIn('id="newSinkingFundOnlyButton"', html)
        self.assertIn('id="sinkingFundSubmitButton" type="submit">Create Plan', html)
        self.assertLess(html.index('id="newSinkingFundButton"'), html.index('class="budgetNavigation"'))
        self.assertGreater(html.index('id="newSinkingFundButton"'), html.index('id="budgetSetupMessage"'))

    def testPayoffHelperRequiresEnoughCashInSelectedSourceAccount(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        application = (repositoryPath / "app.py").read_text(encoding="utf-8")
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        self.assertIn(
            'source["balanceCents"] >= amountCents',
            application,
        )
        overviewLabels = ["Monthly Recurring Expenses", "Flexible Budget Categories", "Monthly Sinking-Fund Contributions", "Planned Debt Payments", "Expected Monthly Income"]
        overview = html[html.index('class="budgetOverview"'):html.index("Coming Up — Next 7 Days")]
        self.assertEqual(sorted(overview.index(label) for label in overviewLabels), [overview.index(label) for label in overviewLabels])
        navigation = html[html.index('class="budgetNavigation"'):html.index('id="budgetMonthlyRecurring"')]
        self.assertLess(navigation.index("#sinking-funds"), navigation.index("#payments-income"))

    def testSimulatorResultsUseResponsiveSummaryAndBalancesRow(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        layout = html[html.index('class="simulatorResultsLayout"'):html.index('id="simulatorResultRows"')]
        self.assertLess(layout.index("<h2>Results</h2>"), layout.index("<h3>Projected Account Balances</h3>"))
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(18rem, 1fr)", styles)
        self.assertIn("@media (max-width: 75rem) { .simulatorResultsLayout { grid-template-columns: minmax(0, 1fr); } }", styles)

    def testSimulatorCanExcludeBuffersFromDisplayedCashProjections(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        checkbox = '<input id="excludeBufferFromProjectedBalances" type="checkbox"> Exclude buffer from projected balances'
        self.assertIn(checkbox, html)
        self.assertLess(html.index('id="simulatorResultRows"'), html.index(checkbox))
        self.assertIn('excludeBuffer&&a.accountClass==="cash"?a.projectedBalanceCents-a.bufferCents:a.projectedBalanceCents', javascript)
        self.assertIn('if(a.minimumStatus)name.append(" ",statusBadge(a.minimumStatus))', javascript)
        self.assertIn('#excludeBufferFromProjectedBalances").addEventListener("change",renderSimulatorResults)', javascript)
        self.assertIn(".simulatorBufferProjectionOption { align-items: flex-start; margin-top: .5rem; max-width: 100%; width: fit-content; }", styles)

    def testSinkingFundDateRefreshWaitsForFocusOrPointerLeave(self):
        javascript = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertIn('addEventListener("blur",refreshSinkingFundDate)', javascript)
        self.assertNotIn('addEventListener("mouseleave",refreshSinkingFundDate)', javascript)
        self.assertIn('event.key==="Enter"', javascript)
        self.assertIn('#sinkingFundAsOfDate").addEventListener("change",refreshSinkingFundDate)', javascript)
        self.assertNotIn('#sinkingFundAsOfDate").addEventListener("change",loadBudget)', javascript)
        self.assertIn("value!==loadedSinkingFundDate", javascript)

    def testSettingsAccountsIncludesCompactMinimumColumn(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        accountSection = html[html.index('id="settingsAccounts"'):html.index('id="settingsCategories"')]
        self.assertIn("Cash Accounts", accountSection)
        self.assertIn("Credit Cards and Loans", accountSection)
        self.assertNotIn("Sort Order", accountSection)
        self.assertEqual(1, html.count('id="settingsCashAccountRows"'))
        self.assertEqual(1, html.count('id="settingsCreditAccountRows"'))
        self.assertIn('document.querySelector("#settingsCashAccountRows")', javascript)
        self.assertIn('document.querySelector("#settingsCreditAccountRows")', javascript)
        self.assertNotIn("Display Order", html[html.index('id="accountDialog"'):html.index('id="categoryDialog"')])
        self.assertIn('account.minimumBalanceCents !== null', javascript)
        self.assertIn('.minimumColumn { width: 1%; }', styles)

    def testAccountsUseAuthoritativeAlphabeticalClassGroupsAndPreserveStoredOrder(self):
        connection = connectDatabase(self.databasePath)
        try:
            zCashId = createAccount(connection, "z Cash", "cash", "checking", displayOrder=-20)
            alphaCashId = createAccount(connection, "Alpha cash", "cash", "savings", displayOrder=90)
            alphaCreditId = createAccount(connection, "alpha Card", "credit", "Credit Card", displayOrder=-50)
            betaCreditId = createAccount(connection, "Beta Loan", "credit", "Personal Loan", displayOrder=1)
        finally:
            connection.close()
        accounts = self.client.get("/api/accounts").get_json()
        self.assertEqual([alphaCashId, zCashId, alphaCreditId, betaCreditId], [account["id"] for account in accounts if account["accountClass"] != "system"])
        dashboardAccounts = self.client.get("/api/dashboard").get_json()["accounts"]
        self.assertEqual(
            [alphaCashId, zCashId, alphaCreditId, betaCreditId],
            [account["id"] for account in dashboardAccounts],
        )
        response = self.client.patch(
            f"/api/accounts/{alphaCashId}",
            json={"name": "Alpha renamed", "accountClass": "cash", "accountSubtype": "savings", "active": True,
                  "creditLimit": "", "utilizationWarning": "", "minimumBalance": "", "minimumCautionPercent": "25"},
        )
        self.assertEqual(200, response.status_code)
        connection = connectDatabase(self.databasePath)
        try:
            storedOrder = connection.execute("SELECT displayOrder FROM accounts WHERE id = ?", (alphaCashId,)).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(90, storedOrder)

    def testAccountSelectorsUseNativeClassGroupsWithoutRedundantSingleClassGroup(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        self.assertIn('document.createElement("optgroup")', javascript)
        self.assertIn('[["pinned", "Pinned"], ["cash", "Cash"], ["credit", "Credit Cards and Loans"], ["system", "System"]]', javascript)
        self.assertIn('const grouped = accountClasses.size > 1', javascript)
        self.assertIn('const parent = grouped ? document.createElement("optgroup") : select', javascript)
        for selector in ("accountFilter", "transactionAccount", "transferFromAccount", "templateAccount", "plannedPaymentAccount"):
            self.assertIn(f'setAccountOptions(document.querySelector("#{selector}")', javascript)
        self.assertIn('setAccountOptions(accountSelect, activeAccountsWith()', javascript)
        self.assertIn('simulatorAccountSelect(allowed', javascript)

    def testMinimumHelperKeepsStableSpaceAndIgnoresStaleResponses(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        self.assertIn("Leave blank or enter $0 to clear.", html)
        self.assertIn("requestId!==minimumHelperRequest", javascript)
        self.assertIn("helper.replaceChildren(...lines)", javascript)
        self.assertIn("#minimumBalanceHelper { min-height: 3.75rem; }", styles)

    def testSimulatorRowEditExplainsExecutionAndUsesContextualDeletion(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="deleteSimulatorRowButton" class="destructiveButton" type="button">Delete Row</button>', html)
        self.assertIn("An executed row represents activity that has already happened", html)
        self.assertNotIn('id="simulatorExecutionMessage"', html)
        self.assertIn('form.dataset.hasLinkedLedger=row.executedTransactionId?"true":"false"', javascript)
        self.assertIn('if(form.dataset.hasLinkedLedger==="true")message+=', javascript)

    def testSimulatorTransferDropdownUsesShortUiLabel(self):
        javascript = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertIn('{id:"transfer",name:"Transfer",active:true}', javascript)
        self.assertNotIn('{id:"transfer",name:"Transfer / Payment",active:true}', javascript)

    def testTemplatePrefillAmountPersistsThroughApi(self):
        connection = connectDatabase(self.databasePath)
        try:
            accountId = createAccount(connection, "Template account", "cash")
            categoryId = connection.execute("SELECT id FROM categories WHERE name = ?", ("Wants",)).fetchone()[0]
        finally:
            connection.close()
        response = self.client.post("/api/templates", json={"name": "No amount prefill", "accountId": accountId, "description": "Variable bill", "categoryId": categoryId, "amount": "25.00", "prefillAmount": False})
        self.assertEqual(201, response.status_code)
        template = next(item for item in self.client.get("/api/templates").get_json() if item["name"] == "No amount prefill")
        self.assertEqual(2500, template["enteredAmountCents"])
        self.assertEqual(0, template["prefillAmount"])

    def testBudgetSuggestionSettingsDefaultsAndValidation(self):
        settings = self.client.get("/api/settings").get_json()
        self.assertEqual("3", settings["monthlySuggestionWindowDays"])
        self.assertEqual("1", settings["quarterlySuggestionWindowMonths"])
        self.assertEqual("3", settings["annualSuggestionWindowMonths"])
        invalid = self.client.put("/api/settings/budget-suggestion-windows", json={"monthlyDays": -1, "quarterlyMonths": 1, "annualMonths": 3})
        self.assertEqual(400, invalid.status_code)

    def testConfigurationDeletesUseEditModalAndSolidRedStyle(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        javascript = (repositoryPath / "app.js").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        for buttonId in ("deleteCategoryButton", "deleteTemplateButton", "deleteSinkingFundButton"):
            self.assertIn(f'id="{buttonId}" class="destructiveButton" type="button" hidden', html)
        self.assertNotIn("actions.append(edit, remove)", javascript)
        self.assertIn('id="deleteTransactionButton" class="destructiveButton', html)
        self.assertIn('id="deleteTransferButton" class="destructiveButton', html)
        self.assertIn(".destructiveButton { background: #b3261e;", styles)

    def testDatabaseStatusCountsLedgerRowsAndAllAccounts(self):
        connection = connectDatabase(self.databasePath)
        try:
            firstAccountId = createAccount(connection, "Active", "cash")
            secondAccountId = createAccount(connection, "Archived", "cash")
            wantsId = connection.execute("SELECT id FROM categories WHERE name = ?", ("Wants",)).fetchone()[0]
            createTransaction(connection, firstAccountId, "2026-08-23", "Expense", wantsId, "10.00")
        finally:
            connection.close()
        transfer = self.client.post("/api/transfers", json={"date": "2026-08-23", "fromAccountId": firstAccountId, "toAccountId": secondAccountId, "amount": "5.00", "description": "Transfer"})
        self.assertEqual(201, transfer.status_code)
        connection = connectDatabase(self.databasePath)
        try:
            connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (secondAccountId,))
            connection.commit()
        finally:
            connection.close()
        modifiedBefore = self.databasePath.stat().st_mtime_ns
        status = self.client.get("/api/system/database-status").get_json()
        modifiedAfter = self.databasePath.stat().st_mtime_ns
        self.assertEqual(3, status["transactionCount"])
        self.assertEqual(3, status["accountCount"])
        self.assertEqual(30, status["schemaVersion"])
        self.assertRegex(status["modifiedLocal"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.assertEqual(modifiedBefore, modifiedAfter)

    def testReleaseHistoryAndPlatformLaunchersAreIncluded(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        html = (repositoryPath / "index.html").read_text(encoding="utf-8")
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        self.assertIn("Release History", html)
        self.assertIn("<h3>26.10</h3>", html)
        self.assertIn("<h3>Earlier versions</h3>", html)
        self.assertNotIn("<h3>26.09</h3>", html)
        self.assertNotIn("<h3>26.08 — Initial Release</h3>", html)
        self.assertNotIn("<h3>26.09.3</h3>", html)
        self.assertIn('href="/api/package-no-db" download>Download application only</a>', html)
        self.assertIn("Download with database includes this ledger's financial history and credential-free user records.", html)
        self.assertIn(".packageActions { align-items: flex-start; display: flex; flex-direction: column; font-weight: 400;", styles)
        backupSection = html[html.index("<h3>Database Backup</h3>"):html.index("<h3>Application Package</h3>")]
        self.assertLess(backupSection.index("Create a consistent SQLite backup"), backupSection.index("Download Backup"))
        packageSection = html[html.index("<h3>Application Package</h3>"):html.index("</section>", html.index("<h3>Application Package</h3>"))]
        self.assertLess(packageSection.index("credential-free user records"), packageSection.index(">Download with database<"))
        self.assertNotIn("26.08.24 —", html)
        self.assertIn("The inspiration behind No Part Finance.", html)
        windowsLauncher = (repositoryPath / "startWindows.bat").read_text(encoding="utf-8")
        linuxLauncher = (repositoryPath / "startLinux.sh").read_text(encoding="utf-8")
        for launcher in (windowsLauncher, linuxLauncher):
            self.assertIn("requirements.txt", launcher)
            self.assertIn("app.py", launcher)
            self.assertNotIn("finance.db", launcher)
        self.assertIn(".venv\\Scripts\\python.exe", windowsLauncher)
        self.assertIn(".venv/bin/python", linuxLauncher)

    def testSolidUtilizationBadges(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".utilizationGreen { background:", styles)
        self.assertIn(".utilizationYellow { background:", styles)
        self.assertIn(".utilizationRed { background:", styles)
        self.assertIn("color: #fff", styles)

    def testSettingsNavigationWrapsOnNarrowScreens(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        styles = (repositoryPath / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".settingsNavigation { flex-wrap: wrap; }", styles)
        self.assertIn(".settingsNavigation a { flex: none; white-space: nowrap; }", styles)

    def testDevelopmentServerBindsToAllInterfacesByDefault(self):
        with patch("app.make_server") as createServer, patch("users.input") as setupInput:
            runApplication(self.app)
        createServer.assert_called_once_with("0.0.0.0", 5000, self.app, threaded=True)
        setupInput.assert_not_called()
        applicationSource = (Path(__file__).resolve().parents[1] / "app.py").read_text()
        self.assertNotIn("FINANCE_HOST", applicationSource)

    def testThisMonthIncludesFutureDatesAndSortingIsDeterministic(self):
        connection = connectDatabase(self.databasePath)
        try:
            accountId = createAccount(connection, "Checking", "cash", "checking")
            wantsId = connection.execute("SELECT id FROM categories WHERE name = 'Wants'").fetchone()[0]
            monthEnd = date.today().replace(day=__import__("calendar").monthrange(date.today().year, date.today().month)[1]).isoformat()
            createTransaction(connection, accountId, monthEnd, "zeta", wantsId, "2.00")
            createTransaction(connection, accountId, date.today().isoformat(), "Alpha", wantsId, "1.00")
        finally:
            connection.close()
        thisMonth = self.client.get("/api/transactions?period=thisMonth").get_json()
        self.assertIn(monthEnd, [row["date"] for row in thisMonth])
        ascending = self.client.get("/api/transactions?period=all&sort=description&direction=asc").get_json()
        self.assertEqual(["Alpha", "zeta"], [row["description"] for row in ascending])
        descending = self.client.get("/api/transactions?period=all&sort=amount&direction=desc").get_json()
        self.assertEqual([-200, -100], [row["amountCents"] for row in descending])

    def testNewRouteAndTransactionTypeFilter(self):
        newResponse = self.client.get("/new")
        self.assertEqual(200, newResponse.status_code)
        newResponse.close()
        connection = connectDatabase(self.databasePath)
        try:
            cash = createAccount(connection, "Cash", "cash")
            savings = createAccount(connection, "Savings", "cash")
            wants = connection.execute("SELECT id FROM categories WHERE name = 'Wants'").fetchone()[0]
            createTransaction(connection, cash, "2026-08-01", "Expense", wants, "1.00")
            from database import createTransfer
            createTransfer(connection, "2026-08-02", cash, savings, "2.00", "Move")
        finally:
            connection.close()
        ordinary = self.client.get("/api/transactions?period=all&type=transactions").get_json()
        transfers = self.client.get("/api/transactions?period=all&type=transfers").get_json()
        self.assertEqual([None], [row["transferId"] for row in ordinary])
        self.assertEqual(1, len(transfers))
        self.assertTrue(all(row["transferId"] for row in transfers))


if __name__ == "__main__":
    unittest.main()
