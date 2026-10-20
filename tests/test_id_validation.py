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
