"""Recovery browser flows use synthetic databases and captured email only."""
import tests
from tests.test_support import RecoveryFixture
import sqlite3
import unittest

from platformConfiguration import ConfigurationError, loadConfiguration


class RecoveryRouteTests(RecoveryFixture, unittest.TestCase):
    def testConfigurationDependenciesAndSafeErrors(self):
        other = self.root / "configuration"
        other.mkdir()
        self.assertFalse(loadConfiguration(other)["allowWebPasswordRecovery"])
        for setting in ("allowWebPasswordRecovery = true\n", "allowWebPasswordRecovery = true\nsmtpEnabled = true\nsmtpHost = smtp.example.test\nsmtpFromAddress = finance@example.test\n", "publicApplicationUrl = http://finance.example.test\n", "publicApplicationUrl = https://127.0.0.1\n", "publicApplicationUrl = https://secret:private@finance.example.test\n"):
            other.joinpath("platform.conf").write_text(setting)
            with self.assertRaises(ConfigurationError) as raised:
                loadConfiguration(other)
            self.assertNotIn("secret:private", str(raised.exception))

    def testDisabledAndCsrfProtection(self):
        self.assertEqual(403, self.client.post("/forgot-password", data={"username": "owner@example.test"}).status_code)
        self.assertEqual(403, self.client.post("/reset-password", data={"token": "x" * 43}).status_code)
        self.application.config["PLATFORM_CONFIGURATION"]["allowWebPasswordRecovery"] = False
        self.assertNotIn('href="/forgot-password"', self.client.get("/sign-in").get_data(as_text=True))
        for route in ("/forgot-password", "/reset-password"):
            self.assertEqual(403, self.client.get(route).status_code)
            self.assertEqual(403, self.client.post(route, data={}).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM passwordRecoveryAttempts").fetchone()[0])

    def testGenericResponseAndNoTokenInHttpUrls(self):
        token, response = self.issue()
        self.assertNotIn(token, response.get_data(as_text=True))
        csrfToken = self.csrf(self.client, "/forgot-password")
        unknown = self.client.post("/forgot-password", data={"username": "unknown@example.test", "csrfToken": csrfToken})
        self.assertEqual(response.get_data(as_text=True), unknown.get_data(as_text=True))
        self.assertEqual("no-store", response.headers["Cache-Control"])
        reset = self.client.get("/reset-password")
        self.assertNotIn(token, reset.get_data(as_text=True))

    def testRecoveryInvalidatesSessionsAndRequiresFreshSignIn(self):
        owner = self.login()
        token, _ = self.issue()
        csrfToken = self.csrf(self.client, "/reset-password")
        response = self.client.post("/reset-password", data={"csrfToken": csrfToken, "token": token, "newPassword": "new recovery passphrase", "confirmation": "new recovery passphrase"})
        self.assertEqual(200, response.status_code)
        self.assertIn("Sign In", response.get_data(as_text=True))
        self.assertEqual(401, owner.get("/api/session").status_code)
        self.assertEqual(401, self.client.get("/api/session").status_code)
        csrfToken = self.csrf(self.client, "/sign-in")
        self.assertEqual(302, self.client.post("/sign-in", data={"csrfToken": csrfToken, "username": "owner@example.test", "password": "new recovery passphrase"}).status_code)

    def testValidationRetryPreservesHiddenTokenAndSingleUse(self):
        token, _ = self.issue()
        csrfToken = self.csrf(self.client, "/reset-password")
        response = self.client.post("/reset-password", data={"csrfToken": csrfToken, "token": token, "newPassword": "password", "confirmation": "password"})
        self.assertEqual(400, response.status_code)
        self.assertIn(token, response.get_data(as_text=True))
        self.assertNotIn('value="password"', response.get_data(as_text=True))
        submitted = {"csrfToken": csrfToken, "token": token, "newPassword": "corrected passphrase", "confirmation": "corrected passphrase"}
        self.assertEqual(200, self.client.post("/reset-password", data=submitted).status_code)
        submitted["csrfToken"] = self.csrf(self.client, "/reset-password")
        self.assertEqual(400, self.client.post("/reset-password", data=submitted).status_code)

    def testBrowserExportOmitsRecoveryState(self):
        owner = self.login()
        token, _ = self.issue()
        response = owner.get("/api/backup")
        self.assertEqual(200, response.status_code)
        self.assertNotIn(token.encode(), response.get_data())
        exported = self.root / "exported.db"
        exported.write_bytes(response.get_data())
        response.close()
        with sqlite3.connect(exported) as connection:
            for table in ("recoveryEligibility", "passwordRecoveryLinks", "passwordRecoveryAttempts"):
                self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
