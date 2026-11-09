"""Account identity, registration, verification, and self-service behavior."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import IdentityFixture
import json
import re
import tempfile
import unittest
from pathlib import Path
import database as ledger
from app import createApp
from users import createUser, passwordHash
import concurrent.futures
import sqlite3
from unittest.mock import patch
from platformConfiguration import ConfigurationError, configurationSample, loadConfiguration
import users

class MyAccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.ledgerId = self.connection.ledgerId
        self.ownerId = createUser(self.connection, "owner", "Owner", "owner password", firstName="Original", lastName="Owner")
        self.auditorId = createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.application = createApp(self.path)

    def login(self, username="Owner", password="owner password", application=None, restricted=False):
        client = (application or self.application).test_client()
        page = client.get("/sign-in").get_data(as_text=True)
        csrfToken = re.search('name="csrfToken" value="([^"]+)"', page).group(1)
        response = client.post("/sign-in", data={"username": username, "password": password, "csrfToken": csrfToken})
        self.assertEqual(302, response.status_code, response.get_data(as_text=True))
        if not restricted:
            session = client.get("/api/session").get_json()
            client.environ_base["HTTP_X_CSRF_TOKEN"] = session["csrfToken"]
        return client

    def passwordChange(self, client, **overrides):
        submitted = {"currentPassword": "owner password", "newPassword": "a new private passphrase", "confirmation": "a new private passphrase"}
        submitted.update(overrides)
        return client.post("/api/account/password", json=submitted)

    def testConfigurationDefaultsAndDisabledEnforcement(self):
        root = Path(self.directory.name)
        self.assertTrue(loadConfiguration(root)["allowAccountSelfService"])
        self.assertIn("allowAccountSelfService = true", configurationSample())
        client = self.login()
        (root / "platform.conf").write_text("allowAccountSelfService = false\n")
        self.assertTrue(client.get("/api/account").get_json()["allowAccountSelfService"])
        disabled = createApp(self.path)
        client = self.login(application=disabled)
        identity = client.get("/api/account").get_json()
        self.assertFalse(identity["allowAccountSelfService"])
        self.assertEqual("Owner", identity["username"])
        self.assertEqual(403, client.put("/api/account/name", json={"firstName": "Denied", "lastName": ""}).status_code)
        self.assertEqual(403, self.passwordChange(client).status_code)
        self.assertEqual(200, client.get("/api/session").status_code)
        (root / "platform.conf").write_text("allowAccountSelfService = invalid\n")
        with self.assertRaises(ConfigurationError):
            loadConfiguration(root)

    def testReadOnlyIdentityAndNameValidation(self):
        client = self.login()
        identity = client.get("/api/account").get_json()
        self.assertEqual(self.ownerId, identity["userId"])
        self.assertEqual(("Owner", "Original", "Owner"), (identity["username"], identity["firstName"], identity["lastName"]))
        for firstName, lastName in ((None, ""), ("", ""), ("   ", ""), ("x" * 41, ""), ("Fine", "x" * 41), ("Line\nbreak", ""), ("Fine", 123)):
            with self.subTest(firstName=firstName, lastName=lastName):
                self.assertEqual(400, client.put("/api/account/name", json={"firstName": firstName, "lastName": lastName}).status_code)
        self.assertEqual(400, client.put("/api/account/name", json={"firstName": "Other", "lastName": "", "username": "Injected"}).status_code)
        self.assertEqual("Owner", client.get("/api/account").get_json()["username"])
        response = client.put("/api/account/name", json={"firstName": "  Zoë <safe>  ", "lastName": "  "})
        self.assertEqual(200, response.status_code)
        self.assertEqual("Zoë <safe>", response.get_json()["firstName"])
        self.assertEqual("", response.get_json()["lastName"])

    def testNameKeepsCurrentSessionAndExpiryInvalidatesOthers(self):
        client, other = self.login(), self.login()
        before = client.get("/api/session").get_json()
        cookie = client.get_cookie("npfSession").value
        response = client.put("/api/account/name", json={"firstName": "Changed", "lastName": "Name"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(before["csrfToken"], response.get_json()["csrfToken"])
        self.assertEqual(cookie, client.get_cookie("npfSession").value)
        after = client.get("/api/session").get_json()
        self.assertEqual(before["expiresAt"], after["expiresAt"])
        self.assertEqual(before["csrfToken"], after["csrfToken"])
        self.assertEqual(401, other.get("/api/session").status_code)

    def testPasswordValidationCurrentPasswordAndSessionContinuity(self):
        client, other = self.login(), self.login()
        before = client.get("/api/session").get_json()
        cookie = client.get_cookie("npfSession").value
        for invalid in ({"currentPassword": "wrong password"}, {"newPassword": "short", "confirmation": "short"}, {"newPassword": "password", "confirmation": "password"}, {"confirmation": "different"}, {"newPassword": "x" * 129, "confirmation": "x" * 129}, {"currentPassword": None}):
            with self.subTest(invalid=invalid):
                self.assertEqual(400, self.passwordChange(client, **invalid).status_code)
                self.assertEqual(200, other.get("/api/session").status_code)
        newPassword = "  a Unicode 密碼 passphrase  "
        response = self.passwordChange(client, newPassword=newPassword, confirmation=newPassword)
        self.assertEqual(200, response.status_code)
        self.assertEqual(before["csrfToken"], response.get_json()["csrfToken"])
        self.assertEqual(cookie, client.get_cookie("npfSession").value)
        self.assertEqual(before["expiresAt"], client.get("/api/session").get_json()["expiresAt"])
        self.assertEqual(401, other.get("/api/session").status_code)
        self.assertEqual(200, self.login(password=newPassword).get("/api/account").status_code)
        auditText = json.dumps([dict(row) for row in self.connection.execute("SELECT * FROM userEvents")])
        self.assertNotIn(newPassword, auditText)
        self.assertNotIn("passwordHash", auditText)

    def testAllRolesAndEnabledUserWithoutLedger(self):
        with self.connection:
            memberId = self.connection.execute("INSERT INTO users (username,usernameKey,passwordHash,firstName) VALUES ('Member','member',?,'Member')", (passwordHash("member password"),)).lastrowid
            self.connection.execute("INSERT INTO ledgerAccess (userId,ledgerId,role) VALUES (?,?,'member')", (memberId, self.ledgerId))
            noLedgerId = self.connection.execute("INSERT INTO users (username,usernameKey,passwordHash,firstName) VALUES ('Unattached','unattached',?,'Unattached')", (passwordHash("unattached password"),)).lastrowid
        for username, password, userId in (("Owner", "owner password", self.ownerId), ("Auditor", "auditor password", self.auditorId), ("Member", "member password", memberId), ("Unattached", "unattached password", noLedgerId)):
            with self.subTest(username=username):
                client = self.login(username, password)
                self.assertEqual(userId, client.get("/api/account").get_json()["userId"])
                self.assertEqual(200, client.put("/api/account/name", json={"firstName": username, "lastName": "Updated"}).status_code)
                self.assertEqual(200, self.passwordChange(client, currentPassword=password).status_code)
                if userId == noLedgerId:
                    self.assertEqual(403, client.get("/api/accounts").status_code)

    def testAuthorizationCsrfAndRestrictedIdentity(self):
        client = self.login()
        signedOut = self.application.test_client()
        self.assertEqual(401, signedOut.get("/api/account").status_code)
        self.assertEqual(403, client.get("/api/account", headers={"Authorization": "Bearer not-browser-auth"}).status_code)
        client.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, client.put("/api/account/name", json={"firstName": "Denied", "lastName": ""}).status_code)
        self.assertEqual(403, self.passwordChange(client).status_code)
        with self.connection:
            self.connection.execute("UPDATE users SET firstName = '' WHERE id = ?", (self.ownerId,))
        self.assertEqual(403, client.get("/api/account").status_code)
        self.assertEqual(302, client.get("/settings").status_code)
        self.assertEqual("/complete-profile", client.get("/settings").headers["Location"])

    def testAuditFailureRollsBackNameAndPasswordAndSessionInvalidation(self):
        client, other = self.login(), self.login()
        before = dict(self.connection.execute("SELECT * FROM users WHERE id = ?", (self.ownerId,)).fetchone())
        beforeEvents = self.connection.execute("SELECT COUNT(*) FROM userEvents").fetchone()[0]
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectAccountAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'users' BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        for requestChange in (lambda: client.put("/api/account/name", json={"firstName": "Unsaved", "lastName": ""}), lambda: self.passwordChange(client)):
            self.assertEqual(500, requestChange().status_code)
            self.assertEqual(before, dict(self.connection.execute("SELECT * FROM users WHERE id = ?", (self.ownerId,)).fetchone()))
            self.assertEqual(beforeEvents, self.connection.execute("SELECT COUNT(*) FROM userEvents").fetchone()[0])
            self.assertEqual(200, client.get("/api/session").status_code)
            self.assertEqual(200, other.get("/api/session").status_code)

    def testNameChangePreservesLedgerHistoryAndMemberships(self):
        client = self.login()
        ledger.bindLedger(self.connection, self.ledgerId, "user", self.ownerId, "Owner")
        accountId = ledger.createAccount(self.connection, "Cash", "cash", openingBalance="100")
        categoryId = ledger.createCategory(self.connection, "Expense")
        ledger.createTransaction(self.connection, accountId, "2026-10-01", "Historical entry", categoryId, "12.34")
        preserved = {table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in ("ledgers", "ledgerAccess", "accounts", "categories", "transactions", "auditEvents")}
        self.assertEqual(200, client.put("/api/account/name", json={"firstName": "Changed", "lastName": "Owner"}).status_code)
        for table in ("ledgers", "ledgerAccess", "accounts", "categories", "transactions"):
            self.assertEqual(preserved[table], [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")])
        currentEvents = [tuple(row) for row in self.connection.execute("SELECT * FROM auditEvents ORDER BY id")]
        self.assertEqual(preserved["auditEvents"], currentEvents[:len(preserved["auditEvents"])])

    def testPendingAndDisabledUsersCannotUseAccountUpdates(self):
        client = self.login()
        with self.connection:
            self.connection.execute("UPDATE users SET enabled = 0 WHERE id = ?", (self.ownerId,))
        self.assertEqual(401, client.get("/api/account").status_code)
        self.assertEqual(401, client.put("/api/account/name", json={"firstName": "Denied", "lastName": ""}).status_code)
        with self.connection:
            self.connection.execute("UPDATE users SET enabled = 1, verificationStatus = 'pending' WHERE id = ?", (self.ownerId,))
        self.assertEqual(403, client.get("/api/account").status_code)
        self.assertEqual(403, self.passwordChange(client).status_code)

    def testTrustedCliCanStillUpdateDisabledOrSetupRequiredProfiles(self):
        from users import updateProfile
        for enabled, setupRequired in ((0, 0), (1, 1)):
            with self.connection:
                self.connection.execute("UPDATE users SET enabled = ?, passwordSetupRequired = ? WHERE id = ?", (enabled, setupRequired, self.auditorId))
            updateProfile(self.connection, self.auditorId, "Updated through CLI", "")
            user = self.connection.execute("SELECT firstName, enabled, passwordSetupRequired FROM users WHERE id = ?", (self.auditorId,)).fetchone()
            self.assertEqual(("Updated through CLI", enabled, setupRequired), tuple(user))

class IdentityTests(IdentityFixture, unittest.TestCase):
    def enableVerification(self):
        self.configure("allowWebRegistration = true\nenforceEmailUsernames = true\nrequireRegistrationVerification = true\nsmtpEnabled = true\nsmtpHost = smtp.example.test\nsmtpFromAddress = sender@example.test")

    def csrf(self, client=None, page="/sign-in"):
        html = (client or self.client).get(page).text
        return re.search(r'name="csrfToken" value="([^"]+)"', html).group(1)

    def login(self, username="Owner", password="owner password", client=None):
        client = client or self.client
        token = self.csrf(client)
        return client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token})

    def register(self, username="new@example.test", client=None):
        client = client or self.client
        token = self.csrf(client)
        with patch("users.sendVerificationEmail", return_value="Verification email sent.") as send:
            response = client.post("/register", data={"username": username, "firstName": "James", "lastName": "Jones", "password": "independent password", "confirmation": "independent password", "csrfToken": token})
        code = send.call_args.args[2] if send.called else None
        return response, code

    def testNameAndEmailBoundaries(self):
        for username in ("a@b.co", "Jane@example.com", "ä@example.test"):
            self.assertEqual(username, users.validateUsername(username, True))
        for username in ("a@b.c", "a@b", "@b.co", "a@@b.co", "a @b.co", "a@b..co", "a@.co", "a@co."):
            with self.subTest(username=username), self.assertRaises(ledger.ValidationError):
                users.validateUsername(username, True)
        self.assertEqual("x", users.validateUsername(" x ", False))
        for firstName, lastName, expected in (("Jane", "Smith", "Jane Smith’s Finances"), ("James", "", "James’ Finances"), ("James", "Jones", "James Jones’ Finances"), ("Jane", "O’Neill", "Jane O’Neill’s Finances")):
            self.assertEqual(expected, users.defaultLedgerName(firstName, lastName))
        self.assertEqual(("Éva", ""), users.validateProfile(" Éva ", "  "))
        for firstName, lastName in (("", "X"), ("a" * 41, ""), ("Jane", "x" * 41), ("Jane\n", "Bad\x00")):
            with self.assertRaises(ledger.ValidationError):
                users.validateProfile(firstName, lastName)

    def testCommonPasswordsAndExistingHashes(self):
        for password in ("password", "Password123", "12345678"):
            with self.assertRaises(ledger.ValidationError):
                users.passwordHash(password)
        self.assertTrue(users.check_password_hash(users.passwordHash("  valid α password  "), "  valid α password  "))
        # A legacy weak password remains a valid sign-in credential.
        from werkzeug.security import generate_password_hash
        with self.connection:
            self.connection.execute("UPDATE users SET passwordHash = ? WHERE id = ?", (generate_password_hash("password", method="scrypt"), self.owner))
        self.assertEqual(302, self.login(password="password").status_code)

    def testRegistrationDisabledAndInitialOwnerGate(self):
        self.assertEqual(403, self.client.get("/register").status_code)
        self.assertNotIn("data-register", self.client.get("/sign-in").text)
        emptyPath = self.root / "unowned.db"
        (self.root / "platform.conf").write_text("allowWebRegistration = true")
        client = createApp(emptyPath).test_client()
        self.assertEqual(503, client.get("/register").status_code)
        self.assertEqual(503, client.post("/register", data={}).status_code)

    def testPlainRegistrationWelcomeAndExplicitLedgerChoice(self):
        self.configure("allowWebRegistration = true")
        response, _ = self.register()
        self.assertEqual("/welcome", response.location)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0])
        token = self.csrf(page="/welcome")
        self.assertEqual("/?createLedger=1", self.client.post("/welcome", data={"csrfToken": token, "choice": "yes"}).location)
        session = self.client.get("/api/session").json
        self.assertIsNone(session["ledgerId"])
        self.assertEqual("James Jones’ Finances", session["defaultLedgerName"])
        response = self.client.post("/api/ledgers", json={"name": session["defaultLedgerName"]}, headers={"X-CSRF-Token": session["csrfToken"]})
        self.assertEqual(201, response.status_code)
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0])

    def testValidationPreservesOnlySafeFieldsAndCsrf(self):
        self.configure("allowWebRegistration = true")
        token = self.csrf()
        fields = {"username": "New User", "firstName": "Jane", "lastName": "", "password": "private-password-marker", "confirmation": "different", "csrfToken": token}
        response = self.client.post("/register", data=fields)
        self.assertEqual(400, response.status_code)
        self.assertIn('value="New User"', response.text)
        self.assertNotIn("private-password-marker", response.text)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        self.assertEqual(403, self.client.post("/register", data={}).status_code)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM registrationAttempts").fetchone()[0])

    def testRegistrationLimitsPersistAndBlockedDoesNotHash(self):
        for index in range(5):
            users.admitRegistration(self.connection, "198.51.100.1", now=1000 + index)
        with patch("users.passwordHash", side_effect=AssertionError("must not hash")), self.assertRaises(users.IdentityRateLimit):
            users.admitRegistration(self.connection, "198.51.100.1", now=1006)
        self.assertEqual(5, self.connection.execute("SELECT COUNT(*) FROM registrationAttempts").fetchone()[0])
        users.admitRegistration(self.connection, "198.51.100.1", now=1600)
        self.assertEqual(6, self.connection.execute("SELECT COUNT(*) FROM registrationAttempts").fetchone()[0])
        self.app = createApp(self.path)
        self.assertEqual(6, self.connection.execute("SELECT COUNT(*) FROM registrationAttempts").fetchone()[0])
        for count in range(20):
            timestamp = 10000 + count * 601
            users.admitRegistration(self.connection, "daily", now=timestamp)
        with self.assertRaises(users.IdentityRateLimit):
            users.admitRegistration(self.connection, "daily", now=23000)

    def testPendingRestrictionsCodeAndSecrecy(self):
        self.enableVerification()
        with patch("users.secrets.randbelow", return_value=42):
            response, code = self.register()
        self.assertEqual("000042", code)
        self.assertEqual("/verify", response.location)
        user = self.connection.execute("SELECT * FROM users WHERE usernameKey = 'new@example.test'").fetchone()
        for route in ("/api/session", "/api/accounts", "/api/transactions", "/api/backup", "/api/ledgers", "/api/invitations"):
            with self.subTest(route=route):
                self.assertEqual(403, self.client.get(route).status_code)
        self.assertEqual(403, self.client.post("/api/ledgers", json={"name": "Escalation"}).status_code)
        challenge = self.connection.execute("SELECT * FROM verificationChallenges WHERE userId = ?", (user["id"],)).fetchone()
        self.assertNotIn(code, challenge["verifier"])
        self.assertTrue(users.check_password_hash(challenge["verifier"], code))
        token = self.csrf(page="/verify")
        self.assertIn('autocomplete="one-time-code"', self.client.get("/verify").text)
        self.assertEqual(403, self.client.post("/verify", data={"code": code}).status_code)
        self.assertEqual("/welcome", self.client.post("/verify", data={"code": code, "csrfToken": token}).location)
        self.assertEqual(200, self.client.get("/api/session").status_code)
        self.assertEqual("accepted", self.connection.execute("SELECT verificationStatus FROM users WHERE id = ?", (user["id"],)).fetchone()[0])
        audit = "\n".join(row[0] or "" for row in self.connection.execute("SELECT afterValues FROM userEvents"))
        self.assertNotIn(code, audit)
        self.assertNotIn(challenge["verifier"], audit)
        with self.assertRaises(ledger.ValidationError):
            users.verifyIdentityCode(self.connection, user["id"], code)

    def testExpiryResendCooldownAndRestart(self):
        self.enableVerification()
        _, code = self.register()
        userId = self.connection.execute("SELECT id FROM users WHERE usernameKey = 'new@example.test'").fetchone()[0]
        issued = self.connection.execute("SELECT issuedAt FROM verificationChallenges WHERE userId = ?", (userId,)).fetchone()[0]
        with self.assertRaises(users.IdentityRateLimit):
            users.issueVerification(self.connection, userId, "127.0.0.1", now=issued + 29)
        with patch("users.sendVerificationEmail", return_value="sent") as send:
            users.issueVerification(self.connection, userId, "127.0.0.1", now=issued + 30)
        newCode = send.call_args.args[2]
        if newCode != code:
            with self.assertRaises(ledger.ValidationError):
                users.verifyIdentityCode(self.connection, userId, code, now=issued + 31)
        with self.assertRaises(ledger.ValidationError):
            users.verifyIdentityCode(self.connection, userId, newCode, now=issued + 630)
        with patch("users.sendVerificationEmail", return_value="sent") as send:
            users.issueVerification(self.connection, userId, "127.0.0.1", now=issued + 631)
        newCode = send.call_args.args[2]
        wrong = "999999" if newCode != "999999" else "888888"
        for index in range(5):
            with self.assertRaises(ledger.ValidationError):
                users.verifyIdentityCode(self.connection, userId, wrong, now=issued + 632 + index)
        with self.assertRaises(users.IdentityRateLimit):
            users.issueVerification(self.connection, userId, "127.0.0.1", now=issued + 700)
        createApp(self.path)
        with self.assertRaises(users.IdentityRateLimit):
            users.verifyIdentityCode(self.connection, userId, newCode, now=issued + 700)

    def testRegistrationReservationExpiryNewIdentity(self):
        self.enableVerification()
        _, code = self.register()
        old = self.connection.execute("SELECT * FROM users WHERE usernameKey = 'new@example.test'").fetchone()
        deadline = old["registrationExpiresAt"]
        with self.assertRaises(ledger.ValidationError):
            users.registerUser(self.connection, old["username"], "Jane", "", "independent password", "independent password", now=deadline-1)
        with self.assertRaises(ledger.ValidationError):
            users.verifyIdentityCode(self.connection, old["id"], code, now=deadline)
        newId = users.registerUser(self.connection, old["username"], "Jane", "", "another independent password", "another independent password", now=deadline)
        self.assertNotEqual(old["id"], newId)
        self.assertTrue(self.connection.execute("SELECT registrationExpired FROM users WHERE id = ?", (old["id"],)).fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE userId = ?", (newId,)).fetchone()[0])
        self.assertIn(self.client.get("/api/session").status_code, (401, 403))  # stale session cannot gain access
        with self.assertRaises(ledger.ValidationError):
            users.acceptUserManually(self.connection, old["id"])

    def testExplicitDisableAcceptsPendingOnlyOnLogin(self):
        self.enableVerification()
        self.register()
        userId = self.connection.execute("SELECT id FROM users WHERE usernameKey = 'new@example.test'").fetchone()[0]
        self.configure("allowWebRegistration = true\nenforceEmailUsernames = true\nrequireRegistrationVerification = false")
        self.assertEqual("pending", self.connection.execute("SELECT verificationStatus FROM users WHERE id = ?", (userId,)).fetchone()[0])
        self.assertEqual(302, self.login("new@example.test", "independent password").status_code)
        self.assertEqual("configuration-policy-bypass", self.connection.execute("SELECT acceptanceMethod FROM users WHERE id = ?", (userId,)).fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM verificationChallenges WHERE userId = ?", (userId,)).fetchone()[0])

    def testProfileMigrationContinuityOtherSessionsAndLedgerPreservation(self):
        first = self.app.test_client()
        second = self.app.test_client()
        self.login(client=first)
        self.login(client=second)
        ledgerBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM ledgers")]
        with self.connection:
            self.connection.execute("UPDATE users SET firstName = '' WHERE id = ?", (self.owner,))
        self.assertEqual(403, first.get("/api/accounts").status_code)
        token = self.csrf(first, "/complete-profile")
        response = first.post("/complete-profile", data={"firstName": "James", "lastName": "", "csrfToken": token})
        self.assertEqual("/", response.location)
        self.assertEqual(200, first.get("/api/session").status_code)
        self.assertEqual(401, second.get("/api/session").status_code)
        self.assertEqual(ledgerBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM ledgers")])
        self.assertEqual("/", self.login(client=first).location)

    def testEmailMigrationWithRegistrationDisabled(self):
        self.enableVerification()
        self.configure((self.root / "platform.conf").read_text().replace("allowWebRegistration = true", "allowWebRegistration = false"))
        self.assertEqual("/complete-profile", self.login().location)
        token = self.csrf(page="/complete-profile")
        with patch("users.sendVerificationEmail", return_value="sent") as send:
            response = self.client.post("/complete-profile", data={"username": "owner@example.test", "firstName": "Jane", "lastName": "Smith", "csrfToken": token})
        code = send.call_args.args[2]
        self.assertEqual("/verify", response.location)
        self.assertEqual("Owner", self.connection.execute("SELECT username FROM users WHERE id = ?", (self.owner,)).fetchone()[0])
        self.assertEqual(403, self.client.get("/api/accounts").status_code)
        token = self.csrf(page="/verify")
        self.assertEqual("/", self.client.post("/verify", data={"code": code, "csrfToken": token}).location)
        self.assertEqual("owner@example.test", self.client.get("/api/session").json["username"])

    def testManualAcceptanceNoLedgerAndDisablement(self):
        self.enableVerification()
        self.register()
        userId = self.connection.execute("SELECT id FROM users WHERE usernameKey = 'new@example.test'").fetchone()[0]
        users.setUserEnabled(self.connection, userId, False)
        users.acceptUserManually(self.connection, userId)
        user = self.connection.execute("SELECT * FROM users WHERE id = ?", (userId,)).fetchone()
        self.assertFalse(user["enabled"])
        self.assertEqual("accepted", user["verificationStatus"])
        self.assertEqual("cli-manual-bypass", user["acceptanceMethod"])
        self.assertEqual(401, self.login("new@example.test", "independent password").status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE userId = ?", (userId,)).fetchone()[0])

    def testAtomicAccountAndAuditFailures(self):
        self.configure("allowWebRegistration = true")
        self.connection.execute("CREATE TRIGGER rejectUserAudit BEFORE INSERT ON userEvents BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            users.registerUser(self.connection, "rollback", "Jane", "", "independent password", "independent password")
        self.assertIsNone(self.connection.execute("SELECT id FROM users WHERE usernameKey = 'rollback'").fetchone())
        with self.assertRaises(sqlite3.IntegrityError):
            users.updateProfile(self.connection, self.owner, "Changed", "")
        self.assertEqual("Jane", self.connection.execute("SELECT firstName FROM users WHERE id = ?", (self.owner,)).fetchone()[0])

    def testConcurrentDuplicateRegistrations(self):
        self.configure("allowWebRegistration = true")
        configuration = self.connection.configuration
        def attempt():
            connection = ledger.connectDatabase(self.path)
            connection.configuration = configuration
            try:
                return users.registerUser(connection, "Race", "Jane", "", "independent password", "independent password")
            except ledger.ValidationError:
                return None
            finally:
                connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(1, sum(value is not None for value in results))

    def testConfigurationRestartOnlyAndNonDisclosure(self):
        (self.root / "platform.conf").write_text("allowWebRegistration = true")
        self.assertEqual(403, self.client.get("/register").status_code)
        self.assertEqual("/", self.login().location)
        for route in ("/platform.conf", "/platform.conf.sample", "/logs/email.log", "/static/platform.conf", "/assets/../platform.conf", "/api/platform.conf"):
            self.assertEqual(404, self.client.get(route).status_code)
        self.app = createApp(self.path)
        self.assertIn("data-register", self.app.test_client().get("/sign-in").text)


    def testVerificationAuditFailureRollsBackAcceptance(self):
        self.enableVerification()
        _, code = self.register()
        userId = self.connection.execute("SELECT id FROM users WHERE usernameKey = 'new@example.test'").fetchone()[0]
        before = tuple(self.connection.execute("SELECT verificationStatus, authVersion FROM users WHERE id = ?", (userId,)).fetchone())
        self.connection.execute("CREATE TRIGGER rejectVerificationAudit BEFORE INSERT ON userEvents BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            users.verifyIdentityCode(self.connection, userId, code)
        self.assertEqual(before, tuple(self.connection.execute("SELECT verificationStatus, authVersion FROM users WHERE id = ?", (userId,)).fetchone()))
        self.assertIsNone(self.connection.execute("SELECT consumedAt FROM verificationChallenges WHERE userId = ?", (userId,)).fetchone()[0])

    def testPolicyAcceptanceCannotAdoptConcurrentPasswordReset(self):
        self.enableVerification()
        self.register()
        userId = self.connection.execute("SELECT id FROM users WHERE usernameKey = 'new@example.test'").fetchone()[0]
        self.configure("requireRegistrationVerification = false")
        originalCheck = users.check_password_hash
        def checkThenReset(verifier, password):
            valid = originalCheck(verifier, password)
            users.resetPassword(self.connection, userId, "new independent password")
            return valid
        with patch("app.check_password_hash", side_effect=checkThenReset):
            self.assertEqual(401, self.login("new@example.test", "independent password").status_code)
        self.assertEqual("pending", self.connection.execute("SELECT verificationStatus FROM users WHERE id = ?", (userId,)).fetchone()[0])
        self.assertEqual(401, self.client.get("/api/session").status_code)

    def testConcurrentCodeConsumptionIsSingleUse(self):
        self.enableVerification()
        _, code = self.register()
        userId = self.connection.execute("SELECT id FROM users WHERE usernameKey = 'new@example.test'").fetchone()[0]
        def verify():
            connection = ledger.connectDatabase(self.path)
            connection.configuration = self.connection.configuration
            try:
                users.verifyIdentityCode(connection, userId, code)
                return True
            except ledger.ValidationError:
                return False
            finally:
                connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: verify(), range(2)))
        self.assertEqual(1, sum(results))
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM userEvents WHERE userId = ? AND action = 'email-verified'", (userId,)).fetchone()[0])

    def testIntegrationUnaffectedAndProfileCannotRenameCompliantUser(self):
        account = ledger.createAccount(self.connection, "Cash", "cash")
        secretId = ledger.createIntegrationSecret(self.connection, "Synthetic integration")
        secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (secretId,)).fetchone()[0]
        with self.connection:
            self.connection.execute("UPDATE users SET firstName = '' WHERE id = ?", (self.owner,))
        self.assertEqual("/complete-profile", self.login().location)
        self.assertEqual(403, self.client.get("/api/accounts").status_code)
        response = self.client.get("/api/integration/read/accounts", headers={"Authorization": "Bearer " + secret})
        self.assertEqual(200, response.status_code)
        self.assertIn(account, [row["id"] for row in response.json])
        token = self.csrf(page="/complete-profile")
        response = self.client.post("/complete-profile", data={"csrfToken": token, "username": "Unapproved rename", "firstName": "Jane", "lastName": ""})
        self.assertEqual("/", response.location)
        self.assertEqual("Owner", self.connection.execute("SELECT username FROM users WHERE id = ?", (self.owner,)).fetchone()[0])

    def testMigrationCompletionRechecksUsernameUniqueness(self):
        self.enableVerification()
        self.assertEqual("/complete-profile", self.login().location)
        token = self.csrf(page="/complete-profile")
        with patch("users.sendVerificationEmail", return_value="sent") as send:
            self.client.post("/complete-profile", data={"csrfToken": token, "username": "race@example.test", "firstName": "Jane", "lastName": ""})
        code = send.call_args.args[2]
        users.registerUser(self.connection, "race@example.test", "Other", "", "independent password", "independent password")
        token = self.csrf(page="/verify")
        self.assertEqual(400, self.client.post("/verify", data={"csrfToken": token, "code": code}).status_code)
        self.assertEqual("Owner", self.connection.execute("SELECT username FROM users WHERE id = ?", (self.owner,)).fetchone()[0])
        self.assertIsNone(self.connection.execute("SELECT consumedAt FROM verificationChallenges WHERE userId = ?", (self.owner,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
