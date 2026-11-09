"""Installation configuration, proxy trust, SMTP, and safe operational logs."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import IdentityFixture
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app import createApp
import database as ledger
from platformConfiguration import ConfigurationError, OperationalLogs, configurationSample, loadConfiguration, synchronizeConfigurationFiles
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

    def testServerListeningDefaultsValidationAndLocalhostAlias(self):
        defaults = loadConfiguration(self.root)
        self.assertEqual(5000, defaults["serverPort"])
        self.assertEqual("0.0.0.0", defaults["listenAddress"])
        commented = self.write("# serverPort = 8080\n# listenAddress = localhost\n")
        self.assertEqual(defaults, commented)
        for port in (1, 5000, 8080, 65535):
            for address, expected in (("0.0.0.0", "0.0.0.0"), ("127.0.0.1", "127.0.0.1"), ("LOCALhost", "127.0.0.1"), ("192.168.50.20", "192.168.50.20")):
                with self.subTest(port=port,address=address):
                    configured = self.write(f" serverPort = {port} \n listenAddress = {address} \n")
                    self.assertEqual(port, configured["serverPort"])
                    self.assertEqual(expected, configured["listenAddress"])
        for port in ("", "0", "65536", "-1", "+5000", "5.0", "５０００", "9" * 100, "SECRET"):
            with self.subTest(port=port), self.assertRaises(ConfigurationError) as raised:
                self.write(f"serverPort = {port}\n")
            self.assertIn("serverPort", str(raised.exception))
            self.assertNotIn("SECRET", str(raised.exception))
        for address in ("", "SECRET", "example.org", "::1", "127.0.0.1/8", "http://127.0.0.1", "127.0.0.1:8080", "999.0.0.1", "127.1", "127.000.0.1", "localhost SECRET"):
            with self.subTest(address=address), self.assertRaises(ConfigurationError) as raised:
                self.write(f"listenAddress = {address}\n")
            self.assertIn("listenAddress", str(raised.exception))
            self.assertNotIn("SECRET", str(raised.exception))

    def testListeningSettingsPreservedAndAppliedOnlyToNewApp(self):
        runtime = self.root / "platform.conf"
        runtime.write_text("# Preserve these listening choices\nserverPort = invalid-earlier-value\nserverPort = 8080\nlistenAddress = LOCALHOST\n")
        application = createApp(self.root / "synthetic.db")
        self.assertEqual(8080, application.config["PLATFORM_CONFIGURATION"]["serverPort"])
        self.assertEqual("127.0.0.1", application.config["PLATFORM_CONFIGURATION"]["listenAddress"])
        self.assertIn("listenAddress = LOCALHOST", runtime.read_text())
        self.assertIn("# Preserve these listening choices", runtime.read_text())
        sample = (self.root / "platform.conf.sample").read_text()
        self.assertIn("serverPort = 5000", sample)
        self.assertIn("listenAddress = 0.0.0.0", sample)
        self.assertNotIn("8080", sample)
        runtime.write_text("serverPort = 8081\nlistenAddress = 192.168.50.20\n")
        self.assertEqual(8080, application.config["PLATFORM_CONFIGURATION"]["serverPort"])
        restarted = createApp(self.root / "synthetic.db")
        self.assertEqual(8081, restarted.config["PLATFORM_CONFIGURATION"]["serverPort"])
        self.assertEqual("192.168.50.20", restarted.config["PLATFORM_CONFIGURATION"]["listenAddress"])
        runtime.write_text("serverPort = 8082\n# --- Extra data (ignored) ---\nserverPort = 1234\nlistenAddress = localhost\n")
        self.assertEqual(8082, loadConfiguration(self.root)["serverPort"])
        self.assertEqual("0.0.0.0", loadConfiguration(self.root)["listenAddress"])

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
        for original in ("smtpPort = SECRET", "allowWebRegistration = true\nallowWebRegistration = SECRET", "smtpPort SECRET", "serverPort = 0", "listenAddress = SECRET"):
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

class ProxyAndSmtpTests(IdentityFixture, unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
