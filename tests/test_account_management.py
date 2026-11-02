"""Own-account acceptance uses guarded synthetic databases and no financial source."""
import tests

import json
import re
import tempfile
import unittest
from pathlib import Path

import database as ledger
from app import createApp
from platformConfiguration import ConfigurationError, configurationSample, loadConfiguration
from users import createUser, passwordHash


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
