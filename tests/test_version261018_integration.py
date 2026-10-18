import tests  # Enable the production database guard before application imports.
import json
import tempfile
import unittest
from pathlib import Path

from app import createApp
import database as ledger
from users import createUser
from tests.test_release_2610 import signIn


class IntegrationApiFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.directory.name) / "synthetic.db"
        self.backupsPath = Path(self.directory.name) / "backups"
        self.backupsPath.mkdir()
        self.application = createApp(self.databasePath)
        self.connection = ledger.connectDatabase(self.databasePath)
        createUser(self.connection, "owner", "Owner", "owner password")
        self.cash = ledger.createAccount(self.connection, "Example Cash", "cash")
        self.expense = ledger.createCategory(self.connection, "Example Expense")
        self.integrationId = ledger.createIntegrationSecret(self.connection, "Example Agent")
        self.secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.integrationId,)).fetchone()[0]
        self.headers = {"Authorization": "Bearer " + self.secret}
        self.client = self.application.test_client()
        self.limit = self.application.config["MAX_CONTENT_LENGTH"]
        self.proposal = {"submissionId": "example", "date": "2026-10-01", "accountId": self.cash, "categoryId": self.expense, "description": "Example purchase", "amount": "1.00"}

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def snapshot(self):
        return tuple(self.connection.iterdump())

    def bodyWithSize(self, size, content=None):
        encoded = json.dumps(self.proposal if content is None else content).encode("utf-8")
        return encoded + b" " * (size - len(encoded))


class IntegrationRequestSizeTests(IntegrationApiFixture):
    def testOversizedSubmissionUsesDocumentedJsonAndWritesNothing(self):
        before = self.snapshot()
        for body in (self.bodyWithSize(self.limit + 1), b"PRIVATE MALFORMED " + b"x" * self.limit, b"\xc3\xa9" * (self.limit // 2 + 1)):
            with self.subTest(byteLength=len(body)):
                response = self.client.post("/api/integration/submissions", data=body, content_type="application/json", headers=self.headers)
                self.assertEqual(413, response.status_code)
                self.assertEqual("application/json", response.mimetype)
                self.assertEqual({"error": "Request exceeds the 1 MiB limit"}, response.get_json())
                self.assertEqual(before, self.snapshot())

    def testByteBoundaryStillParsesAndValidatesAtLimit(self):
        self.assertEqual(1048576, self.limit)
        response = self.client.post("/api/integration/submissions", data=self.bodyWithSize(self.limit), content_type="application/json", headers=self.headers)
        self.assertEqual(201, response.status_code)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM pendingSubmissions").fetchone()[0])
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=self.bodyWithSize(self.limit, {}), content_type="application/json", headers=self.headers)
        self.assertEqual(400, response.status_code)
        self.assertEqual({"error"}, set(response.get_json()))
        self.assertEqual(before, self.snapshot())

    def testAuthenticationAndOwnerPrecedenceRemainUnchanged(self):
        oversized = self.bodyWithSize(self.limit + 1)
        before = self.snapshot()
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = self.client.post("/api/integration/submissions", data=oversized, content_type="application/json", headers=headers)
            self.assertEqual(401, response.status_code)
            self.assertEqual({"error"}, set(response.get_json()))
        self.assertEqual(before, self.snapshot())
        with self.connection:
            self.connection.execute("UPDATE integrationSecrets SET active = 0 WHERE id = ?", (self.integrationId,))
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=oversized, content_type="application/json", headers=self.headers)
        self.assertEqual(401, response.status_code)
        self.assertEqual(before, self.snapshot())
        with self.connection:
            self.connection.execute("DELETE FROM users")
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=oversized, content_type="application/json", headers=self.headers)
        self.assertEqual(503, response.status_code)
        self.assertEqual(before, self.snapshot())

    def testPendingUpdateOversizeLeavesSavedProposalIntact(self):
        proposalId = self.client.post("/api/integration/submissions", json=self.proposal, headers=self.headers).get_json()["id"]
        before = self.snapshot()
        response = self.client.put(f"/api/integration/submissions/{proposalId}", data=self.bodyWithSize(self.limit + 1), content_type="application/json", headers=self.headers)
        self.assertEqual(413, response.status_code)
        self.assertEqual({"error": "Request exceeds the 1 MiB limit"}, response.get_json())
        self.assertEqual(before, self.snapshot())

    def testOrdinaryErrorsAndBrowserHtmlRemainUnchanged(self):
        before = self.snapshot()
        response = self.client.post("/api/integration/submissions", data=b"{invalid", content_type="application/json", headers=self.headers)
        self.assertEqual(400, response.status_code)
        self.assertEqual({"error"}, set(response.get_json()))
        owner = self.application.test_client()
        signIn(owner)
        response = owner.post("/api/transactions", data=self.bodyWithSize(self.limit + 1), content_type="application/json")
        self.assertEqual(413, response.status_code)
        self.assertEqual("text/html", response.mimetype)
        self.assertEqual(before, self.snapshot())


