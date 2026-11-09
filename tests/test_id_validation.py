"""Malformed edit IDs must fail validation without changing financial state."""

import tempfile
import unittest
from pathlib import Path

import tests  # Enable the database guard before importing application modules.
from app import createApp
import database as ledger
from tests.test_support import ownerClient
from tests.test_support import connectFixtureDatabase as connectDatabase


class AccountIdValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporaryDirectory.cleanup)
        self.databasePath = Path(self.temporaryDirectory.name) / "validation.db"
        self.application = createApp(self.databasePath)
        self.application.config["TESTING"] = True
        self.client = ownerClient(self.application)
        self.connection = connectDatabase(self.databasePath)
        self.addCleanup(self.connection.close)
        self.cashId = ledger.createAccount(self.connection, "Cash", "cash", openingBalance="100.00")
        self.creditId = ledger.createAccount(self.connection, "Credit", "credit", openingBalance="20.00")
        self.categoryId = ledger.createCategory(self.connection, "Expense")
        self.transactionId = ledger.createTransaction(self.connection, self.cashId, "2026-10-01", "Purchase", self.categoryId, "1.00")
        self.transferId = ledger.createTransfer(self.connection, "2026-10-01", self.cashId, self.creditId, "2.00")
        self.planId = ledger.createSimulatorPlan(self.connection, "Plan", "2026-10-01")
        self.rowId = ledger.saveSimulatorRow(self.connection, self.planId, None, "transfer", self.cashId, self.creditId, label="Payment", amount="3.00")

    def financialSnapshot(self):
        return {
            table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("accounts", "transactions", "simulatorRows")
        }

    def operations(self):
        return (
            ("transaction", "accountId", self.cashId),
            ("transfer", "fromAccountId", self.cashId),
            ("transfer", "toAccountId", self.creditId),
            ("simulator", "fromAccountId", self.cashId),
            ("simulator", "toAccountId", self.creditId),
        )

    def requestDetails(self, operation):
        payload = {"date": "2026-10-02", "description": "Edited", "amount": "4.00"}
        if operation == "transaction":
            payload.update(accountId=self.cashId, categoryId=self.categoryId)
            return "PATCH", f"/api/transactions/{self.transactionId}", payload
        payload.update(fromAccountId=self.cashId, toAccountId=self.creditId)
        if operation == "transfer":
            return "PATCH", f"/api/transfers/{self.transferId}", payload
        return "POST", f"/api/simulator/plans/{self.planId}/rows/{self.rowId}/transfer", payload

    def callBackend(self, operation, payload):
        if operation == "transaction":
            return ledger.updateTransaction(self.connection, self.transactionId, payload.get("accountId"), payload["date"], payload["description"], self.categoryId, payload["amount"])
        arguments = (payload["date"], payload.get("fromAccountId"), payload.get("toAccountId"), payload["amount"], payload["description"])
        if operation == "transfer":
            return ledger.updateTransfer(self.connection, self.transferId, *arguments)
        return ledger.recordSimulatorTransfer(self.connection, self.planId, self.rowId, *arguments)

    def testMalformedIdsRaiseValidationAndPreserveFinancialState(self):
        before = self.financialSnapshot()
        for operation, field, _ in self.operations():
            for invalidId in (None, "", "bad-id", [], {}):
                with self.subTest(operation=operation, field=field, invalidId=invalidId):
                    _, _, payload = self.requestDetails(operation)
                    payload[field] = invalidId
                    with self.assertRaises(ledger.ValidationError):
                        self.callBackend(operation, payload)
                    self.assertEqual(before, self.financialSnapshot())

    def testMalformedAndMissingIdsReturn400WithoutWrites(self):
        before = self.financialSnapshot()
        for operation, field, _ in self.operations():
            for invalidId in (None, "", "bad-id", [], {}, "missing"):
                with self.subTest(operation=operation, field=field, invalidId=invalidId):
                    method, path, payload = self.requestDetails(operation)
                    if invalidId == "missing":
                        del payload[field]
                    else:
                        payload[field] = invalidId
                    response = self.client.open(path, method=method, json=payload)
                    self.assertEqual(400, response.status_code)
                    self.assertIsInstance(response.get_json()["error"], str)
                    self.assertEqual(before, self.financialSnapshot())

    def testMalformedSelectorAndSortFieldsFailWithoutFinancialOrAuditWrites(self):
        from unittest.mock import patch
        templateId = ledger.createTemplate(self.connection, "Expense preset", self.cashId, "Purchase", self.categoryId, "4.00")
        incomeId = ledger.createCategory(self.connection, "Income", reportingRole="income")
        incomeTemplateId = ledger.createTemplate(self.connection, "Income preset", self.cashId, "Income", incomeId, "5.00")
        expectedId = ledger.createExpectedIncomePlan(self.connection, incomeTemplateId, "2026-10-02")
        sinkingId = ledger.createSinkingFundPlan(self.connection, templateId, "monthly", "2026-10-02")
        paymentAccountId = ledger.createAccount(self.connection, "Payment Card", "credit", "Credit Card")
        paymentId = ledger.createPlannedCreditPayment(self.connection, "2026-10-02", paymentAccountId, "Payment", "5.00")
        transaction = {"date": "2026-10-02", "accountId": self.cashId, "categoryId": self.categoryId, "description": "Purchase", "amount": "4.00"}
        transfer = {"date": "2026-10-02", "fromAccountId": self.cashId, "toAccountId": self.creditId, "description": "Payment", "amount": "4.00"}
        template = {"name": "Expense preset", "accountId": self.cashId, "description": "Purchase", "categoryId": self.categoryId, "amount": "4.00"}
        cases = [
            ("POST", "/api/transactions", transaction, ("accountId", "categoryId")),
            ("PATCH", f"/api/transactions/{self.transactionId}", transaction, ("accountId", "categoryId")),
            ("POST", "/api/transfers", transfer, ("fromAccountId", "toAccountId")),
            ("PATCH", f"/api/transfers/{self.transferId}", transfer, ("fromAccountId", "toAccountId")),
            ("POST", "/api/templates", {**template, "name": "New preset"}, ("accountId", "categoryId", "displayOrder")),
            ("PATCH", f"/api/templates/{templateId}", template, ("accountId", "categoryId", "displayOrder")),
            ("POST", "/api/categories", {"name": "New category"}, ("parentCategoryId", "displayOrder")),
            ("PATCH", f"/api/categories/{self.categoryId}", {"name": "Expense"}, ("parentCategoryId", "displayOrder")),
            ("POST", "/api/accounts", {"name": "New account", "accountClass": "cash"}, ("displayOrder",)),
            ("POST", "/api/expected-income-plans", {"expectedDate": "2026-10-02"}, ("templateId",)),
            ("POST", "/api/sinking-funds", {"frequency": "monthly", "expectedDate": "2026-10-02"}, ("templateId",)),
            ("POST", "/api/flexible-budgets", {"monthlyTarget": "5.00"}, ("categoryId",)),
            ("POST", "/api/planned-credit-payments", {"expectedDate": "2026-10-02", "description": "Payment", "amount": "5.00"}, ("accountId",)),
            ("POST", f"/api/simulator/plans/{self.planId}/rows", {"rowType": "spend", "fromAccountId": self.cashId, "label": "Spend", "amount": "4.00"}, ("fromAccountId", "categoryId")),
            ("PATCH", f"/api/expected-income-plans/{expectedId}", {"expectedDate": "2026-10-02", "active": False}, ("templateId",)),
            ("PATCH", f"/api/sinking-funds/{sinkingId}", {"frequency": "monthly", "expectedDate": "2026-10-02", "active": False}, ("templateId",)),
            ("PATCH", f"/api/planned-credit-payments/{paymentId}", {"expectedDate": "2026-10-02", "description": "Payment", "amount": "5.00", "active": False}, ("accountId",)),
        ]
        before = list(self.connection.iterdump())
        invalidValues = ([], {}, True, False, 1.5, "bad-id", "1_0", "1.0", "\u0661", 2 ** 63, -(2 ** 63) - 1)
        with patch.object(self.application.logger, "exception") as unexpectedFailure:
            for method, path, payload, fields in cases:
                for field in fields:
                    for value in invalidValues + ((None,) if field == "displayOrder" else ()):
                        with self.subTest(method=method, path=path, field=field, value=value):
                            response = self.client.open(path, method=method, json={**payload, field: value})
                            self.assertEqual(400, response.status_code, response.get_data(as_text=True))
                            self.assertTrue(response.get_json()["error"])
            unexpectedFailure.assert_not_called()
        for value in invalidValues:
            for operation in (
                lambda: ledger.createTransaction(self.connection, value, "2026-10-02", "Purchase", self.categoryId, "1.00"),
                lambda: ledger.createTransaction(self.connection, self.cashId, "2026-10-02", "Purchase", value, "1.00"),
                lambda: ledger.createCategory(self.connection, "Invalid parent", parentCategoryId=value),
                lambda: ledger.createTemplate(self.connection, "Invalid order", None, None, None, None, displayOrder=value),
            ):
                with self.assertRaises(ledger.ValidationError):
                    operation()
        self.assertEqual(before, list(self.connection.iterdump()))

    def testIntegerStringSelectorsSortOrdersAndOptionalReferencesRemainSupported(self):
        parentId = ledger.createCategory(self.connection, "Empty parent")
        category = self.client.post("/api/categories", json={"name": "Child", "parentCategoryId": str(parentId), "displayOrder": "-20"})
        self.assertEqual(201, category.status_code)
        childId = category.get_json()["id"]
        preset = self.client.post("/api/templates", json={"name": "Typed preset", "accountId": str(self.cashId), "categoryId": str(childId), "description": "Purchase", "amount": "1.23", "displayOrder": "7"})
        self.assertEqual(201, preset.status_code)
        transaction = self.client.post("/api/transactions", json={"accountId": str(self.cashId), "categoryId": str(childId), "date": "2026-10-02", "description": "Purchase", "amount": "1.23"})
        self.assertEqual(201, transaction.status_code)
        self.assertEqual((parentId, -20), tuple(self.connection.execute("SELECT parentCategoryId,displayOrder FROM categories WHERE id=?", (childId,)).fetchone()))
        self.assertEqual((self.cashId, childId, 7), tuple(self.connection.execute("SELECT accountId,categoryId,displayOrder FROM transactionTemplates WHERE id=?", (preset.get_json()["id"],)).fetchone()))
        self.assertEqual((self.cashId, childId, -123), tuple(self.connection.execute("SELECT accountId,categoryId,amountCents FROM transactions WHERE id=?", (transaction.get_json()["id"],)).fetchone()))
        for index, empty in enumerate((None, "")):
            partial = self.client.post("/api/templates", json={"name": f"Partial {index}", "accountId": empty, "categoryId": empty})
            standalone = self.client.post("/api/categories", json={"name": f"Standalone {index}", "parentCategoryId": empty})
            self.assertEqual(201, partial.status_code)
            self.assertEqual(201, standalone.status_code)
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())

    def testIntegerStringIdsStillSucceed(self):
        for operation in ("transaction", "transfer", "simulator"):
            with self.subTest(operation=operation):
                method, path, payload = self.requestDetails(operation)
                for field in ("accountId", "fromAccountId", "toAccountId"):
                    if field in payload:
                        payload[field] = str(payload[field])
                response = self.client.open(path, method=method, json=payload)
                self.assertEqual(201 if operation == "simulator" else 200, response.status_code)
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
