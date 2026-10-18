import tests  # Enable the production database guard before application imports.
import json
import tempfile
import unittest
from pathlib import Path

from app import createApp
import database as ledger
from users import createUser
from tests.test_release_2610 import signIn


class IntegrationRequestSizeTests(unittest.TestCase):
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