class IntegrationHistoryFilterTests(IntegrationApiFixture):
    def setUp(self):
        super().setUp()
        createUser(self.connection, "auditor", "Auditor", "auditor password")
        self.otherCash = ledger.createAccount(self.connection, "Other Cash", "cash")
        self.purchase = ledger.createTransaction(self.connection, self.cash, "2026-10-01", "Example purchase", self.expense, "10.00")
        self.refund = ledger.createTransaction(self.connection, self.cash, "2026-10-02", "Example refund", self.expense, "-2.00")
        self.transferId = ledger.createTransfer(self.connection, "2026-10-03", self.cash, self.otherCash, "5.00", "Example transfer")
        with self.connection:
            self.connection.execute("INSERT INTO transactions (accountId, date, description, amountCents, transferId) VALUES (?, ?, ?, ?, ?)", (self.cash, "2026-10-04", "Invalid linked row", -300, "invalid-group"))
        self.historyPath = "/api/integration/read/transactions"

    def history(self, **filters):
        return self.client.get(self.historyPath, query_string={"period": "all", **filters}, headers=self.headers)

    def testMalformedSuppliedIdsReturnJson400WithoutWrites(self):
        before = self.snapshot()
        for name in ("accountId", "categoryId"):
            for value in ("", "0", "-1", "-99", "abc", "1.5", "1e2", " ", "null", "1/2"):
                with self.subTest(name=name, value=value):
                    response = self.history(**{name: value, "includeSummary": "true", "description": "Example", "limit": "1", "offset": "0"})
                    self.assertEqual(400, response.status_code)
                    self.assertEqual("application/json", response.mimetype)
                    self.assertEqual({"error": ("Account" if name == "accountId" else "Category") + " ID must be a positive integer"}, response.get_json())
                    self.assertEqual(before, self.snapshot())
        for filters in ({"accountId": self.cash, "categoryId": "0"}, {"accountId": "", "categoryId": self.expense}):
            self.assertEqual(400, self.history(**filters).status_code)

    def testOmittedExistingAndUnavailableIdsPreserveEnvelopeAndSummary(self):
        before = self.snapshot()
        result = self.history(includeSummary="true").get_json()
        self.assertEqual(4, result["total"])
        self.assertEqual({"expenseCents": 800, "incomeCents": 0, "transferPaymentCents": 500}, result["summary"])
        for filters in ({"accountId": 999999}, {"categoryId": 999999}, {"accountId": self.cash, "categoryId": 999999}):
            with self.subTest(filters=filters):
                response = self.history(includeSummary="true", **filters)
                self.assertEqual(200, response.status_code)
                self.assertEqual({"transactions": [], "total": 0, "offset": 0, "limit": 100, "nextOffset": None, "summary": {"expenseCents": 0, "incomeCents": 0, "transferPaymentCents": 0}}, response.get_json())
        result = self.history(accountId=self.cash, categoryId=self.expense, includeSummary="true").get_json()
        self.assertEqual(2, result["total"])
        self.assertEqual({self.purchase, self.refund}, {event["id"] for event in result["transactions"]})
        self.assertEqual(800, result["summary"]["expenseCents"])
        self.assertEqual(before, self.snapshot())

    def testArchivedReferencesStillMatchAndIntegerSyntaxIsPreserved(self):
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.cash,))
            self.connection.execute("UPDATE categories SET active = 0 WHERE id = ?", (self.expense,))
        before = self.snapshot()
        for value in (str(self.cash), f"+{self.cash}", f" {self.cash} ", f"0{self.cash}"):
            response = self.history(accountId=value)
            self.assertEqual(200, response.status_code)
            self.assertEqual(4, response.get_json()["total"])
        result = self.history(accountId=self.cash, categoryId=self.expense).get_json()
        self.assertEqual(2, result["total"])
        self.assertEqual(before, self.snapshot())

    def testTransferSideCategoryExclusionAndInvalidLinkedVisibility(self):
        before = self.snapshot()
        source = self.history(accountId=self.cash).get_json()
        destination = self.history(accountId=self.otherCash).get_json()
        self.assertEqual(1, destination["total"])
        event = destination["transactions"][0]
        self.assertTrue(event["combinedTransfer"])
        self.assertEqual(self.transferId, event["transferId"])
        self.assertIn(event, source["transactions"])
        invalid = next(row for row in source["transactions"] if row["transferId"] == "invalid-group")
        self.assertFalse(invalid["combinedTransfer"])
        self.assertEqual(-300, invalid["displayAmountCents"])
        self.assertEqual(0, self.history(categoryId=self.expense, type="transfers").get_json()["total"])
        self.assertEqual(before, self.snapshot())

    def testOtherFiltersPaginationAndSummaryRemainAuthoritative(self):
        before = self.snapshot()
        filters = {"accountId": self.cash, "categoryId": self.expense, "period": "custom", "startDate": "2026-10-01", "endDate": "2026-10-02", "type": "transactions", "description": "EXAMPLE", "amountMode": "between", "amountMin": "1.00", "amountMax": "10.00", "sort": "amount", "direction": "desc", "includeSummary": "true", "limit": 1}
        first = self.history(**filters).get_json()
        self.assertEqual((2, 1, self.purchase), (first["total"], first["nextOffset"], first["transactions"][0]["id"]))
        second = self.history(**filters, offset=1).get_json()
        self.assertEqual((2, None, self.refund), (second["total"], second["nextOffset"], second["transactions"][0]["id"]))
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual({"expenseCents": 800, "incomeCents": 0, "transferPaymentCents": 0}, first["summary"])
        self.assertEqual(before, self.snapshot())

    def testPermissionsAndBrowserStableIdBehaviorRemainUnchanged(self):
        before = self.snapshot()
        self.assertEqual(401, self.client.get(self.historyPath, query_string={"accountId": "0"}).status_code)
        owner = self.application.test_client()
        signIn(owner)
        auditor = self.application.test_client()
        signIn(auditor, "Auditor", "auditor password")
        for browser in (owner, auditor):
            self.assertEqual(401, browser.get(self.historyPath, query_string={"accountId": self.cash}).status_code)
            self.assertEqual(200, browser.get("/api/transactions?period=all&accountId=0").status_code)
            self.assertEqual([], browser.get("/api/transactions?period=all&accountId=0").get_json())
        self.assertEqual(before, self.snapshot())
