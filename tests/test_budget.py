"""Budget calculations and Simulator allocation and utilization."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import initializeTestDatabase
import tempfile
import unittest
from datetime import date
from pathlib import Path
from database import ValidationError, budgetTemplateSuggestions, createAccount, createCategory, createExpectedIncomePlan, createFlexibleCategoryBudget, createPlannedCreditPayment, createSinkingFundPlan, createTemplate, createTransaction, deleteTemplate, setPaychecksPerYear, setSuggestionRounding, setBudgetSuggestionWindows, sinkingFundOccurrenceRange, sinkingFundSummary, updateTemplate
from tests.test_support import createScenarioCategories
import database
from tests.test_support import connectFixtureDatabase as connectDatabase

class BudgetPhaseOneTests(unittest.TestCase):
    def setUp(self):
        initializeTestDatabase(self, "budget.db")
        self.categoryIds = createScenarioCategories(self.connection)
        self.accountId = createAccount(self.connection, "Checking", "cash")
        self.templateId = createTemplate(
            self.connection, "Insurance", self.accountId, "Insurance bill",
            self.categoryIds["Wants"], "1200.00", 1,
        )

    def testPlanningTemplateCompletenessRetainsRoleRulesAndErrors(self):
        cases = [({}, True), ({"accountId": None}, True), ({"active": 0}, False),
                 ({"description": None}, False), ({"categoryId": None}, False),
                 ({"enteredAmountCents": None}, False), ({"enteredAmountCents": -100}, False)]
        for role, categoryId, validator, message in (
            ("expense", self.categoryIds["Wants"], database.validateCompleteExpenseTemplate, "Sinking Fund plans require a complete active Expense template with a positive amount"),
            ("income", self.categoryIds["Income/Reimbursement"], database.validateCompleteIncomeTemplate, "Expected income plans require a complete active Income template with a positive amount"),
        ):
            templateId = createTemplate(self.connection, f"Completeness {role}", self.accountId, "Explicit label", categoryId, "1.00")
            for changes, valid in cases:
                with self.subTest(role=role, changes=changes):
                    with self.connection:
                        self.connection.execute("UPDATE transactionTemplates SET active = 1, description = 'Explicit label', accountId = ?, categoryId = ?, enteredAmountCents = 100 WHERE id = ?", (self.accountId, categoryId, templateId))
                        for field, value in changes.items():
                            self.connection.execute(f"UPDATE transactionTemplates SET {field} = ? WHERE id = ?", (value, templateId))
                    before = list(self.connection.iterdump())
                    if valid:
                        self.assertEqual(templateId, validator(self.connection, templateId)["id"])
                    else:
                        with self.assertRaises(ValidationError) as raised:
                            validator(self.connection, templateId)
                        self.assertEqual(message, str(raised.exception))
                    self.assertEqual(before, list(self.connection.iterdump()))
            with self.connection:
                self.connection.execute("UPDATE transactionTemplates SET active=1, description='Explicit label', accountId=?, categoryId=?, enteredAmountCents=100 WHERE id=?", (self.accountId, categoryId, templateId))
            for table, identifier in (("accounts", self.accountId), ("categories", categoryId)):
                with self.connection:
                    self.connection.execute(f"UPDATE {table} SET active = 0 WHERE id = ?", (identifier,))
                with self.assertRaises(ValidationError) as raised:
                    validator(self.connection, templateId)
                self.assertEqual(message, str(raised.exception))
                with self.connection:
                    self.connection.execute(f"UPDATE {table} SET active = 1 WHERE id = ?", (identifier,))
            otherValidator = database.validateCompleteIncomeTemplate if role == "expense" else database.validateCompleteExpenseTemplate
            with self.assertRaises(ValidationError):
                otherValidator(self.connection, templateId)
        with self.assertRaises(ValidationError) as raised:
            database.validateCompleteExpenseTemplate(self.connection, -1)
        self.assertEqual("Template does not exist", str(raised.exception))
        with self.assertRaises(ValidationError) as raised:
            database.validateCompleteIncomeTemplate(self.connection, -1)
        self.assertEqual("Expected income plans require a complete active Income template with a positive amount", str(raised.exception))

    def testQuarterlyAndYearlyIntegerCentCalculations(self):
        setPaychecksPerYear(self.connection, "26")
        createSinkingFundPlan(self.connection, self.templateId, "quarterly", "2026-10-01")
        yearlyTemplateId = createTemplate(
            self.connection, "Registration", self.accountId, "Registration",
            self.categoryIds["Wants"], "100.01", 2,
        )
        createSinkingFundPlan(self.connection, yearlyTemplateId, "yearly", "2027-02-28")
        summary = sinkingFundSummary(self.connection, today=date(2026, 8, 23))
        quarterly, yearly = summary["plans"]
        self.assertEqual(40000, quarterly["monthlyContributionCents"])
        self.assertEqual(18462, quarterly["perPaycheckContributionCents"])
        self.assertEqual(834, yearly["monthlyContributionCents"])
        self.assertEqual(385, yearly["perPaycheckContributionCents"])
        self.assertEqual(
            sum(plan["reserveTargetCents"] for plan in summary["plans"]),
            summary["totalReserveTargetCents"],
        )

    def testExpectedDateClampingAndFullTargetOnOccurrence(self):
        self.assertEqual(
            (date(2024, 2, 29), date(2025, 2, 28)),
            sinkingFundOccurrenceRange("2024-02-29", "yearly", date(2025, 2, 28)),
        )
        setPaychecksPerYear(self.connection, "26")
        createSinkingFundPlan(self.connection, self.templateId, "yearly", "2024-02-29")
        plan = sinkingFundSummary(self.connection, today=date(2025, 2, 28))["plans"][0]
        self.assertEqual(plan["enteredAmountCents"], plan["reserveTargetCents"])

    def testMonthlyRecurrenceClampsAndHasSeparateSubtotal(self):
        createSinkingFundPlan(self.connection, self.templateId, "monthly", "2026-01-31")
        self.assertEqual(
            (date(2026, 1, 31), date(2026, 2, 28)),
            sinkingFundOccurrenceRange("2026-01-31", "monthly", date(2026, 2, 28)),
        )
        summary = sinkingFundSummary(self.connection, today=date(2026, 2, 28))
        plan = summary["plans"][0]
        self.assertEqual("2026-02-28", plan["upcomingExpectedDate"])
        self.assertIsNone(plan["monthlyContributionCents"])
        self.assertIsNone(plan["perPaycheckContributionCents"])
        self.assertIsNone(plan["reserveTargetCents"])
        self.assertEqual(120000, summary["totalMonthlyRecurringCents"])
        self.assertEqual(0, summary["totalMonthlyContributionCents"])

    def testMonthlyRecurringGroupsAreAuthoritativeAndExcludeInactivePlans(self):
        earlierAccountId = createAccount(self.connection, "Earlier", "cash", displayOrder=-1)
        earlierTemplateId = createTemplate(
            self.connection, "Child expense", earlierAccountId, "Child expense",
            self.categoryIds["Recurring Needs"], "25.00", 2,
        )
        unassignedTemplateId = createTemplate(
            self.connection, "Unassigned expense", None, "Unassigned expense",
            self.categoryIds["Wants"], "10.00", 3,
        )
        creditAccountId = createAccount(self.connection, "Card", "credit", "Credit Card", displayOrder=5)
        creditTemplateId = createTemplate(
            self.connection, "Card expense", creditAccountId, "Card expense",
            self.categoryIds["Wants"], "5.00", 4,
        )
        inactiveTemplateId = createTemplate(
            self.connection, "Inactive expense", self.accountId, "Inactive expense",
            self.categoryIds["Wants"], "99.00", 4,
        )
        createSinkingFundPlan(self.connection, self.templateId, "monthly", "2026-08-01")
        createSinkingFundPlan(self.connection, earlierTemplateId, "monthly", "2026-08-02")
        createSinkingFundPlan(self.connection, unassignedTemplateId, "monthly", "2026-08-03")
        createSinkingFundPlan(self.connection, creditTemplateId, "monthly", "2026-08-03")
        inactivePlanId = createSinkingFundPlan(self.connection, inactiveTemplateId, "monthly", "2026-08-04")
        self.connection.execute("UPDATE sinkingFundPlans SET active = 0 WHERE id = ?", (inactivePlanId,))
        self.connection.commit()

        summary = sinkingFundSummary(self.connection, today=date(2026, 8, 23))
        accounts = summary["monthlyRecurringByPaymentAccount"]
        self.assertEqual(["Checking", "Earlier", "Card", "Unassigned"], [item["accountName"] for item in accounts])
        self.assertEqual([120000, 2500, 500, 1000], [item["amountCents"] for item in accounts])
        self.assertEqual(
            [item["amountCents"] for item in accounts],
            [sum(detail["amountCents"] for detail in item["items"]) for item in accounts],
        )
        categories = summary["monthlyRecurringByCategory"]
        self.assertEqual(["Recurring Needs", "Wants"], [item["categoryName"] for item in categories])
        self.assertEqual("Needs", categories[0]["parentCategoryName"])
        self.assertEqual([2500, 121500], [item["amountCents"] for item in categories])
        self.assertEqual(
            [item["amountCents"] for item in categories],
            [sum(detail["amountCents"] for detail in item["items"]) for item in categories],
        )
        self.assertEqual(summary["totalMonthlyRecurringCents"], sum(item["amountCents"] for item in accounts))
        self.assertEqual(summary["totalMonthlyRecurringCents"], sum(item["amountCents"] for item in categories))

    def testPlanRequiresCompleteExpenseTemplateAndProtectsIt(self):
        setPaychecksPerYear(self.connection, "26")
        partialTemplateId = createTemplate(self.connection, "Partial", None, None, None, None)
        with self.assertRaisesRegex(ValidationError, "complete active Expense template"):
            createSinkingFundPlan(self.connection, partialTemplateId, "yearly", "2027-01-01")
        createSinkingFundPlan(self.connection, self.templateId, "yearly", "2027-01-01")
        with self.assertRaisesRegex(ValidationError, "active Sinking Fund plan"):
            deleteTemplate(self.connection, self.templateId)
        with self.assertRaisesRegex(ValidationError, "active Sinking Fund plan"):
            updateTemplate(self.connection, self.templateId, "Insurance", None, None, None, None, 1, True)

    def testEligibleTemplateMayOmitPaymentAccount(self):
        setPaychecksPerYear(self.connection, "26")
        templateId = createTemplate(
            self.connection, "Account chosen later", None, "Annual fee",
            self.categoryIds["Wants"], "75.00", 2,
        )
        createSinkingFundPlan(self.connection, templateId, "yearly", "2027-08-01")
        plan = sinkingFundSummary(self.connection, today=date(2026, 8, 23))["plans"][0]
        self.assertIsNone(plan["accountId"])
        self.assertIsNone(plan["accountName"])

    def testSpecifiedPaymentAccountMustRemainActive(self):
        setPaychecksPerYear(self.connection, "26")
        self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.accountId,))
        self.connection.commit()
        with self.assertRaisesRegex(ValidationError, "complete active Expense template"):
            createSinkingFundPlan(self.connection, self.templateId, "yearly", "2027-08-01")

    def testPaychecksPerYearMustBePositiveWholeNumber(self):
        for invalidValue in (None, "", "0", "2.5", "-1"):
            with self.subTest(invalidValue=invalidValue):
                with self.assertRaisesRegex(ValidationError, "positive whole number"):
                    setPaychecksPerYear(self.connection, invalidValue)

    def testFlexibleTargetsSuggestionsAndMonthlyRequirement(self):
        createTransaction(self.connection, self.accountId, "2026-05-10", "May", self.categoryIds["Wants"], "100.00")
        createTransaction(self.connection, self.accountId, "2026-06-10", "June", self.categoryIds["Wants"], "50.00")
        createTransaction(self.connection, self.accountId, "2026-06-11", "Refund", self.categoryIds["Wants"], "-20.00")
        createTransaction(self.connection, self.accountId, "2026-08-01", "Current month excluded", self.categoryIds["Wants"], "900.00")
        setSuggestionRounding(self.connection, "25.00")
        createFlexibleCategoryBudget(self.connection, self.categoryIds["Wants"], "200.00")
        createSinkingFundPlan(self.connection, self.templateId, "monthly", "2026-08-31")
        setPaychecksPerYear(self.connection, "26")
        annualTemplateId = createTemplate(self.connection, "Annual", None, "Annual", self.categoryIds["Wants"], "120.00")
        createSinkingFundPlan(self.connection, annualTemplateId, "yearly", "2027-01-01")
        incomeCategoryId = createCategory(self.connection, "Expected Income", reportingRole="income")
        incomeTemplateId = createTemplate(self.connection, "Partner reimbursement", None, "Partner share", incomeCategoryId, "300.00")
        createExpectedIncomePlan(self.connection, incomeTemplateId, "2026-08-15")
        creditId = createAccount(self.connection, "Card", "credit", "Credit Card")
        createPlannedCreditPayment(self.connection, "2026-08-20", creditId, "Card payment", "75.00")

        summary = sinkingFundSummary(self.connection, today=date(2026, 8, 23))
        wants = next(item for item in summary["flexibleCategories"] if item["id"] == self.categoryIds["Wants"])
        self.assertEqual(5000, wants["suggestionCents"])
        self.assertEqual(1, wants["active"])
        self.assertEqual(120000 + 20000 + 7500 + 1000 - 30000, summary["monthlyRequirementCents"])
        self.assertEqual(30000, summary["totalExpectedIncomeCents"])

    def testFlexibleBudgetsRejectRollupsAndIncomeCategories(self):
        incomeId = createCategory(self.connection, "Income Two", reportingRole="income")
        with self.assertRaisesRegex(ValidationError, "selectable Expense"):
            createFlexibleCategoryBudget(self.connection, incomeId, "10.00")
        with self.assertRaisesRegex(ValidationError, "greater than zero"):
            createFlexibleCategoryBudget(self.connection, self.categoryIds["Wants"], "0")

    def testExpectedIncomeAllowsMissingAccountAndPaymentRequiresDebtAccount(self):
        incomeId = createCategory(self.connection, "Known Income", reportingRole="income")
        templateId = createTemplate(self.connection, "Known", None, "Known income", incomeId, "25.00")
        createExpectedIncomePlan(self.connection, templateId, "2026-08-31")
        summary = sinkingFundSummary(self.connection, today=date(2026, 8, 23))
        self.assertIsNone(summary["expectedIncomePlans"][0]["accountId"])
        with self.assertRaisesRegex(ValidationError, "Credit Card or Personal Loan"):
            createPlannedCreditPayment(self.connection, "2026-08-31", self.accountId, "Not debt", "10.00")

    def testSmartTemplateSuggestionsUseConfiguredWindowsAndSelectedDate(self):
        monthlyId = createTemplate(self.connection, "Monthly", None, "Monthly", self.categoryIds["Wants"], "10.00")
        quarterlyId = createTemplate(self.connection, "Quarterly", None, "Quarterly", self.categoryIds["Wants"], "20.00")
        annualId = createTemplate(self.connection, "Annual", None, "Annual", self.categoryIds["Wants"], "30.00")
        createSinkingFundPlan(self.connection, monthlyId, "monthly", "2026-01-31")
        setPaychecksPerYear(self.connection, "26")
        createSinkingFundPlan(self.connection, quarterlyId, "quarterly", "2026-01-15")
        createSinkingFundPlan(self.connection, annualId, "yearly", "2026-12-15")
        setBudgetSuggestionWindows(self.connection, "0", "1", "3")

        february = budgetTemplateSuggestions(self.connection, "2026-02-28")
        self.assertIn(monthlyId, february["suggestedTemplateIds"])
        self.assertIn(quarterlyId, february["suggestedTemplateIds"])
        self.assertEqual("Monthly", february["suggestionReasons"][str(monthlyId)])
        self.assertEqual("Quarterly", february["suggestionReasons"][str(quarterlyId)])
        october = budgetTemplateSuggestions(self.connection, "2026-10-01")
        self.assertIn(annualId, october["suggestedTemplateIds"])
        self.assertEqual("Yearly", october["suggestionReasons"][str(annualId)])

    def testExpectedIncomeUsesMonthlyWindowAndInactivePlansDoNotLinkTemplates(self):
        incomeId = createCategory(self.connection, "Scheduled Income", reportingRole="income")
        incomeTemplateId = createTemplate(self.connection, "Income plan", None, "Income", incomeId, "40.00")
        planId = createExpectedIncomePlan(self.connection, incomeTemplateId, "2026-08-25")
        suggested = budgetTemplateSuggestions(self.connection, "2026-08-23")
        self.assertIn(incomeTemplateId, suggested["suggestedTemplateIds"])
        self.assertEqual("Expected income", suggested["suggestionReasons"][str(incomeTemplateId)])
        self.connection.execute("UPDATE expectedIncomePlans SET active = 0 WHERE id = ?", (planId,))
        self.connection.commit()
        inactive = budgetTemplateSuggestions(self.connection, "2026-08-23")
        self.assertNotIn(incomeTemplateId, inactive["linkedTemplateIds"])

    def testSuggestionWindowSettingsValidateAndDefault(self):
        defaults = budgetTemplateSuggestions(self.connection, "2026-08-23")
        self.assertEqual((3, 1, 3), (defaults["monthlyDays"], defaults["quarterlyMonths"], defaults["annualMonths"]))
        for invalidValue in (None, "", "-1", "1.5"):
            with self.subTest(invalidValue=invalidValue):
                with self.assertRaisesRegex(ValidationError, "nonnegative whole numbers"):
                    setBudgetSuggestionWindows(self.connection, invalidValue, "1", "3")

    def testComingUpIncludesOnlyActiveDatedPlansWithinSevenDays(self):
        createSinkingFundPlan(self.connection, self.templateId, "monthly", "2026-08-30")
        createFlexibleCategoryBudget(self.connection, self.categoryIds["Wants"], "200.00")
        creditId = createAccount(self.connection, "Outside window card", "credit", "Credit Card")
        createPlannedCreditPayment(self.connection, "2026-08-31", creditId, "Eight days away", "10.00")
        summary = sinkingFundSummary(self.connection, today=date(2026, 8, 23))
        self.assertEqual(1, len(summary["comingUp"]))
        self.assertEqual(("2026-08-30", "Monthly Recurring"), (summary["comingUp"][0]["date"], summary["comingUp"][0]["type"]))

    def testAnchoredRecurrenceResetsTargetsAndSuggestionWindowsAcrossYearEnd(self):
        for selectedDate, expected in (
            (date(2024, 2, 29), (date(2024, 1, 31), date(2024, 2, 29))),
            (date(2024, 3, 1), (date(2024, 2, 29), date(2024, 3, 31))),
            (date(2023, 12, 31), (date(2023, 11, 30), date(2023, 12, 31))),
        ):
            with self.subTest(selectedDate=selectedDate):
                self.assertEqual(expected, sinkingFundOccurrenceRange("2024-01-31", "monthly", selectedDate))
        setPaychecksPerYear(self.connection, "26")
        quarterId = createSinkingFundPlan(self.connection, self.templateId, "quarterly", "2024-01-31")
        annualTemplate = createTemplate(self.connection, "Leap fee", None, "Leap fee", self.categoryIds["Wants"], "100.01")
        annualId = createSinkingFundPlan(self.connection, annualTemplate, "yearly", "2024-02-29")
        due = {row["id"]: row for row in sinkingFundSummary(self.connection, date(2025, 2, 28))["plans"]}
        following = {row["id"]: row for row in sinkingFundSummary(self.connection, date(2025, 3, 1))["plans"]}
        self.assertEqual(10001, due[annualId]["reserveTargetCents"])
        self.assertEqual(("2026-02-28", 28), (following[annualId]["upcomingExpectedDate"], following[annualId]["reserveTargetCents"]))
        quarterDue = next(row for row in sinkingFundSummary(self.connection, date(2024, 4, 30))["plans"] if row["id"] == quarterId)
        quarterAfter = next(row for row in sinkingFundSummary(self.connection, date(2024, 5, 1))["plans"] if row["id"] == quarterId)
        self.assertEqual(120000, quarterDue["reserveTargetCents"])
        self.assertEqual(("2024-07-31", 1305), (quarterAfter["upcomingExpectedDate"], quarterAfter["reserveTargetCents"]))
        monthlyTemplate = createTemplate(self.connection, "January bill", None, "January bill", self.categoryIds["Wants"], "1.00")
        createSinkingFundPlan(self.connection, monthlyTemplate, "monthly", "2026-01-01")
        for selectedDate, included in (("2025-12-29", True), ("2025-12-28", False), ("2026-01-04", True), ("2026-01-05", False)):
            with self.subTest(windowDate=selectedDate):
                self.assertEqual(included, monthlyTemplate in budgetTemplateSuggestions(self.connection, selectedDate)["suggestedTemplateIds"])

    def testJanuaryFlexibleSuggestionsIncludeRefundsAndZeroMonthsWithoutChangingTargets(self):
        for transactionDate, category, amount in (
            ("2025-09-30", "Wants", "900.00"),
            ("2025-10-01", "Wants", "100.01"),
            ("2025-12-31", "Wants", "-20.00"),
            ("2026-01-01", "Wants", "900.00"),
            ("2025-12-15", "Giving", "-3.00"),
        ):
            createTransaction(self.connection, self.accountId, transactionDate, "Suggestion fixture", self.categoryIds[category], amount)
        createFlexibleCategoryBudget(self.connection, self.categoryIds["Wants"], "77.77")
        summary = sinkingFundSummary(self.connection, date(2026, 1, 10))
        suggestions = {row["id"]: row["suggestionCents"] for row in summary["flexibleCategories"]}
        self.assertEqual(2667, suggestions[self.categoryIds["Wants"]])
        self.assertEqual(0, suggestions[self.categoryIds["Giving"]])
        setSuggestionRounding(self.connection, "10.00")
        rounded = sinkingFundSummary(self.connection, date(2026, 1, 10))
        self.assertEqual(3000, next(row for row in rounded["flexibleCategories"] if row["id"] == self.categoryIds["Wants"])["suggestionCents"])
        self.assertEqual(7777, rounded["flexibleBudgets"][0]["monthlyTargetCents"])
        self.assertEqual(7777, rounded["monthlyRequirementCents"])

class ExternalSimulatorAllocationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.databasePath = Path(self.directory.name) / "explicit-external-allocation.db"
        database.initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
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
        otherConnection = connectDatabase(self.databasePath)
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

    def testRecordedTransfersAndPayoffsRollbackPairAndLinkFailures(self):
        import sqlite3
        creditId = database.createAccount(self.connection, "Synthetic Card", "credit", "Credit Card", openingBalance="40.00")
        for rowType in ("transfer", "payoff"):
            for failure in ("source", "destination", "execution"):
                with self.subTest(rowType=rowType, failure=failure):
                    rowId = database.saveSimulatorRow(self.connection, self.planId, None, rowType, self.cashId, creditId, label="Saved draft", amount="10.00")
                    if failure == "execution":
                        statement = f"CREATE TRIGGER failRecordedTransfer BEFORE UPDATE ON simulatorRows WHEN NEW.id = {rowId} AND NEW.executed = 1 BEGIN SELECT RAISE(ABORT, 'forced execution failure'); END"
                    else:
                        sign = "<" if failure == "source" else ">"
                        statement = f"CREATE TRIGGER failRecordedTransfer BEFORE INSERT ON transactions WHEN NEW.transferId IS NOT NULL AND NEW.amountCents {sign} 0 BEGIN SELECT RAISE(ABORT, 'forced pair failure'); END"
                    with self.connection:
                        self.connection.execute(statement)
                    before = list(self.connection.iterdump())
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "forced .* failure"):
                        database.recordSimulatorTransfer(self.connection, self.planId, rowId, "2024-02-29", self.cashId, creditId, "10.00", "Confirmed payment")
                    self.assertEqual(before, list(self.connection.iterdump()), "Both ledger inserts, audits, and execution changes must roll back together")
                    with self.connection:
                        self.connection.execute("DROP TRIGGER failRecordedTransfer")
                    transferId, sourceId = database.recordSimulatorTransfer(self.connection, self.planId, rowId, "2024-02-29", self.cashId, creditId, "10.00", "Confirmed payment")
                    rows = self.connection.execute("SELECT * FROM transactions WHERE transferId = ? ORDER BY amountCents", (transferId,)).fetchall()
                    self.assertTrue(database.validTransferRows(rows))
                    self.assertEqual(sourceId, rows[0]["id"])
                    saved = self.connection.execute("SELECT * FROM simulatorRows WHERE id = ?", (rowId,)).fetchone()
                    self.assertEqual((1, sourceId, "fixed"), tuple(saved[field] for field in ("executed", "executedTransactionId", "amountMode")))
                    if rowType == "payoff":
                        self.assertEqual((1000, "Confirmed payment"), (saved["amountCents"], saved["label"]))

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
            connection = connectDatabase(databasePath)
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
            connection = connectDatabase(databasePath)
            try:
                planId = database.createSimulatorPlan(connection, "Fictional ineligible", "2026-10-17")
                for name, accountClass, subtype in [("Fictional Cash", "cash", None), ("Fictional Loan", "credit", "Personal Loan"), ("Fictional Other", "credit", "Other"), ("Fictional Unlimited", "credit", "Credit Card")]:
                    accountId = database.createAccount(connection, name, accountClass, subtype)
                    database.saveSimulatorRow(connection, planId, None, "spend", accountId, label="Fictional charge", amount="10.00")
                self.assertTrue(all(row["projectedUtilization"] is None for row in database.simulatorWorkspace(connection, planId)["plan"]["results"]))
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
