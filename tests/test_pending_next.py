import tests
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from app import createApp
import database as ledger
from users import createUser
from tests.test_release_2610 import signIn


class PendingNextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "synthetic.db"
        self.backupsPath = Path(self.directory.name) / "backups"
        self.backupsPath.mkdir()
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectDatabase(self.path)
        createUser(self.connection, "owner", "Owner", "owner password")
        createUser(self.connection, "auditor", "Auditor", "auditor password")
        self.cash = ledger.createAccount(self.connection, "Example Cash", "cash", openingBalance="100.00")
        self.card = ledger.createAccount(self.connection, "Example Card", "credit", "Credit Card", openingBalance="20.00")
        self.expense = ledger.createCategory(self.connection, "Example Expense")
        self.income = ledger.createCategory(self.connection, "Example Income", reportingRole="income")
        self.external = self.connection.execute("SELECT id FROM accounts WHERE systemIdentity = 'external'").fetchone()[0]
        self.integration = ledger.createIntegrationSecret(self.connection, "Example Agent")
        secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.integration,)).fetchone()[0]
        self.headers = {"Authorization": "Bearer " + secret}
        self.app = createApp(self.path)
        self.owner = self.app.test_client()
        self.assertEqual(302, signIn(self.owner).status_code)
        self.agent = self.app.test_client()
        self.ordinary = {"submissionId": "ordinary", "date": "2028-02-29", "accountId": self.cash, "description": "Example purchase", "categoryId": self.expense, "amount": "10.00"}
        self.transfer = {"submissionId": "transfer", "type": "transfer", "date": "2028-02-29", "fromAccountId": self.cash, "toAccountId": self.card, "description": "Example payment", "amount": "50.00"}

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def submit(self, content, manual=False):
        return ledger.submitPendingEntry(self.connection, None if manual else self.integration, content)[0]

    def rows(self):
        return [{"id": row["id"], **{key: row[key] for key in ledger.pendingFields(row["kind"])}, "reviewedRole": row["currentRole"], "reviewVersion": row["reviewVersion"]} for row in ledger.pendingReviewRows(self.connection)]

    def snapshot(self):
        return tuple(self.connection.iterdump())

    def testMixedKindsAndSourcesCountsSignsPinDatesAndNoPendingEffect(self):
        before = self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()
        self.submit(self.transfer)
        self.submit({**self.ordinary, "pinned": True}, manual=True)
        self.submit({**self.ordinary, "submissionId": "refund", "amount": "-5.00"})
        self.submit({**self.ordinary, "submissionId": "reversal", "categoryId": self.income, "amount": "-3.00"})
        review = self.owner.get("/api/pending").get_json()
        self.assertEqual((4, 3, 1), (review["count"], review["transactionCount"], review["transferCount"]))
        self.assertEqual(["Example Agent", "Manual", "Example Agent", "Example Agent"], [row["label"] for row in review["rows"]])
        self.assertEqual(before, self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json())
        ledger.decidePendingTransactions(self.connection, "approve", self.rows(), True)
        rows = self.connection.execute("SELECT * FROM transactions ORDER BY id").fetchall()
        self.assertEqual([-5000, 5000, -1000, 500, -300], [row["amountCents"] for row in rows])
        self.assertTrue(all(row["date"] == "2028-02-29" for row in rows))
        self.assertEqual(1, rows[2]["pinned"])
        self.assertEqual(0, sum(row["amountCents"] for row in rows[:2]))
        after = self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()
        self.assertEqual(500, after["expensesCents"])
        self.assertEqual(-300, after["incomeCents"])

    def testPendingSummaryUsesAllEnteredSignsAndCountsTransfersOnceWithoutWrites(self):
        self.submit({**self.ordinary, "pinned": False}, manual=True)
        self.submit({**self.ordinary, "submissionId": "refund", "accountId": self.card, "amount": "-5.00"})
        self.submit({**self.ordinary, "submissionId": "income", "categoryId": self.income, "amount": "25.00"})
        self.submit({**self.ordinary, "submissionId": "reversal", "categoryId": self.income, "amount": "-3.00"})
        self.submit(self.transfer)
        self.submit({**self.transfer, "submissionId": "external", "fromAccountId": self.external, "amount": "7.23"}, manual=True)
        before = self.snapshot()
        review = self.owner.get("/api/pending").get_json()
        summaryRows = [{"kind": row["kind"], "amount": row["amount"]} for row in review["rows"]]
        response = self.owner.post("/api/pending/summary", json={"rows": summaryRows})
        self.assertEqual(200, response.status_code)
        self.assertEqual({"transactionCents": 2700, "transferCents": 5723, "transactionDisplay": "$27.00", "transferDisplay": "$57.23"}, response.get_json())
        # Draft edits change the displayed sum without saving the proposal or ledger.
        summaryRows[0]["amount"] = "100.00"
        self.assertEqual(11700, self.owner.post("/api/pending/summary", json={"rows": summaryRows}).get_json()["transactionCents"])
        summaryRows[0]["amount"] = "10.00"
        self.assertEqual(2700, self.owner.post("/api/pending/summary", json={"rows": summaryRows}).get_json()["transactionCents"])
        self.assertEqual(before, self.snapshot())

    def testPendingSummaryEmptyExactCentsExpressionsNegativeNetsAndInvalidAmounts(self):
        self.assertEqual({"transactionCents": 0, "transferCents": 0}, ledger.pendingReviewSummary([]))
        rows = [
            {"kind": "transaction", "amount": "$1,000.50-125.25"},
            {"kind": "transaction", "amount": "-1,000.00"},
            {"kind": "transaction", "amount": ".10+.20"},
            {"kind": "transfer", "amount": "10.01+2.02"},
        ]
        result = self.owner.post("/api/pending/summary", json={"rows": rows}).get_json()
        self.assertEqual({"transactionCents": -12445, "transferCents": 1203, "transactionDisplay": "-$124.45", "transferDisplay": "$12.03"}, result)
        # Formatting remains exact beyond the browser's safe integer range.
        huge = [{"kind": "transaction", "amount": "90071992547409.91"}, {"kind": "transaction", "amount": ".02"}]
        hugeResult = self.owner.post("/api/pending/summary", json={"rows": huge}).get_json()
        self.assertEqual(9007199254740993, hugeResult["transactionCents"])
        self.assertEqual("$90,071,992,547,409.93", hugeResult["transactionDisplay"])
        for invalid in ("", "broken", "1.001", "1,00", None, True, 3.5):
            with self.subTest(invalid=invalid):
                partial = ledger.pendingReviewSummary([*rows, {"kind": "transaction", "amount": invalid}])
                self.assertEqual({"transactionCents": None, "transferCents": 1203}, partial)
        for invalid in ("0", "-1", "bad"):
            with self.subTest(transferAmount=invalid):
                partial = ledger.pendingReviewSummary([*rows, {"kind": "transfer", "amount": invalid}])
                self.assertEqual({"transactionCents": -12445, "transferCents": None}, partial)
        self.assertEqual({"transactionCents": 0, "transferCents": 0}, ledger.pendingReviewSummary([
            {"kind": "transaction", "amount": "1.00"}, {"kind": "transaction", "amount": "-1.00"},
        ]))

    def testPendingSummaryRejectsMalformedRequestsAndPreservesPermissions(self):
        before = self.snapshot()
        for submitted in (None, [], {}, {"rows": None}, {"rows": {}}, {"rows": [None]}, {"rows": [{"kind": "other", "amount": "1"}]}, {"rows": [{"kind": "transfer"}]}, {"rows": [], "decision": "approve"}):
            with self.subTest(submitted=submitted):
                self.assertEqual(400, self.owner.post("/api/pending/summary", json=submitted).status_code)
        auditor = self.app.test_client()
        signIn(auditor, "Auditor", "auditor password")
        request = {"rows": []}
        self.assertEqual(403, auditor.post("/api/pending/summary", json=request).status_code)
        self.assertEqual(403, self.agent.post("/api/pending/summary", json=request, headers=self.headers).status_code)
        self.assertEqual(401, self.app.test_client().post("/api/pending/summary", json=request).status_code)
        token = self.owner.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, self.owner.post("/api/pending/summary", json=request).status_code)
        self.owner.environ_base["HTTP_X_CSRF_TOKEN"] = token
        self.assertEqual(before, self.snapshot())

    def testPendingSummaryTracksSavedCorrectionsRestartAndTerminalDecisions(self):
        self.submit({**self.ordinary, "pinned": False}, manual=True)
        self.submit(self.transfer)
        correction = self.rows()[0]
        fields = {key: value for key, value in correction.items() if key != "id"}
        fields["amount"] = "12.34"
        self.assertEqual(200, self.owner.patch(f"/api/pending/{correction['id']}", json=fields).status_code)
        restarted = createApp(self.path).test_client()
        self.assertEqual(302, signIn(restarted).status_code)

        def summary():
            review = restarted.get("/api/pending").get_json()
            rows = [{"kind": row["kind"], "amount": row["amount"]} for row in review["rows"]]
            return restarted.post("/api/pending/summary", json={"rows": rows}).get_json()

        self.assertEqual((1234, 5000), (summary()["transactionCents"], summary()["transferCents"]))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        ledger.decidePendingTransactions(self.connection, "approve", [self.rows()[0]])
        self.assertEqual((0, 5000), (summary()["transactionCents"], summary()["transferCents"]))
        ledger.decidePendingTransactions(self.connection, "reject", self.rows())
        self.assertEqual({"transactionCents": 0, "transferCents": 0, "transactionDisplay": "$0.00", "transferDisplay": "$0.00"}, summary())
        self.assertEqual([-1234], [row[0] for row in self.connection.execute("SELECT amountCents FROM transactions")])

    def testDocumentedRequestsVersionAndOwnerOnlyContract(self):
        from packageApplication import applicationVersion
        contract = self.owner.get("/openapi.json").get_json()
        self.assertEqual("26.10.18", applicationVersion)
        self.assertEqual(applicationVersion, contract["info"]["version"])
        body = contract["paths"]["/api/integration/submissions"]["post"]["requestBody"]["content"]["application/json"]
        shapes = body["schema"]["oneOf"]
        self.assertEqual(2, len(shapes))
        self.assertTrue(all(shape["additionalProperties"] is False for shape in shapes))
        for index, example in enumerate(body["examples"].values()):
            submitted = dict(example["value"])
            submitted["submissionId"] = f"documented-{index}"
            if submitted.get("type") == "transfer":
                submitted.update(fromAccountId=self.cash, toAccountId=self.card)
            else:
                submitted.update(accountId=self.cash, categoryId=self.expense)
            response = self.agent.post("/api/integration/submissions", json=submitted, headers=self.headers)
            self.assertEqual(201, response.status_code, response.get_json())
            result = response.get_json()
            self.assertEqual(ledger.pendingKind(submitted), result["kind"])
            self.assertIsNone(result["transactionId"])
            self.assertIsNone(result["transferId"])
        self.assertTrue(all("manual" not in path and "move-to-pending" not in path for path in contract["paths"]))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testTransferShapesAndEligibilityRejectWithoutWrites(self):
        invalid = [{**self.transfer, "amount": amount} for amount in ("0", "-1", "text", "1.001", "1,00", 1)]
        invalid += [{**self.transfer, "toAccountId": self.cash}, {**self.transfer, "categoryId": self.expense}, {**self.transfer, "accountId": self.cash}, {**self.transfer, "type": "payment"}, {**self.transfer, "fromAccountId": True}, {**self.transfer, "date": "2027-02-29"}, {key: value for key, value in self.transfer.items() if key != "description"}]
        before = self.snapshot()
        for content in invalid:
            with self.subTest(content=content):
                self.assertEqual(400, self.agent.post("/api/integration/submissions", json=content, headers=self.headers).status_code)
                self.assertEqual(before, self.snapshot())
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.card,))
        self.assertEqual(400, self.agent.post("/api/integration/submissions", json=self.transfer, headers=self.headers).status_code)

    def testCashCreditLoanOverpaymentAndExternalBothDirections(self):
        loan = ledger.createAccount(self.connection, "Example Loan", "credit", "Personal Loan", openingBalance="2.00")
        otherCash = ledger.createAccount(self.connection, "Other Cash", "cash")
        pairs = [(self.cash, otherCash), (self.cash, self.card), (self.cash, loan), (self.external, self.cash), (self.cash, self.external), (self.external, self.card)]
        for index, (fromId, toId) in enumerate(pairs):
            self.submit({**self.transfer, "submissionId": str(index), "fromAccountId": fromId, "toAccountId": toId})
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        groups = self.connection.execute("SELECT transferId, COUNT(*), SUM(amountCents) FROM transactions GROUP BY transferId").fetchall()
        self.assertEqual(6, len(groups))
        self.assertTrue(all(tuple(row)[1:] == (2, 0) for row in groups))
        report = self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()
        self.assertEqual((0, 0), (report["incomeCents"], report["expensesCents"]))

    def testSavedCorrectionKindIdentityRestartTerminalAndDeletedGroup(self):
        proposal = self.submit(self.transfer)
        row = {**self.rows()[0], "amount": "7.00"}
        fields = {key: value for key, value in row.items() if key != "id"}
        ledger.savePendingReview(self.connection, proposal["id"], fields)
        self.connection.close(); ledger.initializeDatabase(self.path); self.connection = ledger.connectDatabase(self.path)
        self.assertEqual("7.00", ledger.pendingReviewRows(self.connection)[0]["amount"])
        self.assertEqual(proposal["id"], self.submit(self.transfer)["id"])
        with self.assertRaises(ledger.SubmissionConflict):
            self.submit({**self.transfer, "amount": "7.00"})
        with self.assertRaises(ledger.SubmissionConflict):
            self.submit({**self.ordinary, "submissionId": "transfer"})
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        response = self.agent.get("/api/integration/submissions", headers=self.headers).get_json()["submissions"][0]
        self.assertEqual("transfer", response["kind"])
        self.assertIsNone(response["transactionId"])
        group = response["transferId"]
        ledger.deleteTransaction(self.connection, self.connection.execute("SELECT id FROM transactions WHERE transferId = ?", (group,)).fetchone()[0])
        retried = self.agent.post("/api/integration/submissions", json=self.transfer, headers=self.headers).get_json()
        self.assertEqual(("Approved", group), (retried["status"], retried["transferId"]))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testManualPermissionsCsrfIdentityAndStatusIsolation(self):
        manual = {**self.ordinary, "pinned": True}
        auditor = self.app.test_client(); signIn(auditor, "Auditor", "auditor password")
        before = self.snapshot()
        self.assertEqual(403, auditor.post("/api/pending/manual", json=manual).status_code)
        self.assertEqual(403, self.agent.post("/api/pending/manual", json=manual, headers=self.headers).status_code)
        token = self.owner.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, self.owner.post("/api/pending/manual", json=manual).status_code)
        self.owner.environ_base["HTTP_X_CSRF_TOKEN"] = token
        self.assertEqual(before, self.snapshot())
        first = self.owner.post("/api/pending/manual", json=manual)
        self.assertEqual(201, first.status_code)
        self.assertEqual(200, self.owner.post("/api/pending/manual", json=manual).status_code)
        self.assertEqual(409, self.owner.post("/api/pending/manual", json={**manual, "amount": "5.00"}).status_code)
        self.assertEqual(0, self.agent.get("/api/integration/submissions", headers=self.headers).get_json()["total"])
        ledger.decidePendingTransactions(self.connection, "reject", self.rows())
        self.assertEqual("Rejected", self.owner.post("/api/pending/manual", json=manual).get_json()["status"])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual(201, self.owner.post("/api/pending/manual", json={**manual, "submissionId": "distinct"}).status_code)

    def testMixedRollbackFirstTransferRowMidBatchAndDecisionFailure(self):
        self.submit({**self.ordinary, "pinned": True}, manual=True); self.submit(self.transfer)
        before = self.snapshot()
        triggers = ["CREATE TRIGGER fail BEFORE INSERT ON transactions WHEN NEW.amountCents > 0 BEGIN SELECT RAISE(ABORT, 'second side'); END", "CREATE TRIGGER fail BEFORE UPDATE OF status ON pendingSubmissions WHEN NEW.kind = 'transfer' BEGIN SELECT RAISE(ABORT, 'decision'); END"]
        for statement in triggers:
            with self.connection:
                self.connection.execute(statement)
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.decidePendingTransactions(self.connection, "approve", self.rows())
            with self.connection:
                self.connection.execute("DROP TRIGGER fail")
            self.assertEqual(before, self.snapshot())
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testConcurrentMixedApprovalCannotDuplicate(self):
        self.submit(self.ordinary); self.submit(self.transfer)
        reviewed = self.rows(); barrier = threading.Barrier(2)
        def approve(_):
            connection = ledger.connectDatabase(self.path)
            try:
                barrier.wait()
                try:
                    ledger.decidePendingTransactions(connection, "approve", reviewed)
                    return "approved"
                except ledger.SubmissionConflict:
                    return "stale"
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(["approved", "stale"], list(executor.map(approve, [1, 2])))
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testApproveAllQueueRevisionSavedContentAndEligibility(self):
        self.submit(self.ordinary); self.submit(self.transfer)
        rows = self.rows()
        with self.assertRaises(ledger.SubmissionConflict):
            ledger.decidePendingTransactions(self.connection, "approve", rows[:1], True)
        with self.assertRaises(ledger.SubmissionConflict):
            ledger.decidePendingTransactions(self.connection, "approve", [rows[0], {**rows[1], "amount": "1.00"}], True)
        fields = {key: value for key, value in rows[1].items() if key != "id"}
        ledger.savePendingReview(self.connection, rows[1]["id"], fields)
        for decision in ("approve", "reject"):
            with self.assertRaises(ledger.SubmissionConflict):
                ledger.decidePendingTransactions(self.connection, decision, rows)
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.card,))
        with self.assertRaises(ledger.ValidationError):
            ledger.decidePendingTransactions(self.connection, "approve", self.rows(), True)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testMoveOrdinaryAllSignsPreservesSnapshotPinOffsetsAndRoundTrip(self):
        for index, (category, amount, expected) in enumerate([(self.expense, "10.00", -1000), (self.expense, "-3.00", 300), (self.income, "9.00", 900), (self.income, "-2.00", -200)]):
            event = ledger.createTransaction(self.connection, self.card, "2020-01-01", "Round trip", category, amount, True)
            snapshot = ledger.ledgerMoveSnapshot(self.connection, transactionId=event)
            offsets = [tuple(row) for row in self.connection.execute("SELECT id, balanceOffsetCents FROM accounts")]
            proposal, created = ledger.moveLedgerToPending(self.connection, "move-" + str(index), snapshot, transactionId=event)
            self.assertTrue(created)
            self.assertIsNone(self.connection.execute("SELECT id FROM transactions WHERE id = ?", (event,)).fetchone())
            self.assertEqual(offsets, [tuple(row) for row in self.connection.execute("SELECT id, balanceOffsetCents FROM accounts")])
            self.assertEqual("2020-01-01", ledger.pendingReviewRows(self.connection)[0]["date"])
            ledger.decidePendingTransactions(self.connection, "approve", self.rows())
            transaction = self.connection.execute("SELECT * FROM transactions WHERE id = (SELECT transactionId FROM pendingSubmissions WHERE id = ?)", (proposal["id"],)).fetchone()
            self.assertEqual((expected, "2020-01-01", 1), (transaction["amountCents"], transaction["date"], transaction["pinned"]))
            self.assertNotEqual(snapshot[0]["eventIdentity"], transaction["eventIdentity"])

    def testMoveTransferResetsEitherSimulatorSideAllPlansNoRelink(self):
        group = ledger.createTransfer(self.connection, "2020-12-31", self.cash, self.card, "15.00", "Recorded payment")
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transferId=group)
        simulatorIds = []
        for index, ledgerRow in enumerate(snapshot):
            plan = ledger.createSimulatorPlan(self.connection, "Example plan", "2026-10-01")
            rowId = ledger.saveSimulatorRow(self.connection, plan, None, "transfer", self.cash, self.card, None, "Original plan", "7.00")
            with self.connection:
                self.connection.execute("UPDATE simulatorRows SET executed = 1, executedTransactionId = ? WHERE id = ?", (ledgerRow["id"], rowId))
            simulatorIds.append(rowId)
        beforeRows = [dict(row) for row in self.connection.execute("SELECT * FROM simulatorRows")]
        ledger.moveLedgerToPending(self.connection, "move-payment", snapshot, transferId=group)
        self.assertEqual(1, self.owner.get("/api/pending").get_json()["transferCount"])
        for before, after in zip(beforeRows, self.connection.execute("SELECT * FROM simulatorRows")):
            self.assertEqual({**before, "executed": 0, "executedTransactionId": None}, dict(after))
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertTrue(all(row["executedTransactionId"] is None and not row["executed"] for row in self.connection.execute("SELECT * FROM simulatorRows")))
        self.assertEqual(0, self.connection.execute("SELECT SUM(amountCents) FROM transactions").fetchone()[0])

    def testMoveStaleRetryConcurrencyAndReusedLedgerIdentity(self):
        event = ledger.createTransaction(self.connection, self.cash, "2020-01-01", "Original", self.expense, "10.00")
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transactionId=event)
        ledger.updateTransaction(self.connection, event, self.cash, "2020-01-02", "Changed", self.expense, "10.00")
        with self.assertRaises(ledger.SubmissionConflict):
            ledger.moveLedgerToPending(self.connection, "move", snapshot, transactionId=event)
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transactionId=event)
        proposal, _ = ledger.moveLedgerToPending(self.connection, "move", snapshot, transactionId=event)
        replacement = ledger.createTransaction(self.connection, self.cash, "2020-01-02", "Changed", self.expense, "10.00")
        self.assertNotEqual(event, replacement)
        # Simulate an externally reused numeric ID; retry must still leave it alone.
        with self.connection:
            self.connection.execute("UPDATE transactions SET id = ? WHERE id = ?", (event, replacement))
        replacement = event
        self.assertEqual((proposal["id"], False), (lambda result: (result[0]["id"], result[1]))(ledger.moveLedgerToPending(self.connection, "move", snapshot, transactionId=event)))
        with self.assertRaises(ledger.SubmissionConflict):
            ledger.moveLedgerToPending(self.connection, "another-move", snapshot, transactionId=event)
        self.assertIsNotNone(self.connection.execute("SELECT id FROM transactions WHERE id = ?", (replacement,)).fetchone())

    def testMoveFailuresRollbackProposalDeletionAndSimulatorReset(self):
        group = ledger.createTransfer(self.connection, "2020-01-01", self.cash, self.card, "5.00")
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transferId=group)
        plan = ledger.createSimulatorPlan(self.connection, "Rollback plan", "2026-10-01")
        simulatorId = ledger.saveSimulatorRow(self.connection, plan, None, "transfer", self.cash, self.card, None, "Saved planner", "4.00")
        with self.connection:
            self.connection.execute("UPDATE simulatorRows SET executed = 1, executedTransactionId = ? WHERE id = ?", (snapshot[1]["id"], simulatorId))
        before = self.snapshot()
        for trigger in ["CREATE TRIGGER fail BEFORE DELETE ON transactions BEGIN SELECT RAISE(ABORT, 'after proposal'); END", "CREATE TRIGGER fail BEFORE DELETE ON transactions WHEN OLD.amountCents > 0 BEGIN SELECT RAISE(ABORT, 'second removal'); END", "CREATE TRIGGER fail BEFORE UPDATE ON simulatorRows BEGIN SELECT RAISE(ABORT, 'reset'); END"]:
            with self.connection:
                self.connection.execute(trigger)
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.moveLedgerToPending(self.connection, "move", snapshot, transferId=group)
            with self.connection:
                self.connection.execute("DROP TRIGGER fail")
            self.assertEqual(before, self.snapshot())

    def testInvalidLegacyMovesRefusedAndArchivedReferencesRetained(self):
        event = ledger.createTransaction(self.connection, self.cash, "2020-01-01", "Archived purchase", self.expense, "10.00")
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.cash,))
            self.connection.execute("UPDATE categories SET active = 0 WHERE id = ?", (self.expense,))
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transactionId=event)
        ledger.moveLedgerToPending(self.connection, "archived", snapshot, transactionId=event)
        pending = ledger.pendingReviewRows(self.connection)[0]
        self.assertEqual((self.cash, self.expense), (pending["accountId"], pending["categoryId"]))
        self.assertIsNotNone(pending["validationError"])
        with self.assertRaises(ledger.ValidationError):
            ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        row = {**self.rows()[0], "accountId": self.card, "categoryId": self.income, "reviewedRole": "income", "date": "2029-01-01"}
        ledger.decidePendingTransactions(self.connection, "approve", [row])
        self.assertEqual("2029-01-01", self.connection.execute("SELECT date FROM transactions").fetchone()[0])
        with self.connection:
            legacyId = self.connection.execute("INSERT INTO transactions (accountId, date, description, amountCents) VALUES (?, '2020-01-01', 'Opening Balance', 100)", (self.card,)).lastrowid
        with self.assertRaises(ledger.ValidationError):
            ledger.ledgerMoveSnapshot(self.connection, transactionId=legacyId)
        with self.assertRaises(ledger.ValidationError):
            ledger.ledgerMoveSnapshot(self.connection, transferId="missing")

    def testOriginalIntegrationIdentityAfterMoveAndRejection(self):
        submitted = self.submit(self.ordinary)
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        original = self.submit(self.ordinary)
        event = original["transactionId"]
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transactionId=event)
        ledger.moveLedgerToPending(self.connection, "manual-move", snapshot, transactionId=event)
        ledger.decidePendingTransactions(self.connection, "reject", self.rows())
        original = self.submit(self.ordinary)
        self.assertEqual("Approved", original["status"])
        self.assertEqual(submitted["id"], original["id"])
        self.assertIsNone(original["transactionId"])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testRevokedLabelsAndOtherSecretStatusesStayIsolated(self):
        self.submit(self.transfer)
        other = ledger.createIntegrationSecret(self.connection, "Other Agent")
        ledger.submitPendingEntry(self.connection, other, {**self.ordinary, "submissionId": "other"})
        with self.connection:
            self.connection.execute("UPDATE integrationSecrets SET active = 0, label = 'Revoked relabeled' WHERE id = ?", (self.integration,))
        self.assertEqual("Revoked relabeled", ledger.pendingReviewRows(self.connection)[0]["label"])
        self.assertEqual(401, self.agent.get("/api/integration/submissions", headers=self.headers).status_code)
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testPinsPreserveFinancialStateClearOnArchiveAndSystemBoundary(self):
        before = self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()
        ledger.updateAccount(self.connection, self.cash, "Example Cash", "cash", pinnedForSelection=True)
        account = next(row for row in self.owner.get("/api/accounts").get_json() if row["id"] == self.cash)
        self.assertTrue(account["pinnedForSelection"])
        self.assertEqual(before["expensesCents"], self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()["expensesCents"])
        ledger.updateAccount(self.connection, self.cash, "Renamed", "credit", "Other")
        self.assertEqual(1, self.connection.execute("SELECT pinnedForSelection FROM accounts WHERE id = ?", (self.cash,)).fetchone()[0])
        ledger.updateAccount(self.connection, self.cash, "Renamed", "credit", "Other", active=False)
        self.assertEqual(0, self.connection.execute("SELECT pinnedForSelection FROM accounts WHERE id = ?", (self.cash,)).fetchone()[0])
        with self.assertRaises(ledger.ValidationError):
            ledger.updateAccount(self.connection, self.external, "External", "system", pinnedForSelection=True)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connection:
                self.connection.execute("UPDATE accounts SET pinnedForSelection = 1 WHERE id = ?", (self.external,))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testVersion20MigrationPreservesAllStateAndBackup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version20.db"
            connection = ledger.connectDatabase(path)
            connection.executescript(ledger.schemaSql + ledger.securitySchemaSql)
            ledger.createExternalSystemAccount(connection)
            with connection:
                cashId = connection.execute("INSERT INTO accounts (name, accountClass, balanceOffsetCents, displayOrder) VALUES ('Historical Cash', 'cash', 12345, 77)").lastrowid
                categoryId = connection.execute("INSERT INTO categories (name, reportingRole) VALUES ('Historical Expense', 'expense')").lastrowid
                connection.execute("INSERT INTO transactions (id, accountId, date, description, amountCents, categoryId, pinned) VALUES (42, ?, '2020-01-01', 'Historical purchase', -123, ?, 1)", (cashId, categoryId))
            createUser(connection, "owner", "Historical Owner", "owner password")
            secretId = ledger.createIntegrationSecret(connection, "Historical Agent")
            content = json.dumps({"submissionId":"original", "date":"2020-01-01", "accountId":cashId, "categoryId":categoryId, "description":"Original", "amount":"5.00"}, sort_keys=True)
            corrected = json.dumps({"date":"2020-01-02", "accountId":cashId, "categoryId":categoryId, "description":"Saved correction", "amount":"-2.00"}, sort_keys=True)
            plan = ledger.createSimulatorPlan(connection, "Historical plan", "2020-01-01")
            simulatorRow = ledger.saveSimulatorRow(connection, plan, None, "spend", cashId, None, categoryId, "Planner original", "1.23")
            with connection:
                connection.execute("UPDATE simulatorRows SET executed = 1, executedTransactionId = 42 WHERE id = ?", (simulatorRow,))
                connection.execute("INSERT INTO pendingSubmissions (integrationId, submissionId, originalContent, reviewContent, reviewVersion, originalRole, status, transactionId) VALUES (?, 'original', ?, ?, 3, 'expense', 'Approved', 42)", (secretId, content, corrected))
                connection.execute("UPDATE integrationSecrets SET active = 0 WHERE id = ?", (secretId,))
                connection.execute("PRAGMA user_version = 20")
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'")]
            before = {table: [dict(row) for row in connection.execute('SELECT * FROM "' + table + '"')] for table in tables}
            connection.close()
            ledger.initializeDatabase(path)
            connection = ledger.connectDatabase(path)
            try:
                for table, records in before.items():
                    after = [dict(row) for row in connection.execute('SELECT * FROM "' + table + '"')]
                    self.assertEqual(records, [{key: row[key] for key in records[index]} for index, row in enumerate(after)], table)
                migrated = connection.execute("SELECT * FROM pendingSubmissions").fetchone()
                self.assertEqual(("transaction", "integration", 0, None), (migrated["kind"], migrated["source"], migrated["pinned"], migrated["transferId"]))
                self.assertEqual(0, connection.execute("SELECT SUM(pinnedForSelection) FROM accounts").fetchone()[0])
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual(21, connection.execute("PRAGMA user_version").fetchone()[0])
                created = ledger.createTransaction(connection, cashId, "2026-10-02", "New event", categoryId, "1.00")
                self.assertGreater(created, 42)
            finally:
                connection.close()
            backups = list((Path(directory) / "backups").glob("pre-migration-v20-to-v21-*.db"))
            self.assertEqual(1, len(backups))
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(20, backup.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(-123, backup.execute("SELECT amountCents FROM transactions WHERE id = 42").fetchone()[0])
            finally:
                backup.close()

    def testLedgerMigrationFailureRollsBackTableRebuild(self):
        ledger.createTransaction(self.connection, self.cash, "2020-01-01", "Preserved", self.expense, "1.00")
        with self.connection:
            self.connection.execute("DROP TRIGGER ledgerEventIdentity")
            self.connection.execute("ALTER TABLE transactions DROP COLUMN eventIdentity")
        before = self.snapshot()
        with patch.object(ledger, "schemaSql", ledger.schemaSql + "SELECT missingMigrationColumn;"):
            with self.assertRaises(sqlite3.OperationalError):
                ledger.migrateToVersion21(self.connection)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, self.connection.execute("PRAGMA foreign_keys").fetchone()[0])
        self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertIsNone(self.connection.execute("PRAGMA foreign_key_check").fetchone())

    def testConcurrentMovesAndManualSubmissionsUseOneIdentity(self):
        event = ledger.createTransaction(self.connection, self.cash, "2020-01-01", "Move concurrently", self.expense, "10.00")
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transactionId=event)
        barrier = threading.Barrier(2)
        def move(_):
            connection = ledger.connectDatabase(self.path)
            try:
                barrier.wait()
                return ledger.moveLedgerToPending(connection, "concurrent-move", snapshot, transactionId=event)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(move, [1,2]))
        self.assertEqual(outcomes[0][0]["id"], outcomes[1][0]["id"])
        self.assertCountEqual([True, False], [result[1] for result in outcomes])
        barrier = threading.Barrier(2)
        manual = {**self.transfer, "submissionId": "concurrent-manual"}
        def submit(_):
            connection = ledger.connectDatabase(self.path)
            try:
                barrier.wait()
                return ledger.submitPendingEntry(connection, None, manual)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit, [1,2]))
        self.assertEqual(outcomes[0][0]["id"], outcomes[1][0]["id"])
        self.assertCountEqual([True,False], [result[1] for result in outcomes])
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM pendingSubmissions").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def testMoveApiPermissionCsrfSnapshotAndImmediateBalancesReports(self):
        event = ledger.createTransaction(self.connection, self.cash, "2026-10-01", "Example move", self.expense, "10.00")
        url = f"/api/transactions/{event}"
        snapshot = self.owner.get(url + "/pending-snapshot").get_json()["snapshot"]
        auditor = self.app.test_client(); signIn(auditor, "Auditor", "auditor password")
        self.assertEqual(403, auditor.get(url + "/pending-snapshot").status_code)
        self.assertEqual(403, self.agent.get(url + "/pending-snapshot", headers=self.headers).status_code)
        request = {"requestId": "api-move", "snapshot": snapshot}
        before = self.snapshot()
        self.assertEqual(403, auditor.post(url + "/move-to-pending", json=request).status_code)
        self.assertEqual(403, self.agent.post(url + "/move-to-pending", json=request, headers=self.headers).status_code)
        token = self.owner.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, self.owner.post(url + "/move-to-pending", json=request).status_code)
        self.owner.environ_base["HTTP_X_CSRF_TOKEN"] = token
        self.assertEqual(before, self.snapshot())
        beforeReport = self.owner.get("/api/dashboard?period=month:2026-10").get_json()
        self.assertEqual(1000, beforeReport["expensesCents"])
        self.assertEqual(201, self.owner.post(url + "/move-to-pending", json=request).status_code)
        afterReport = self.owner.get("/api/dashboard?period=month:2026-10").get_json()
        self.assertEqual(0, afterReport["expensesCents"])
        cash = next(row for row in self.owner.get("/api/accounts").get_json() if row["id"] == self.cash)
        self.assertEqual(10000, cash["balanceCents"])
        self.assertEqual(10000, cash["balanceOffsetCents"])
        self.assertEqual(200, self.owner.post(url + "/move-to-pending", json=request).status_code)

    def testInvalidTransferPairsRefusedWithoutWrites(self):
        group = ledger.createTransfer(self.connection, "2026-10-01", self.cash, self.card, "5.00")
        for mutation in ["UPDATE transactions SET description = 'Different' WHERE amountCents > 0", "UPDATE transactions SET categoryId = " + str(self.expense) + " WHERE amountCents > 0", "UPDATE transactions SET accountId = " + str(self.cash) + " WHERE amountCents > 0"]:
            with self.connection:
                self.connection.execute(mutation)
            before = self.snapshot()
            with self.assertRaises(ledger.ValidationError):
                ledger.ledgerMoveSnapshot(self.connection, transferId=group)
            self.assertEqual(before, self.snapshot())
            with self.connection:
                self.connection.execute("UPDATE transactions SET description = 'Transfer', categoryId = NULL, accountId = ? WHERE amountCents > 0", (self.card,))


    def integrationUpdateFields(self, row):
        fields = {key: row[key] for key in ledger.pendingFields(row["kind"])}
        fields["reviewVersion"] = row["reviewVersion"]
        if row["kind"] == "transaction":
            fields["reviewedRole"] = row["currentRole"]
        return fields

    def integrationPendingRow(self, proposalId):
        response = self.agent.get("/api/integration/pending", headers=self.headers)
        self.assertEqual(200, response.status_code)
        return next(row for row in response.get_json()["rows"] if row["id"] == proposalId)

    def updateThroughIntegration(self, proposalId, fields):
        return self.agent.put(f"/api/integration/submissions/{proposalId}", headers=self.headers, json=fields)

    def testIntegrationPendingReadAllSourcesWithoutWritesOrSecrets(self):
        self.assertEqual({"count": 0, "transactionCount": 0, "transferCount": 0, "rows": []}, self.agent.get("/api/integration/pending", headers=self.headers).get_json())
        first = self.submit(self.ordinary)
        other = ledger.createIntegrationSecret(self.connection, "Other Agent")
        ledger.submitPendingEntry(self.connection, other, {**self.transfer, "submissionId": "other-transfer"})
        manual = self.submit({**self.ordinary, "submissionId": "manual", "pinned": True}, manual=True)
        event = ledger.createTransaction(self.connection, self.cash, "2026-10-01", "Moved", self.expense, "5.00")
        snapshot = ledger.ledgerMoveSnapshot(self.connection, transactionId=event)
        moved, _ = ledger.moveLedgerToPending(self.connection, "moved", snapshot, transactionId=event)
        ledger.savePendingReview(self.connection, first["id"], {**self.integrationUpdateFields(self.integrationPendingRow(first["id"])), "description": "Owner corrected"})
        before = self.snapshot()
        review = self.agent.get("/api/integration/pending", headers=self.headers).get_json()
        self.assertEqual((4, 3, 1), (review["count"], review["transactionCount"], review["transferCount"]))
        self.assertEqual(self.owner.get("/api/pending").get_json(), review)
        self.assertEqual("Owner corrected", review["rows"][0]["description"])
        self.assertEqual("Manual", self.integrationPendingRow(manual["id"])["label"])
        self.assertEqual("Manual", self.integrationPendingRow(moved["id"])["label"])
        for row in review["rows"]:
            self.assertNotIn("sourceSnapshot", row)
            self.assertNotIn("originalContent", row)
        for secret in self.connection.execute("SELECT secret FROM integrationSecrets"):
            self.assertNotIn(secret["secret"], json.dumps(review))
        self.assertNotIn("passwordHash", json.dumps(review))
        own = self.agent.get("/api/integration/submissions", headers=self.headers).get_json()
        self.assertEqual(1, own["total"])
        self.assertEqual(first["id"], own["submissions"][0]["id"])
        self.assertEqual(1, own["submissions"][0]["reviewVersion"])
        self.assertEqual(before, self.snapshot())

    def testIntegrationUpdatesAcrossSourcesOwnerEditsAndRestart(self):
        other = ledger.createIntegrationSecret(self.connection, "Other Agent")
        proposal, _ = ledger.submitPendingEntry(self.connection, other, self.ordinary)
        row = self.integrationPendingRow(proposal["id"])
        fields = self.integrationUpdateFields(row)
        path = f"/api/pending/{proposal['id']}"
        self.assertEqual(200, self.owner.patch(path, json={**fields, "description": "Owner corrected"}).status_code)
        self.assertEqual(409, self.updateThroughIntegration(proposal["id"], fields).status_code)
        current = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        original = dict(self.connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (proposal["id"],)).fetchone())
        self.assertEqual({"reviewVersion": 2}, self.updateThroughIntegration(proposal["id"], {**current, "description": "Agent corrected"}).get_json())
        current = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        self.assertEqual({"reviewVersion": 3}, self.updateThroughIntegration(proposal["id"], current).get_json())
        self.assertEqual(409, self.updateThroughIntegration(proposal["id"], current).status_code)
        changed = dict(self.connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (proposal["id"],)).fetchone())
        self.assertEqual({key: value for key, value in original.items() if key not in {"reviewContent", "reviewVersion"}}, {key: value for key, value in changed.items() if key not in {"reviewContent", "reviewVersion"}})
        restarted = createApp(self.path).test_client()
        self.assertEqual("Agent corrected", restarted.get("/api/integration/pending", headers=self.headers).get_json()["rows"][0]["description"])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        for decision in ("approve", "reject"):
            before = self.snapshot()
            self.assertEqual(409, self.owner.post("/api/pending/decision", json={"decision": decision, "rows": [{"id": proposal["id"], **fields}]}).status_code)
            self.assertEqual(before, self.snapshot())
        self.assertEqual(409, self.owner.patch(path, json=fields).status_code)
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertEqual([], restarted.get("/api/integration/pending", headers=self.headers).get_json()["rows"])
        self.assertEqual(409, self.updateThroughIntegration(proposal["id"], fields).status_code)
        retry, created = ledger.submitPendingEntry(self.connection, other, self.ordinary)
        self.assertFalse(created)
        self.assertEqual("Approved", retry["status"])
        with self.assertRaises(ledger.SubmissionConflict):
            ledger.submitPendingEntry(self.connection, other, {**self.ordinary, "description": "Agent corrected"})

    def testIntegrationUpdatedOrdinarySignsAndAtomicApproval(self):
        expectedAmounts = []
        for index, (category, amount, expected) in enumerate(((self.expense, "25.00", -2500), (self.expense, "-5.00", 500), (self.income, "30.00", 3000), (self.income, "-4.00", -400), (self.expense, "0.00", 0))):
            proposal = self.submit({**self.ordinary, "submissionId": f"sign-{index}", "categoryId": category})
            fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
            self.assertEqual(200, self.updateThroughIntegration(proposal["id"], {**fields, "amount": amount, "date": "2028-02-29"}).status_code)
            self.assertEqual(expected, self.integrationPendingRow(proposal["id"])["ledgerAmountCents"])
            expectedAmounts.append(expected)
        before = self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()
        self.assertEqual((0, 0), (before["expensesCents"], before["incomeCents"]))
        with self.connection:
            self.connection.execute("CREATE TRIGGER failApproval BEFORE INSERT ON transactions WHEN NEW.amountCents = 3000 BEGIN SELECT RAISE(ABORT, 'Injected failure'); END")
        pending = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertEqual(pending, self.snapshot())
        with self.connection:
            self.connection.execute("DROP TRIGGER failApproval")
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertEqual(expectedAmounts, [row[0] for row in self.connection.execute("SELECT amountCents FROM transactions ORDER BY id")])
        report = self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()
        self.assertEqual((2000, 2600, 600), (report["expensesCents"], report["incomeCents"], report["netCents"]))

    def testIntegrationUpdatesCashLoanAndExternalTransfers(self):
        cash2 = ledger.createAccount(self.connection, "Second Cash", "cash")
        loan = ledger.createAccount(self.connection, "Example Loan", "credit", "Personal Loan")
        for index, (source, destination) in enumerate(((self.cash, cash2), (self.cash, self.card), (self.cash, loan), (self.external, self.cash), (self.cash, self.external))):
            proposal = self.submit({**self.transfer, "submissionId": f"direction-{index}"})
            fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
            self.assertEqual(200, self.updateThroughIntegration(proposal["id"], {**fields, "fromAccountId": source, "toAccountId": destination, "amount": "12.34", "description": "Preserved supplied wording"}).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        for row in self.connection.execute("SELECT transferId, COUNT(*) count, SUM(amountCents) total FROM transactions GROUP BY transferId"):
            self.assertEqual((2, 0), (row["count"], row["total"]))
        self.assertEqual({-1234, 1234}, {row[0] for row in self.connection.execute("SELECT amountCents FROM transactions")})
        self.assertTrue(all(row[0] is None for row in self.connection.execute("SELECT categoryId FROM transactions")))
        report = self.owner.get("/api/dashboard?period=custom&startDate=1900-01-01&endDate=2100-12-31").get_json()
        self.assertEqual((0, 0), (report["expensesCents"], report["incomeCents"]))

    def testIntegrationUpdatesManualMovedSnapshotsPinsAndLinks(self):
        manual = self.submit({**self.ordinary, "submissionId": "manual", "pinned": True}, manual=True)
        ordinary = ledger.createTransaction(self.connection, self.cash, "2026-10-01", "Original", self.expense, "7.00", pinned=True)
        group = ledger.createTransfer(self.connection, "2026-10-01", self.cash, self.card, "9.00")
        sourceId = self.connection.execute("SELECT id FROM transactions WHERE transferId = ? AND amountCents < 0", (group,)).fetchone()[0]
        plan = ledger.createSimulatorPlan(self.connection, "Linked plan", "2026-10-01")
        rowId = ledger.saveSimulatorRow(self.connection, plan, None, "transfer", self.cash, self.card, None, "Planner", "9.00")
        with self.connection:
            self.connection.execute("UPDATE simulatorRows SET executed = 1, executedTransactionId = ? WHERE id = ?", (sourceId, rowId))
        moved = []
        for identity, identifiers in (("move-ordinary", {"transactionId": ordinary}), ("move-transfer", {"transferId": group})):
            snapshot = ledger.ledgerMoveSnapshot(self.connection, **identifiers)
            proposal, _ = ledger.moveLedgerToPending(self.connection, identity, snapshot, **identifiers)
            moved.append((proposal, snapshot, identifiers))
        simulatorBefore = [dict(row) for row in self.connection.execute("SELECT * FROM simulatorRows")]
        for proposal in [manual, *[item[0] for item in moved]]:
            fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
            self.assertEqual(200, self.updateThroughIntegration(proposal["id"], {**fields, "description": "API corrected", "amount": "4.50"}).status_code)
            stored = dict(self.connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (proposal["id"],)).fetchone())
            for name in ("sourceSnapshot", "originalContent", "pinned", "source", "integrationId"):
                self.assertEqual(proposal[name], stored[name])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertEqual(simulatorBefore, [dict(row) for row in self.connection.execute("SELECT * FROM simulatorRows")])
        for proposal, snapshot, identifiers in moved:
            retried, created = ledger.moveLedgerToPending(self.connection, proposal["submissionId"], snapshot, **identifiers)
            self.assertEqual(proposal["id"], retried["id"])
            self.assertFalse(created)
        ledger.decidePendingTransactions(self.connection, "approve", self.rows())
        self.assertTrue(all(row[0] > sourceId for row in self.connection.execute("SELECT id FROM transactions")))
        self.assertEqual([1, 1], [row[0] for row in self.connection.execute("SELECT pinned FROM transactions WHERE transferId IS NULL")])
        self.assertEqual(simulatorBefore, [dict(row) for row in self.connection.execute("SELECT * FROM simulatorRows")])

    def testIntegrationUpdateValidationFailuresPreserveEverything(self):
        ordinary = self.submit(self.ordinary)
        transfer = self.submit(self.transfer)
        for proposal in (ordinary, transfer):
            fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
            invalid = [{**fields, "date": "2026-02-30"}, {**fields, "description": ""}, {**fields, "amount": "1.001"}, {**fields, "amount": 3.5}, {**fields, "pinned": True}, {**fields, "submissionId": "replacement"}, {**fields, "type": "transfer"}, {**fields, "reviewVersion": True}, {**fields, "reviewVersion": -1}]
            if proposal["kind"] == "transfer":
                invalid += [{**fields, "amount": "0.00"}, {**fields, "amount": "-1.00"}, {**fields, "fromAccountId": self.card}, {**fields, "toAccountId": 99999}, {**fields, "categoryId": self.expense}, {**fields, "reviewedRole": None}]
            else:
                invalid += [{**fields, "accountId": self.external}, {**fields, "accountId": 99999}, {**fields, "categoryId": 99999}, {**fields, "reviewedRole": "income"}, {**fields, "fromAccountId": self.cash}]
            for replacement in [None, [], {}, *invalid]:
                before = self.snapshot()
                response = self.updateThroughIntegration(proposal["id"], replacement)
                self.assertIn(response.status_code, {400, 409}, response.get_json())
                self.assertIsInstance(response.get_json()["error"], str)
                self.assertEqual(before, self.snapshot())
        fields = self.integrationUpdateFields(self.integrationPendingRow(ordinary["id"]))
        with self.connection:
            self.connection.execute("UPDATE categories SET reportingRole = 'income' WHERE id = ?", (self.expense,))
        self.assertEqual(409, self.updateThroughIntegration(ordinary["id"], fields).status_code)
        current = self.integrationUpdateFields(self.integrationPendingRow(ordinary["id"]))
        self.assertEqual(200, self.updateThroughIntegration(ordinary["id"], current).status_code)
        self.assertTrue(self.integrationPendingRow(ordinary["id"])["roleChanged"])
        with self.connection:
            self.connection.execute("UPDATE accounts SET active = 0 WHERE id = ?", (self.cash,))
        before = self.snapshot()
        self.assertEqual(400, self.updateThroughIntegration(ordinary["id"], self.integrationUpdateFields(self.integrationPendingRow(ordinary["id"]))).status_code)
        self.assertEqual(before, self.snapshot())

    def testIntegrationPendingPermissionsMissingTerminalAndRollback(self):
        proposal = self.submit(self.ordinary)
        fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        path = f"/api/integration/submissions/{proposal['id']}"
        auditor = self.app.test_client()
        self.assertEqual(302, signIn(auditor, "Auditor", "auditor password").status_code)
        for client in (self.app.test_client(), self.owner, auditor):
            self.assertEqual(401, client.get("/api/integration/pending").status_code)
            self.assertEqual(401, client.put(path, json=fields).status_code)
        self.assertEqual(403, self.agent.patch(f"/api/pending/{proposal['id']}", headers=self.headers, json=fields).status_code)
        self.assertEqual(404, self.updateThroughIntegration(99999, fields).status_code)
        self.assertEqual(400, self.agent.get("/api/integration/pending?limit=1", headers=self.headers).status_code)
        self.assertEqual(400, self.agent.put(path + "?ignored=1", headers=self.headers, json=fields).status_code)
        for method in ("delete", "patch", "post"):
            before = self.snapshot()
            self.assertEqual(405, getattr(self.agent, method)(path, headers=self.headers, json=fields).status_code)
            self.assertEqual(before, self.snapshot())
        with self.connection:
            self.connection.execute("CREATE TRIGGER failPendingUpdate BEFORE UPDATE OF reviewContent ON pendingSubmissions BEGIN SELECT RAISE(ABORT, 'Injected update failure'); END")
        before = self.snapshot()
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.assertEqual(500, self.updateThroughIntegration(proposal["id"], {**fields, "description": "Failed"}).status_code)
        self.assertEqual(before, self.snapshot())
        with self.connection:
            self.connection.execute("DROP TRIGGER failPendingUpdate")
        ledger.decidePendingTransactions(self.connection, "reject", self.rows())
        self.assertEqual(409, self.updateThroughIntegration(proposal["id"], fields).status_code)
        self.assertEqual(0, self.agent.get("/api/integration/pending", headers=self.headers).get_json()["count"])
        with self.connection:
            self.connection.execute("UPDATE integrationSecrets SET active = 0 WHERE id = ?", (self.integration,))
        self.assertEqual(401, self.agent.get("/api/integration/pending", headers=self.headers).status_code)
        self.assertEqual(401, self.updateThroughIntegration(proposal["id"], fields).status_code)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "backups").mkdir()
            unconfigured = createApp(Path(directory) / "unconfigured.db").test_client()
            self.assertEqual(503, unconfigured.get("/api/integration/pending", headers=self.headers).status_code)
            self.assertEqual(503, unconfigured.put(path, headers=self.headers, json=fields).status_code)

    def testIntegrationPendingConcurrentSaveAndDecision(self):
        for competitor in ("owner", "integration", "approve", "reject"):
            proposal = self.submit({**self.ordinary, "submissionId": competitor})
            fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
            barrier = threading.Barrier(2)
            def updateFromAgent():
                barrier.wait()
                return self.app.test_client().put(f"/api/integration/submissions/{proposal['id']}", headers=self.headers, json={**fields, "description": "Agent"}).status_code
            def updateCompeting():
                connection = ledger.connectDatabase(self.path)
                try:
                    barrier.wait()
                    if competitor in {"approve", "reject"}:
                        ledger.decidePendingTransactions(connection, competitor, [{"id": proposal["id"], **fields}])
                    else:
                        ledger.savePendingReview(connection, proposal["id"], {**fields, "description": "Competing"}, integrationUpdate=competitor == "integration")
                    return 200
                except ledger.SubmissionConflict:
                    return 409
                finally:
                    connection.close()
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = [executor.submit(updateFromAgent), executor.submit(updateCompeting)]
                self.assertEqual([200, 409], sorted(future.result() for future in outcomes))
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])


    def assertPendingContractShape(self, value, shape, contract):
        if "$ref" in shape:
            shape = contract["components"]["schemas"][shape["$ref"].split("/")[-1]]
        if "oneOf" in shape:
            matches = 0
            for branch in shape["oneOf"]:
                try:
                    self.assertPendingContractShape(value, branch, contract)
                    matches += 1
                except AssertionError:
                    pass
            self.assertEqual(1, matches, value)
            return
        types = shape.get("type", [])
        if isinstance(types, str):
            types = [types]
        expected = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
        if types:
            self.assertIn(type(value), [expected[kind] for kind in types], value)
        if "const" in shape:
            self.assertEqual(shape["const"], value)
        if "enum" in shape:
            self.assertIn(value, shape["enum"])
        if isinstance(value, dict):
            properties = shape.get("properties", {})
            self.assertTrue(set(shape.get("required", [])).issubset(value), value)
            if shape.get("additionalProperties") is False:
                self.assertTrue(set(value).issubset(properties), value)
            for name, item in value.items():
                if name in properties:
                    self.assertPendingContractShape(item, properties[name], contract)
        if isinstance(value, list):
            for item in value:
                self.assertPendingContractShape(item, shape["items"], contract)
        if type(value) is int and "minimum" in shape:
            self.assertGreaterEqual(value, shape["minimum"])
        if isinstance(value, str) and "minLength" in shape:
            self.assertGreaterEqual(len(value), shape["minLength"])

    def testIntegrationPendingPublishedContractAndExamples(self):
        from packageApplication import applicationVersion
        contract = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual("26.10.18", applicationVersion)
        self.assertEqual(applicationVersion, contract["info"]["version"])
        pending = contract["paths"]["/api/integration/pending"]["get"]
        update = contract["paths"]["/api/integration/submissions/{proposalId}"]["put"]
        self.assertEqual({"put"}, set(contract["paths"]["/api/integration/submissions/{proposalId}"]))
        reviewShape = pending["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertPendingContractShape(self.agent.get("/api/integration/pending", headers=self.headers).get_json(), reviewShape, contract)
        examples = update["requestBody"]["content"]["application/json"]["examples"]
        for index, example in enumerate(examples.values()):
            proposal = self.submit({**(self.transfer if "fromAccountId" in example["value"] else self.ordinary), "submissionId": f"contract-{index}"})
            fields = dict(example["value"])
            if "fromAccountId" in fields:
                fields.update(fromAccountId=self.cash, toAccountId=self.card)
            else:
                fields.update(accountId=self.cash, categoryId=self.expense)
            self.assertPendingContractShape(fields, update["requestBody"]["content"]["application/json"]["schema"], contract)
            self.assertPendingContractShape(self.agent.get("/api/integration/pending", headers=self.headers).get_json(), reviewShape, contract)
            response = self.updateThroughIntegration(proposal["id"], fields)
            self.assertEqual(200, response.status_code)
            self.assertPendingContractShape(response.get_json(), update["responses"]["200"]["content"]["application/json"]["schema"], contract)
            self.assertPendingContractShape(self.agent.get("/api/integration/pending", headers=self.headers).get_json(), reviewShape, contract)
        statusShape = contract["paths"]["/api/integration/submissions"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertPendingContractShape(self.agent.get("/api/integration/submissions", headers=self.headers).get_json(), statusShape, contract)
        self.assertPendingContractShape(self.updateThroughIntegration(99999, fields).get_json(), contract["components"]["schemas"]["Error"], contract)

    def testIntegrationUpdateOwnerRestoredOriginalAndRevokedSource(self):
        proposal = self.submit(self.ordinary)
        fields = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        ledger.savePendingReview(self.connection, proposal["id"], {**fields, "description": "Owner edit"})
        restored = {**fields, "reviewVersion": 1}
        ledger.savePendingReview(self.connection, proposal["id"], restored)
        other = ledger.createIntegrationSecret(self.connection, "Active updater")
        secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (other,)).fetchone()[0]
        with self.connection:
            self.connection.execute("UPDATE integrationSecrets SET active = 0 WHERE id = ?", (self.integration,))
        self.headers = {"Authorization": "Bearer " + secret}
        current = self.integrationUpdateFields(self.integrationPendingRow(proposal["id"]))
        self.assertEqual({"reviewVersion": 3}, self.updateThroughIntegration(proposal["id"], {**current, "description": "API correction after restore"}).get_json())
        manualTransfer = self.submit({**self.transfer, "submissionId": "manual-transfer"}, manual=True)
        fields = self.integrationUpdateFields(self.integrationPendingRow(manualTransfer["id"]))
        self.assertEqual(200, self.updateThroughIntegration(manualTransfer["id"], {**fields, "description": "Manual transfer correction"}).status_code)
        self.assertEqual("Manual", self.integrationPendingRow(manualTransfer["id"])["label"])
