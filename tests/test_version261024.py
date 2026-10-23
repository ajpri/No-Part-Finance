"""All 26.10.24 fixtures are synthetic and isolated from the real installation."""
import tests

import concurrent.futures
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import zipfile

from app import createApp
import database as ledger
from platformConfiguration import ConfigurationError, OperationalLogs, configurationSample, loadConfiguration, synchronizeConfigurationFiles
from packageApplication import createApplicationPackage, includedFiles, applicationVersion
import users


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, content):
        (self.root / "platform.conf").write_text(content)
        return loadConfiguration(self.root)

    def testDefaultsCommentsAndSample(self):
        defaults = loadConfiguration(self.root)
        self.assertFalse(defaults["enforceEmailUsernames"])
        self.assertFalse(defaults["requireRegistrationVerification"])
        self.assertFalse(defaults["allowWebRegistration"])
        self.assertFalse(defaults["smtpEnabled"])
        self.assertEqual(10, defaults["smtpTimeoutSeconds"])
        self.assertEqual(587, defaults["smtpPort"])
        self.assertEqual(defaults, self.write(configurationSample()))
        self.assertTrue(self.write("# settings\n enforceEmailUsernames = TrUe \n")["enforceEmailUsernames"])
        self.assertEqual(465, self.write("smtpSecurity = tls\n")["smtpPort"])

    def testInvalidSettingsAreSanitized(self):
        for content in ("enforceEmailUsernames = SECRET", "enforceEmailUsernames = false\nenforceEmailUsernames = SECRET", "smtpPort SECRET", "smtpPort = 0", "smtpPort = 65536", "smtpTimeoutSeconds = 0", "smtpTimeoutSeconds = 1.5", "trustedProxyAddresses = localhost", "trustedProxyAddresses = 10.0.0.0/8", "smtpSecurity = SECRET", "smtpUsername = SECRET", "smtpPassword = SECRET", "smtpFromName = a\x00SECRET"):
            with self.subTest(content=content), self.assertRaises(ConfigurationError) as raised:
                self.write(content)
            self.assertNotIn("SECRET", str(raised.exception))

    def testStartupRecreatesAndRefreshesDefaultOnlySample(self):
        app = createApp(self.root / "synthetic.db")
        samplePath = self.root / "platform.conf.sample"
        self.assertEqual(configurationSample(), samplePath.read_text())
        self.assertFalse((self.root / "platform.conf").exists())
        samplePath.write_text("old sample")
        synchronizeConfigurationFiles(self.root, app.config["OPERATIONAL_LOGS"])
        self.assertEqual(configurationSample(), samplePath.read_text())
        samplePath.unlink()
        createApp(self.root / "synthetic.db")
        self.assertEqual(configurationSample(), samplePath.read_text())
        self.assertFalse((self.root / "platform.conf").exists())

    def testStartupPreservesValuesCommentsAndCommentedSettings(self):
        path = self.root / "platform.conf"
        original = "# My mail relay settings\nenforceEmailUsernames = TRUE\nallowWebRegistration = true\n# allowWebRegistration = false\nsmtpSecurity = tls\nsmtpUsername = relay-user\nsmtpPassword = CONFIG_SECRET=with#characters\n# smtpPort = 2525\n# smtpEnabled = true\n"
        path.write_text(original)
        expected = loadConfiguration(self.root)
        app = createApp(self.root / "synthetic.db")
        self.assertEqual(expected, app.config["PLATFORM_CONFIGURATION"])
        self.assertEqual(expected, loadConfiguration(self.root))
        updated = path.read_text()
        for line in original.splitlines():
            self.assertIn(line, updated)
        self.assertIn("# --- Email delivery (SMTP) ---", updated)
        self.assertIn("requireRegistrationVerification = false", updated)
        self.assertNotIn("CONFIG_SECRET", (self.root / "platform.conf.sample").read_text())
        self.assertNotIn("CONFIG_SECRET", (self.root / "logs/startup.log").read_text())
        before = path.stat().st_mtime_ns
        synchronizeConfigurationFiles(self.root, app.config["OPERATIONAL_LOGS"])
        self.assertEqual(updated, path.read_text())
        self.assertEqual(before, path.stat().st_mtime_ns)

    def testInvalidConfigurationIsNotRewrittenAtStartup(self):
        for original in ("smtpPort = SECRET", "allowWebRegistration = true\nallowWebRegistration = SECRET", "smtpPort SECRET"):
            path = self.root / "platform.conf"
            path.write_text(original)
            with self.assertRaises(ConfigurationError):
                createApp(self.root / "synthetic.db")
            self.assertEqual(original, path.read_text())
        self.assertFalse((self.root / "synthetic.db").exists())

    def testLastActiveAssignmentWinsBeforeAndAfterUpgrade(self):
        original = "# Keep my comment\nenforceEmailUsernames = false\nenforceEmailUsernames = TRUE\n# enforceEmailUsernames = false\nallowWebRegistration = true\nallowWebRegistration = false\nsmtpPort = invalid-earlier-value\nsmtpPort = 2525\n# smtpPort = 465\n"
        path = self.root / "platform.conf"
        path.write_text(original)
        expected = loadConfiguration(self.root)
        self.assertFalse(expected["allowWebRegistration"])
        self.assertTrue(expected["enforceEmailUsernames"])
        self.assertEqual(2525, expected["smtpPort"])
        createApp(self.root / "synthetic.db")
        self.assertEqual(expected, loadConfiguration(self.root))
        updated = path.read_text()
        self.assertIn("# Keep my comment", updated)
        self.assertIn("# Earlier duplicate (inactive): allowWebRegistration = true", updated)
        self.assertIn("# Earlier duplicate (inactive): smtpPort = invalid-earlier-value", updated)
        self.assertIn("# smtpPort = 465", updated)
        createApp(self.root / "synthetic.db")
        self.assertEqual(updated, path.read_text())

    def testExtraDataIsPreservedIgnoredAndNeverCopiedToSampleOrLogs(self):
        original = "# Old note\nunknownSetting = CONFIG_SECRET\nold free-form text\nallowWebRegistration = true\n# --- Extra data (ignored) ---\n# Keep this extra comment\nallowWebRegistration = false\nfutureOptionalFeature = true\n"
        path = self.root / "platform.conf"
        path.write_text(original)
        app = createApp(self.root / "synthetic.db")
        self.assertTrue(app.config["PLATFORM_CONFIGURATION"]["allowWebRegistration"])
        updated = path.read_text()
        main, extra = updated.split("# --- Extra data (ignored) ---")
        self.assertNotIn("CONFIG_SECRET", main)
        for line in ("unknownSetting = CONFIG_SECRET", "old free-form text", "# Keep this extra comment", "allowWebRegistration = false", "futureOptionalFeature = true"):
            self.assertIn(line, extra)
        self.assertIn("# Old note", updated)
        self.assertNotIn("CONFIG_SECRET", (self.root / "platform.conf.sample").read_text())
        self.assertNotIn("CONFIG_SECRET", (self.root / "logs/startup.log").read_text())
        createApp(self.root / "synthetic.db")
        self.assertEqual(updated, path.read_text())
        # A future version recognizing an old extra must not silently enable it.
        with patch.dict("platformConfiguration.configurationDefaults", {"futureOptionalFeature": False}):
            app = createApp(self.root / "synthetic.db")
            self.assertFalse(app.config["PLATFORM_CONFIGURATION"]["futureOptionalFeature"])
            self.assertFalse(loadConfiguration(self.root)["futureOptionalFeature"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def testSynchronizationPreservesPrivatePermissions(self):
        path = self.root / "platform.conf"
        path.write_text("enforceEmailUsernames = true")
        path.chmod(0o600)
        createApp(self.root / "synthetic.db")
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def testFailedUpdatesLeaveOriginalFilesAndDoNotBlockStartup(self):
        original = "allowWebRegistration = true\n"
        path = self.root / "platform.conf"
        path.write_text(original)
        with patch("platformConfiguration.updateConfigurationFile", side_effect=PermissionError("SECRET")):
            app = createApp(self.root / "synthetic.db")
        self.assertTrue(app.config["PLATFORM_CONFIGURATION"]["allowWebRegistration"])
        self.assertEqual(original, path.read_text())
        self.assertFalse(list(self.root.glob("*.tmp")))
        logs = (self.root / "logs/startup.log").read_text()
        self.assertIn("Could not update platform.conf", logs)
        self.assertNotIn("SECRET", logs)

    @unittest.skipIf(os.name == "nt", "POSIX replacement")
    def testInterruptedReplacementPreservesRuntimeFile(self):
        path = self.root / "platform.conf"
        original = "enforceEmailUsernames = true\n"
        path.write_text(original)
        with patch("platformConfiguration.os.replace", side_effect=OSError("Interrupted")):
            app = createApp(self.root / "synthetic.db")
        self.assertTrue(app.config["PLATFORM_CONFIGURATION"]["enforceEmailUsernames"])
        self.assertEqual(original, path.read_text())
        self.assertFalse(list(self.root.glob("*.tmp")))

    @unittest.skipIf(os.name == "nt", "POSIX symbolic links")
    def testConfigurationLinksAreReadButNeverRewritten(self):
        target = self.root / "private-config.txt"
        original = "enforceEmailUsernames = true\n"
        target.write_text(original)
        (self.root / "platform.conf").symlink_to(target)
        app = createApp(self.root / "synthetic.db")
        self.assertTrue(app.config["PLATFORM_CONFIGURATION"]["enforceEmailUsernames"])
        self.assertEqual(original, target.read_text())
        self.assertTrue((self.root / "platform.conf").is_symlink())

    def testVerificationDependenciesFailAtStartup(self):
        for content in ("requireRegistrationVerification = true", "requireRegistrationVerification = true\nsmtpEnabled = true", "smtpEnabled = true", "requireRegistrationVerification = true\nenforceEmailUsernames = true"):
            with self.subTest(content=content), self.assertRaises(ConfigurationError):
                self.write(content)
        configuration = self.write("requireRegistrationVerification = true\nenforceEmailUsernames = true\nsmtpEnabled = true\nsmtpHost = mail.example.test\nsmtpFromAddress = sender@example.test")
        self.assertTrue(configuration["requireRegistrationVerification"])
        self.assertFalse(configuration["allowWebRegistration"])

    def testLogsArePlainBoundedAndDated(self):
        logs = OperationalLogs(self.root)
        for name in ("startup", "email", "userManagement"):
            logs.write(name, "A readable result.")
            text = (self.root / "logs" / f"{name}.log").read_text()
            self.assertRegex(text, r"\d{4}-\d\d-\d\d .* [+-]\d{4} A readable result")
            handler = logs.loggers[name].handlers[0]
            self.assertEqual(3, handler.backupCount)
            self.assertEqual(1024 * 1024, handler.maxBytes)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "synthetic.db"
        self.app = createApp(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.connection.configuration = self.app.config["PLATFORM_CONFIGURATION"]
        self.connection.operationalLogs = self.app.config["OPERATIONAL_LOGS"]
        self.owner = users.createOwner(self.connection, "Owner", "owner password", "Jane", "Smith")
        self.client = self.app.test_client()

    def configure(self, content):
        (self.root / "platform.conf").write_text(content)
        self.app = createApp(self.path)
        self.connection.configuration = self.app.config["PLATFORM_CONFIGURATION"]
        self.connection.operationalLogs = self.app.config["OPERATIONAL_LOGS"]
        self.client = self.app.test_client()

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


class ProxyAndSmtpTests(unittest.TestCase):
    setUp = IdentityTests.setUp
    configure = IdentityTests.configure

    def testProxyTrustedRightmostSchemeAndWarnings(self):
        self.configure("trustedProxyAddresses = 10.40.1.2, 2001:db8::1")
        client = self.app.test_client()
        page = client.get("/sign-in", headers={"X-Forwarded-For": "spoofed, 198.51.100.2", "X-Forwarded-Proto": "https"}, environ_overrides={"REMOTE_ADDR": "10.40.1.2"})
        self.assertIn("Secure;", page.headers["Set-Cookie"])
        untrusted = client.get("/sign-in", headers={"X-Forwarded-For": "bad", "X-Forwarded-Proto": "https"}, environ_overrides={"REMOTE_ADDR": "10.40.1.3"})
        self.assertNotIn("Secure;", untrusted.headers["Set-Cookie"])
        for header in ({"X-Forwarded-For": "bad"}, {"X-Forwarded-Proto": "bad"}):
            self.assertEqual(400, client.get("/sign-in", headers=header, environ_overrides={"REMOTE_ADDR": "10.40.1.2"}).status_code)
        for _ in range(3):
            self.assertEqual(200, client.get("/sign-in", environ_overrides={"REMOTE_ADDR": "10.40.1.2"}).status_code)
        warnings = (self.root / "logs/startup.log").read_text()
        self.assertEqual(1, warnings.count("X-Forwarded-For is missing"))
        self.assertEqual(1, warnings.count("X-Forwarded-Proto is missing"))

    def testSmtpBothTransportsRelayCredentialsAndFailures(self):
        from platformConfiguration import configurationDefaults
        configuration = {**configurationDefaults, "smtpEnabled": True, "smtpHost": "mail.example.test", "smtpPort": 587, "smtpFromAddress": "sender@example.test"}
        for transport in ("starttls", "tls"):
            for authenticated in (False, True):
                configuration.update(smtpSecurity=transport, smtpUsername="smtp-user" if authenticated else "", smtpPassword="smtp-secret" if authenticated else "")
                with patch("smtplib.SMTP") as plain, patch("smtplib.SMTP_SSL") as secure:
                    constructor = plain if transport == "starttls" else secure
                    result = users.sendVerificationEmail(configuration, "recipient@example.test", "000042", "registration", self.connection.operationalLogs)
                    self.assertIn("sent", result)
                    self.assertEqual(10, constructor.call_args.kwargs["timeout"])
                    connection = constructor.return_value
                    self.assertEqual(authenticated, connection.login.called)
                    self.assertEqual(transport == "starttls", connection.starttls.called)
                    message = connection.send_message.call_args.args[0]
                    self.assertEqual("No Part Finance verification code", message["Subject"])
                    self.assertNotIn("000042", message["Subject"])
                    self.assertIn("000042", message.get_content())
                    self.assertEqual("text/plain", message.get_content_type())
        with patch("smtplib.SMTP", side_effect=TimeoutError("SECRET 000042 smtp-secret")):
            configuration["smtpSecurity"] = "starttls"
            self.assertIn("couldn’t confirm", users.sendVerificationEmail(configuration, "recipient@example.test", "000042", "registration", self.connection.operationalLogs))
        logs = (self.root / "logs/email.log").read_text()
        for secret in ("SECRET", "000042", "smtp-secret", "recipient@example.test"):
            self.assertNotIn(secret, logs)
        configuration["smtpEnabled"] = False
        with patch("smtplib.SMTP") as smtp, self.assertRaises(ledger.ValidationError):
            users.sendVerificationEmail(configuration, "recipient@example.test", "000042", "registration", None)
        smtp.assert_not_called()


class PackageAndMigrationTests(unittest.TestCase):
    setUp = IdentityTests.setUp

    def testSampleRegenerationAndPrivateCliFiles(self):
        repository = self.root / "repository"
        repository.mkdir()
        for relativePath in includedFiles:
            source = repository / relativePath
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("synthetic maintained fixture")
        (repository / "platform.conf").write_text("smtpPassword = CONFIG_SECRET")
        (repository / "logs").mkdir()
        (repository / "logs/email.log").write_text("PRIVATE_LOG")
        (repository / "logs/email.log.1").write_text("ROTATED_LOG")
        (repository / "logs/nested.zip").write_text("NESTED_SECRET")
        for serverFiles in (False, True):
            result = createApplicationPackage(repositoryPath=repository, outputPath=self.root / "packages", includeDatabase=False, includeServerFiles=serverFiles)
            with zipfile.ZipFile(result["archivePath"]) as archive:
                sample = archive.read("No-Part-Finance/platform.conf.sample").decode()
                self.assertEqual(configurationSample(), sample)
                self.assertNotIn("CONFIG_SECRET", sample)
                self.assertEqual(serverFiles, "No-Part-Finance/platform.conf" in archive.namelist())
                self.assertEqual(serverFiles, "No-Part-Finance/logs/email.log.1" in archive.namelist())
                self.assertFalse(any("nested.zip" in name for name in archive.namelist()))
                if not serverFiles:
                    for name in archive.namelist():
                        self.assertNotIn(b"CONFIG_SECRET", archive.read(name))
                        self.assertNotIn(b"PRIVATE_LOG", archive.read(name))

    def testSchema27MigrationPreservesIdentitiesFinancialHistoryAndBackup(self):
        path = self.root / "legacy27.db"
        with patch.object(ledger, "schemaVersion", 27):
            ledger.initializeDatabase(path)
        connection = ledger.connectInitialLedgerDatabase(path)
        with connection:
            userId = connection.execute("INSERT INTO users (username, usernameKey, passwordHash, authVersion) VALUES ('Legacy', 'legacy', ?, 7)", (users.passwordHash("legacy password"),)).lastrowid
            connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, currentLedgerId(), 'owner')", (userId,))
        account = ledger.createAccount(connection, "Cash", "cash", openingBalance="123.45")
        category = ledger.createCategory(connection, "Expense")
        ledger.createTransaction(connection, account, "2026-10-23", "Historical", category, "1.23")
        before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] for table in ("transactions", "accounts", "ledgers", "ledgerAccess", "auditEvents")}
        oldUser = tuple(connection.execute("SELECT * FROM users").fetchone())
        connection.close()
        ledger.initializeDatabase(path)
        connection = ledger.connectInitialLedgerDatabase(path)
        for table, expected in before.items():
            self.assertEqual(expected, [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")])
        self.assertEqual(oldUser, tuple(connection.execute("SELECT * FROM users").fetchone())[:len(oldUser)])
        self.assertEqual("accepted", connection.execute("SELECT verificationStatus FROM users").fetchone()[0])
        self.assertEqual("", connection.execute("SELECT firstName FROM users").fetchone()[0])
        connection.close()
        self.assertTrue(list((self.root / "backups").glob("pre-migration-v27-to-v28-*.db")))
        ledger.initializeDatabase(path)

    def testExportAcceptanceUnchangedAndCredentialsExcluded(self):
        with self.connection:
            self.connection.execute("UPDATE users SET verificationStatus='pending', acceptanceMethod='awaiting-email', registrationCreatedAt=2000000000, registrationExpiresAt=2000086400 WHERE id=?", (self.owner,))
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM users")]
        exported = self.root / "export.db"
        ledger.exportLedgerDatabase(self.connection, exported)
        copy = sqlite3.connect(exported)
        self.assertEqual(("pending", "awaiting-email", "", 1), copy.execute("SELECT verificationStatus, acceptanceMethod, passwordHash, passwordSetupRequired FROM users").fetchone())
        self.assertEqual([], copy.execute("PRAGMA foreign_key_check").fetchall())
        copy.close()
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM users")])
