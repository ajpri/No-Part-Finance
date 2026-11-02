"""Recovery uses synthetic guarded databases and fake SMTP exclusively."""
import tests

import hashlib
import json
import re
import smtplib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import database as ledger
from platformConfiguration import configurationDefaults
from users import (ValidationError, createUser, passwordHash, trustRecoveryEmail,
    issuePasswordRecovery, resetRecoveredPassword, passwordRecoveryResponse,
    renameUser, resetPassword, changeOwnPassword, saveIdentity, verifyIdentityCode,
    manageSelectedUser, manageIdentityWithoutLedger)
from werkzeug.security import check_password_hash


class PasswordRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.ownerId = createUser(self.connection, "owner", "owner@example.org", "owner password", firstName="Owner")
        self.ledgerId = self.connection.ledgerId
        self.configuration = {**configurationDefaults, "allowWebPasswordRecovery": True, "smtpEnabled": True,
            "smtpHost": "smtp.example.org", "smtpPort": 587, "smtpFromAddress": "finance@example.org",
            "publicApplicationUrl": "https://finance.example.org"}
        self.connection.configuration = self.configuration
        self.mailClient = Mock()
        self.smtpPatch = patch("smtplib.SMTP", return_value=self.mailClient)
        self.smtpPatch.start()
        self.addCleanup(self.smtpPatch.stop)
        self.now = 2000000000

    def issue(self, username="owner@example.org", address="192.0.2.1", now=None):
        self.assertEqual(passwordRecoveryResponse, issuePasswordRecovery(self.connection, username, address, self.now if now is None else now))
        if not self.mailClient.send_message.call_args:
            return None
        message = self.mailClient.send_message.call_args.args[0]
        return re.search(r"/reset-password#([A-Za-z0-9_-]{43})", message.get_content()).group(1)

    def trustAndIssue(self, now=None):
        trustRecoveryEmail(self.connection, self.ownerId)
        return self.issue(now=now)

    def consume(self, token, now=None, connection=None):
        return resetRecoveredPassword(connection or self.connection, token, "new Unicode 密碼 passphrase ", "new Unicode 密碼 passphrase ", self.now + 1 if now is None else now)

    def testGenericEligibilityAndExplicitCliTrust(self):
        for username in ("owner@example.org", "missing@example.org", "not-an-email"):
            self.issue(username, address=username)
        self.mailClient.send_message.assert_not_called()
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryAttempts").fetchone()[0])
        trustRecoveryEmail(self.connection, self.ownerId)
        eligibility = self.connection.execute("SELECT * FROM recoveryEligibility").fetchone()
        self.assertEqual((self.ownerId, "owner@example.org", "cli-trust"), tuple(eligibility))
        self.assertEqual("cli-bypass", self.connection.execute("SELECT acceptanceMethod FROM users WHERE id=?", (self.ownerId,)).fetchone()[0])
        token = self.issue(now=self.now + 30)
        self.assertEqual(43, len(token))
        message = self.mailClient.send_message.call_args.args[0]
        self.assertEqual("owner@example.org", message["To"])
        self.assertIn("https://finance.example.org/reset-password#", message.get_content())
        self.mailClient.starttls.assert_called_once()
        row = self.connection.execute("SELECT * FROM passwordRecoveryLinks").fetchone()
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), row["tokenDigest"])
        self.assertNotIn(token, json.dumps(dict(row)))
        self.assertEqual(900, row["expiresAt"] - row["issuedAt"])
        audit = json.dumps([dict(row) for row in self.connection.execute("SELECT * FROM userEvents")])
        self.assertIn("recovery-cli-trust", audit)
        self.assertNotIn(token, audit)

    def testEmailVerificationCreatesProofManualAcceptanceDoesNot(self):
        with self.connection:
            self.connection.execute("UPDATE users SET verificationStatus='pending' WHERE id=?", (self.ownerId,))
            self.connection.execute("INSERT INTO verificationChallenges VALUES (?, 'registration', ?, ?, ?, ?, NULL, 'Owner', '')", (self.ownerId, "owner@example.org", passwordHash("a fixture passphrase"), self.now, self.now + 600))
            # Verification code hashes do not use the ordinary minimum length rule.
            from werkzeug.security import generate_password_hash
            self.connection.execute("UPDATE verificationChallenges SET verifier=?", (generate_password_hash("123456", method="scrypt"),))
        verifyIdentityCode(self.connection, self.ownerId, "123456", self.now)
        self.assertEqual("email-code", self.connection.execute("SELECT method FROM recoveryEligibility").fetchone()[0])
        self.assertIsNotNone(self.issue())

    def testThirtySecondAndRollingAccountAndIpLimits(self):
        trustRecoveryEmail(self.connection, self.ownerId)
        for offset in (0, 30, 60, 90, 120):
            self.issue(now=self.now + offset)
        self.assertEqual(5, self.mailClient.send_message.call_count)
        self.issue(now=self.now + 121)
        self.issue(now=self.now + 150, address="different-ip")
        self.assertEqual(5, self.mailClient.send_message.call_count)
        self.issue(now=self.now + 600)
        self.assertEqual(6, self.mailClient.send_message.call_count)
        # Different account names cannot evade the same IP quota or cooldown.
        self.issue("missing@example.org", now=self.now + 610)
        self.assertEqual(6, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryAttempts").fetchone()[0])
        for index in range(14):
            self.issue(now=self.now + 1200 + index * 601)
        self.assertEqual(20, self.mailClient.send_message.call_count)
        self.issue(now=self.now + 20000, address="different-ip")
        self.assertEqual(20, self.mailClient.send_message.call_count)
        self.issue(now=self.now + 86400)
        self.assertEqual(21, self.mailClient.send_message.call_count)
        accountKeys = [row[0] for row in self.connection.execute("SELECT accountKey FROM passwordRecoveryAttempts")]
        self.assertTrue(all(len(value) == 64 for value in accountKeys))

    def testSupersededExactExpirySingleUseAndPasswordValidation(self):
        first = self.trustAndIssue()
        second = self.issue(now=self.now + 30)
        with self.assertRaises(ValidationError):
            self.consume(first, now=self.now + 31)
        for password, confirmation in (("password", "password"), ("short", "short"), ("a valid new password", "different")):
            with self.assertRaises(ValidationError):
                resetRecoveredPassword(self.connection, second, password, confirmation, self.now + 31)
        self.assertEqual(self.ownerId, self.consume(second, now=self.now + 929))
        self.assertTrue(check_password_hash(self.connection.execute("SELECT passwordHash FROM users WHERE id=?", (self.ownerId,)).fetchone()[0], "new Unicode 密碼 passphrase "))
        with self.assertRaises(ValidationError):
            self.consume(second, now=self.now + 929)
        third = self.issue(now=self.now + 1000)
        with self.assertRaises(ValidationError):
            self.consume(third, now=self.now + 1900)
        for malformed in (None, "", "x" * 42, "!" * 43):
            with self.assertRaises(ValidationError):
                self.consume(malformed)

    def testInvalidationByUsernameAndPasswordChanges(self):
        token = self.trustAndIssue()
        renameUser(self.connection, self.ownerId, "renamed@example.org")
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM recoveryEligibility").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryLinks").fetchone()[0])
        with self.assertRaises(ValidationError):
            self.consume(token)
        trustRecoveryEmail(self.connection, self.ownerId)
        self.issue("renamed@example.org", now=self.now + 30)
        resetPassword(self.connection, self.ownerId, "CLI reset password")
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryLinks").fetchone()[0])
        self.issue("renamed@example.org", now=self.now + 60)
        version = self.connection.execute("SELECT authVersion FROM users WHERE id=?", (self.ownerId,)).fetchone()[0]
        changeOwnPassword(self.connection, self.ownerId, "CLI reset password", "own changed password", "own changed password", version)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryLinks").fetchone()[0])
        self.issue("renamed@example.org", now=self.now + 90)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saveIdentity(self.connection, self.ownerId, "migrated@example.org", "Owner", "")
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM recoveryEligibility").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryLinks").fetchone()[0])

    def testResetAtomicAuditAndNotificationCleanupWithoutFinancialChanges(self):
        accountId = ledger.createAccount(self.connection, "Cash", "cash", openingBalance="123")
        categoryId = ledger.createCategory(self.connection, "Expense")
        ledger.createTransaction(self.connection, accountId, "2026-10-01", "Preserved history", categoryId, "12")
        financialSnapshot = {table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in ("ledgers", "ledgerAccess", "accounts", "categories", "transactions")}
        token = self.trustAndIssue()
        with self.connection:
            self.connection.execute("INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId) VALUES (?,?,'synthetic','{}',0)", (self.ownerId, self.ledgerId))
            self.connection.execute("CREATE TRIGGER rejectRecoveryAudit BEFORE INSERT ON auditEvents WHEN NEW.action='password-recovery' BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        before = dict(self.connection.execute("SELECT * FROM users WHERE id=?", (self.ownerId,)).fetchone())
        with self.assertRaises(sqlite3.IntegrityError):
            self.consume(token)
        self.assertEqual(before, dict(self.connection.execute("SELECT * FROM users WHERE id=?", (self.ownerId,)).fetchone()))
        self.assertIsNone(self.connection.execute("SELECT consumedAt FROM passwordRecoveryLinks").fetchone()[0])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM pushSubscriptions").fetchone()[0])
        with self.connection:
            self.connection.execute("DROP TRIGGER rejectRecoveryAudit")
        self.assertEqual(self.ownerId, self.consume(token))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM pushSubscriptions").fetchone()[0])
        self.assertEqual(before["authVersion"] + 1, self.connection.execute("SELECT authVersion FROM users WHERE id=?", (self.ownerId,)).fetchone()[0])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE userId=?", (self.ownerId,)).fetchone()[0])
        audit = json.dumps([dict(row) for row in self.connection.execute("SELECT * FROM userEvents")])
        self.assertNotIn(token, audit)
        self.assertNotIn("new Unicode", audit)
        self.assertNotIn("passwordHash", audit)
        for table, rows in financialSnapshot.items():
            self.assertEqual(rows, [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")])

    def testDisabledPendingAndNoLedgerProfileRestrictions(self):
        token = self.trustAndIssue()
        for field, value in (("enabled", 0), ("verificationStatus", "pending"), ("passwordSetupRequired", 1)):
            with self.connection:
                self.connection.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, self.ownerId))
            with self.assertRaises(ValidationError):
                self.consume(token)
            self.mailClient.send_message.reset_mock()
            self.issue(now=self.now + 30)
            self.mailClient.send_message.assert_not_called()
            with self.connection:
                self.connection.execute(f"UPDATE users SET {field}=? WHERE id=?", ("accepted" if field == "verificationStatus" else 1 if field == "enabled" else 0, self.ownerId))
        with self.connection:
            # A user lacking a profile/ledger remains subject to existing login gates.
            self.connection.execute("UPDATE users SET firstName='' WHERE id=?", (self.ownerId,))
            unattachedId = self.connection.execute("INSERT INTO users(username,usernameKey,passwordHash,firstName) VALUES ('unattached@example.org','unattached@example.org',?,'')", (passwordHash("unattached passphrase"),)).lastrowid
        trustRecoveryEmail(self.connection, unattachedId)
        unattachedToken = self.issue("unattached@example.org", address="other-ip", now=self.now + 60)
        self.assertEqual(unattachedId, self.consume(unattachedToken, now=self.now + 61))
        self.assertEqual("", self.connection.execute("SELECT firstName FROM users WHERE id=?", (unattachedId,)).fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE userId=?", (unattachedId,)).fetchone()[0])

    def testConcurrentSingleUseAndSanitizedSmtpFailure(self):
        token = self.trustAndIssue()
        def consumeOnce():
            connection = ledger.connectDatabase(self.path)
            connection.configuration = self.configuration
            try:
                self.consume(token, connection=connection)
                return True
            except ValidationError:
                return False
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual([False, True], sorted(executor.map(lambda index: consumeOnce(), range(2))))
        self.mailClient.send_message.side_effect = smtplib.SMTPAuthenticationError(535, b"sensitive provider response")
        self.assertEqual(passwordRecoveryResponse, issuePasswordRecovery(self.connection, "owner@example.org", "192.0.2.1", self.now + 30))

    def testCliTrustNeedsExplicitConfirmationAndEmailUsername(self):
        with patch("users.input", side_effect=["7", "n", "0"]), patch("builtins.print"):
            manageSelectedUser(self.connection, self.ownerId)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM recoveryEligibility").fetchone()[0])
        with patch("users.input", side_effect=["7", "y", "0"]), patch("builtins.print"):
            manageSelectedUser(self.connection, self.ownerId)
        self.assertEqual("cli-trust", self.connection.execute("SELECT method FROM recoveryEligibility").fetchone()[0])
        with self.connection:
            unattachedId = self.connection.execute("INSERT INTO users(username,usernameKey,passwordHash,firstName) VALUES ('unattached@example.org','unattached@example.org',?,'Unattached')", (passwordHash("unattached passphrase"),)).lastrowid
        with patch("users.input", side_effect=["7", "y", "0"]), patch("builtins.print"):
            manageIdentityWithoutLedger(self.connection, unattachedId)
        renameUser(self.connection, self.ownerId, "LocalUsername")
        with self.assertRaises(ValidationError):
            trustRecoveryEmail(self.connection, self.ownerId)

    def testDisabledRecoveryRejectsDirectCallsButCliRemains(self):
        token = self.trustAndIssue()
        self.configuration["allowWebPasswordRecovery"] = False
        with self.assertRaises(ValidationError):
            self.issue()
        with self.assertRaises(ValidationError):
            self.consume(token)
        resetPassword(self.connection, self.ownerId, "CLI reset remains available")
        trustRecoveryEmail(self.connection, self.ownerId)

    def testIpLimitsApplyAcrossDifferentSubmittedAccounts(self):
        trustRecoveryEmail(self.connection, self.ownerId)
        for index in range(5):
            self.issue(f"missing{index}@example.org", now=self.now + index * 30)
        self.issue(now=self.now + 150)
        self.mailClient.send_message.assert_not_called()
        self.assertEqual(5, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryAttempts").fetchone()[0])
        self.issue(now=self.now + 600)
        self.assertEqual(1, self.mailClient.send_message.call_count)
        for index in range(14):
            self.issue(f"other{index}@example.org", now=self.now + 1200 + index * 601)
        self.issue(now=self.now + 20000)
        self.assertEqual(1, self.mailClient.send_message.call_count)
        self.assertEqual(20, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryAttempts").fetchone()[0])
