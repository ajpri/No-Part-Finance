import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests  # Activates production-database isolation before importing database code.
from database import (
    ValidationError,
    budgetTemplateSuggestions,
    connectDatabase,
    createAccount,
    createCategory,
    createExpectedIncomePlan,
    createFlexibleCategoryBudget,
    createPlannedCreditPayment,
    createSinkingFundPlan,
    createTemplate,
    createTransaction,
    deleteTemplate,
    initializeDatabase,
    setPaychecksPerYear,
    setSuggestionRounding,
    setBudgetSuggestionWindows,
    sinkingFundOccurrenceRange,
    sinkingFundSummary,
    updateTemplate,
)
from tests.test_support import createScenarioCategories
from tests.test_support import connectFixtureDatabase as connectDatabase


class BudgetPhaseOneTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.temporaryDirectory.name) / "budget.db"
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        self.categoryIds = createScenarioCategories(self.connection)
        self.accountId = createAccount(self.connection, "Checking", "cash")
        self.templateId = createTemplate(
            self.connection, "Insurance", self.accountId, "Insurance bill",
            self.categoryIds["Wants"], "1200.00", 1,
        )

    def tearDown(self):
        self.connection.close()
        self.temporaryDirectory.cleanup()

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


if __name__ == "__main__":
    unittest.main()
