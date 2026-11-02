"""Recovery browser flows use synthetic databases and captured email only."""
import tests
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import createApp
from database import connectInitialLedgerDatabase
from platformConfiguration import ConfigurationError, loadConfiguration
from users import createUser, trustRecoveryEmail


class RecoveryRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "routes.db"
        self.root.joinpath("platform.conf").write_text("allowWebPasswordRecovery = true\nsmtpEnabled = true\nsmtpHost = smtp.example.test\nsmtpFromAddress = finance@example.test\npublicApplicationUrl = https://finance.example.test\n")
        self.application = createApp(self.path)
        self.connection = connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.connection.configuration = self.application.config["PLATFORM_CONFIGURATION"]
        self.userId = createUser(self.connection, "owner", "owner@example.test", "original passphrase", firstName="Owner")
        trustRecoveryEmail(self.connection, self.userId)
        self.client = self.application.test_client()

    def csrf(self, client, route):
        page = client.get(route).get_data(as_text=True)
        return re.search(r'name="csrfToken" value="([^"]+)"', page).group(1)

    def login(self):
        client = self.application.test_client()
        csrfToken = self.csrf(client, "/sign-in")
        self.assertEqual(302, client.post("/sign-in", data={"username": "owner@example.test", "password": "original passphrase", "csrfToken": csrfToken}).status_code)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client

    def issue(self):
        csrfToken = self.csrf(self.client, "/forgot-password")
        with patch("users.sendRecoveryEmail") as send:
            response = self.client.post("/forgot-password", data={"username": "owner@example.test", "csrfToken": csrfToken}, headers={"X-Forwarded-Host": "untrusted.example.test"})
        self.assertEqual(200, response.status_code)
        link = send.call_args.args[2]
        self.assertTrue(link.startswith("https://finance.example.test/reset-password#"))
        return link.split("#", 1)[1], response

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
