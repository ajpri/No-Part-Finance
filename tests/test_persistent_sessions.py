"""Persistent login uses isolated synthetic databases; production is never opened."""
import tests
from tests.test_support import RecoveryFixture, SessionFixture

import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import database as ledger
from app import createApp
from packageApplication import createVerifiedDatabaseCopy
from users import setUserEnabled, resetPassword


class PersistentSessionTests(SessionFixture, unittest.TestCase):
    def setUp(self):
        SessionFixture.setUp(self)
        self.configurationPath = self.path.parent / "platform.conf"
        self.configurationPath.write_text("persistLoginOnRestart = true\n")
        self.application = createApp(self.path)

    def restoredClient(self, original, application=None):
        client = (application or createApp(self.path)).test_client()
        for name in ("npfSession", "npfInstallation"):
            cookie = original.get_cookie(name)
            if cookie:
                client.set_cookie(name, cookie.value)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = original.environ_base["HTTP_X_CSRF_TOKEN"]
        return client

    def testRestartPreservesCookieCsrfIdentityAndOriginalExpiry(self):
        original = self.login(agent="Firefox/140.0 Linux")
        baseline = original.get("/api/session").get_json()
        sessionRows = self.sessions(original)
        token = original.get_cookie("npfSession").value
        restored = self.restoredClient(original)
        with patch("app.time.time", return_value=baseline["expiresAt"] - 1):
            response = restored.get("/api/session", headers={"X-Background-Request": "true"})
            self.assertEqual(200, response.status_code)
            self.assertEqual(baseline, response.get_json())
            self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(sessionRows[0]["id"], self.sessions(restored)[0]["id"])
        row = self.connection.execute("SELECT * FROM browserSessions").fetchone()
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), row["tokenDigest"])
        self.assertNotIn(token, json.dumps(dict(row)))
        self.assertEqual(baseline["expiresAt"], row["expiresAt"])
        forged = self.restoredClient(original)
        forged.set_cookie("npfSession", row["tokenDigest"])
        self.assertEqual(401, forged.get("/api/session").status_code)
        with patch("app.time.time", return_value=baseline["expiresAt"]):
            self.assertEqual(401, restored.get("/api/session").status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])

    def testDisablingAndReenablingDoesNotResurrectSavedSessions(self):
        current = self.login()
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        for configuration in ("persistLoginOnRestart = false\n", "# persistLoginOnRestart = true\n", ""):
            self.configurationPath.write_text(configuration)
            disabled = createApp(self.path)
            self.assertEqual(401, self.restoredClient(current, disabled).get("/api/session").status_code)
            self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        self.configurationPath.unlink()
        disabled = createApp(self.path)
        ephemeral = self.login(application=disabled)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        self.configurationPath.write_text("persistLoginOnRestart = true\n")
        enabled = createApp(self.path)
        self.assertEqual(401, self.restoredClient(current, enabled).get("/api/session").status_code)
        self.assertEqual(401, self.restoredClient(ephemeral, enabled).get("/api/session").status_code)

    def testRestoredSessionsIndividualOthersAndCurrentLogout(self):
        first, second, third = self.login(), self.login(), self.login()
        restarted = createApp(self.path)
        first = self.restoredClient(first, restarted)
        second = self.restoredClient(second, restarted)
        third = self.restoredClient(third, restarted)
        remote = next(row for row in self.sessions(first) if not row["current"])
        self.assertEqual(200, first.post("/api/account/sessions/logout", json={"id": remote["id"]}).status_code)
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        self.assertEqual(200, first.post("/api/account/sessions/logout", json={"others": True}).status_code)
        self.assertEqual(401, second.get("/api/session").status_code)
        self.assertEqual(401, third.get("/api/session").status_code)
        self.assertEqual(200, first.post("/api/logout").status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])

    def testOwnIdentityWritesRetainOnlyCompletingSessionAcrossRestart(self):
        first, remote = self.login(), self.login()
        expiry = first.get("/api/session").get_json()["expiresAt"]
        self.assertEqual(200, first.put("/api/account/name", json={"firstName": "Updated", "lastName": "Owner"}).status_code)
        first = self.restoredClient(first)
        self.assertEqual("Updated", first.get("/api/account").get_json()["firstName"])
        self.assertEqual(401, self.restoredClient(remote).get("/api/session").status_code)
        self.assertEqual(200, first.post("/api/account/password", json={"currentPassword": "owner password", "newPassword": "replacement passphrase", "confirmation": "replacement passphrase"}).status_code)
        first = self.restoredClient(first)
        self.assertEqual(expiry, first.get("/api/session").get_json()["expiresAt"])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])

    def testCliRevocationAndDisabledStatusNeverResume(self):
        current = self.login()
        setUserEnabled(self.connection, self.ownerId, False)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        setUserEnabled(self.connection, self.ownerId, True)
        self.assertEqual(401, self.restoredClient(current).get("/api/session").status_code)
        current = self.login()
        resetPassword(self.connection, self.ownerId, "new cli passphrase")
        self.assertEqual(401, self.restoredClient(current).get("/api/session").status_code)

    def testCopiesExcludeSessionsAndRestorationRequiresSignin(self):
        current = self.login()
        token = current.get_cookie("npfSession").value
        csrf = current.environ_base["HTTP_X_CSRF_TOKEN"]
        copied = self.path.parent / "full-copy.db"
        createVerifiedDatabaseCopy(self.path, copied)
        exported = self.path.parent / "browser-copy.db"
        exported.write_bytes(current.get("/api/backup").data)
        for filename in (copied, exported):
            with sqlite3.connect(filename) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertNotIn(token.encode(), filename.read_bytes())
            self.assertNotIn(csrf.encode(), filename.read_bytes())
        self.assertEqual(401, self.restoredClient(current, createApp(copied)).get("/api/session").status_code)
        self.assertEqual(200, current.get("/api/session").status_code)

    def testConcurrentRequestsPreserveRevocationAndFixedExpiry(self):
        current = self.login()
        application = createApp(self.path)
        expiry = current.get("/api/session").get_json()["expiresAt"]
        clients = [self.restoredClient(current, application) for index in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(lambda client: client.get("/api/session", headers={"X-Background-Request": "true"}).status_code, clients))
        self.assertEqual([200] * 8, statuses)
        self.assertEqual(expiry, self.connection.execute("SELECT expiresAt FROM browserSessions").fetchone()[0])
        self.assertEqual(200, current.post("/api/logout").status_code)
        self.assertTrue(all(client.get("/api/session").status_code == 401 for client in clients))

    def testAtomicPasswordFailurePreservesExistingLoginAndNotificationState(self):
        first, remote = self.login(), self.login()
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM browserSessions ORDER BY tokenDigest")]
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectPersistentPassword BEFORE INSERT ON auditEvents WHEN NEW.action = 'password-change' BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        self.assertEqual(500, first.post("/api/account/password", json={"currentPassword": "owner password", "newPassword": "replacement passphrase", "confirmation": "replacement passphrase"}).status_code)
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM browserSessions ORDER BY tokenDigest")])
        self.assertEqual(200, self.restoredClient(first).get("/api/session").status_code)
        self.assertEqual(200, self.restoredClient(remote).get("/api/session").status_code)

    testPersistentUserIsolationAndPublicIds = SessionFixture.checkOwnSessionsPublicIdentifiersClientsAndIndividualRevocation
    testPersistentBackgroundActivity = SessionFixture.checkBackgroundRequestsUpdateIpWithoutLastActivity
    testPersistentRolesAndNoLedger = SessionFixture.checkAllRolesNoLedgerAndDisabledSelfService
    testPersistentCsrfAndPayloads = SessionFixture.checkAuthorizationCsrfAndInvalidPayloads

    def testLogoutFailureRollsBackDurableRevocation(self):
        current = self.login()
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectPersistentLogout BEFORE DELETE ON browserSessions BEGIN SELECT RAISE(ABORT, 'synthetic logout failure'); END")
        self.assertEqual(500, current.post("/api/logout").status_code)
        self.assertEqual(200, self.restoredClient(current).get("/api/session").status_code)

    def testCliRecoveryBackupAlsoOmitsSessions(self):
        from cliDeletion import databaseBackup
        current = self.login()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            copied = databaseBackup(self.path, self.path.parent / "deletion-backups")
        with sqlite3.connect(copied) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        self.assertNotIn(current.environ_base["HTTP_X_CSRF_TOKEN"].encode(), copied.read_bytes())
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])

    def testActivityDoesNotCommitUnfinishedFinancialWrites(self):
        from flask import g
        account = ledger.createAccount(self.connection, "Original account", "cash")

        @self.application.post("/fixture-uncommitted")
        def uncommittedWrite():
            g.database.execute("BEGIN IMMEDIATE")
            g.database.execute("UPDATE accounts SET name = ? WHERE id = ?", ("Uncommitted account", account))
            return {"status": "fixture"}

        current = self.login()
        with self.assertLogs(level="WARNING"):
            self.assertEqual(200, current.post("/fixture-uncommitted").status_code)
        self.assertEqual("Original account", self.connection.execute("SELECT name FROM accounts WHERE id = ?", (account,)).fetchone()[0])

    def testActivityFailureDoesNotChangeCommittedFinancialResponse(self):
        from flask import g
        account = ledger.createAccount(self.connection, "Original account", "cash")

        @self.application.post("/fixture-committed")
        def committedWrite():
            with g.database:
                g.database.execute("UPDATE accounts SET name = ? WHERE id = ?", ("Committed account", account))
            return {"status": "fixture"}

        current = self.login()
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectActivity BEFORE UPDATE ON browserSessions BEGIN SELECT RAISE(ABORT, 'synthetic activity failure'); END")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(200, current.post("/fixture-committed").status_code)
        self.assertEqual("Committed account", self.connection.execute("SELECT name FROM accounts WHERE id = ?", (account,)).fetchone()[0])

    def testNotificationSessionPresenceAndLogoutCleanupAfterRestart(self):
        current = self.login()
        installation = current.get_cookie("npfInstallation").value
        with self.connection:
            self.connection.execute("INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId,installationId,client) VALUES (?,?,?,'{}',0,?,'Fixture browser')", (self.ownerId,self.ledgerId,"fixture-persistent-device",installation))
        restored = self.restoredClient(current)
        row = restored.get("/api/account/notifications").get_json()[0]
        self.assertTrue(row["current"])
        self.assertEqual("signed-in", row["sessionStatus"])
        self.assertEqual(200, restored.post("/api/logout").status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM pushSubscriptions").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])

    def testRestrictedRegistrationDoesNotGainAccessAfterRestart(self):
        self.configurationPath.write_text("persistLoginOnRestart = true\nallowWebRegistration = true\nenforceEmailUsernames = true\nrequireRegistrationVerification = true\nsmtpEnabled = true\nsmtpHost = smtp.example.test\nsmtpFromAddress = finance@example.test\n")
        application = createApp(self.path)
        client = application.test_client()
        page = client.get("/register").get_data(as_text=True)
        csrf = re.search('name="csrfToken" value="([^\"]+)"', page).group(1)
        def concurrentRequestDuringEmail(*arguments, **options):
            self.assertEqual(200, application.test_client().get("/sign-in").status_code)
            return "Verification code sent."

        with patch("users.sendVerificationEmail", side_effect=concurrentRequestDuringEmail):
            response = client.post("/register", data={"csrfToken":csrf,"username":"pending@example.test","firstName":"Pending","lastName":"","password":"pending passphrase","confirmation":"pending passphrase"})
        self.assertEqual(302, response.status_code)
        self.assertEqual("/verify", response.location)
        restored = createApp(self.path).test_client()
        for name in ("npfSession","npfInstallation"):
            restored.set_cookie(name, client.get_cookie(name).value)
        self.assertEqual(403, restored.get("/api/session").status_code)
        self.assertEqual(403, restored.get("/api/accounts").status_code)
        self.assertEqual(200, restored.get("/verify").status_code)

    def testConcurrentRevocationPreventsOwnIdentityWrite(self):
        from users import updateProfile
        current = self.login()
        digest = hashlib.sha256(current.get_cookie("npfSession").value.encode()).hexdigest()

        def revokedBeforeWrite(*arguments, **options):
            with self.connection:
                self.connection.execute("DELETE FROM browserSessions WHERE tokenDigest = ?", (digest,))
            return updateProfile(*arguments, **options)

        with patch("app.updateProfile", side_effect=revokedBeforeWrite):
            self.assertEqual(400, current.put("/api/account/name", json={"firstName":"Revoked", "lastName":""}).status_code)
        self.assertEqual("Owner", self.connection.execute("SELECT firstName FROM users WHERE id = ?", (self.ownerId,)).fetchone()[0])
        self.assertEqual(401, current.get("/api/session").status_code)

    def testManagementInitializationDoesNotApplyPendingFlagChange(self):
        current = self.login()
        self.configurationPath.write_text("persistLoginOnRestart = false\n")
        createApp(self.path, clearSavedSessions=False)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        self.assertEqual(200, current.get("/api/session").status_code)
        createApp(self.path)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        self.assertEqual(401, current.get("/api/session").status_code)

    def testFailedSessionInsertDoesNotRecordSuccessfulLogin(self):
        before = self.connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (self.ownerId,)).fetchone()[0]
        client = self.application.test_client()
        csrf = re.search('name="csrfToken" value="([^\"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectNewSession BEFORE INSERT ON browserSessions BEGIN SELECT RAISE(ABORT, 'synthetic session write failure'); END")
        self.assertEqual(500, client.post("/sign-in", data={"username":"Owner","password":"owner password","csrfToken":csrf}).status_code)
        self.assertEqual(before, self.connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (self.ownerId,)).fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        self.assertEqual(401, client.get("/api/session").status_code)

    def testConfigurationDefaultSampleAndInvalidFlag(self):
        from platformConfiguration import ConfigurationError, configurationSample, loadConfiguration
        self.assertIn("persistLoginOnRestart = false", configurationSample())
        for value in ("yes", "1", "invalid-secret"):
            self.configurationPath.write_text(f"persistLoginOnRestart = {value}\n")
            with self.assertRaises(ConfigurationError) as raised:
                loadConfiguration(self.path.parent)
            self.assertNotIn("invalid-secret", str(raised.exception))

    def testCombinedAccountSessionUpgradeAndFailedUpgradeRecovery(self):
        for fail in (False, True):
            path = self.path.parent / ("legacy-failure.db" if fail else "legacy-success.db")
            with patch.object(ledger, "schemaVersion", 30):
                ledger.initializeDatabase(path)
            with sqlite3.connect(path) as connection:
                before = "\n".join(connection.iterdump())
            previousBackups = set((path.parent / "backups").glob("pre-migration-v30-to-v31-*.db"))
            originalMigration = ledger.migrateBrowserSessions

            def partialUpgrade(connection):
                originalMigration(connection)
                raise RuntimeError("synthetic session migration failure")

            if fail:
                with patch.object(ledger, "migrateBrowserSessions", side_effect=partialUpgrade), self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "session migration failure"):
                    ledger.initializeDatabase(path)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(before, "\n".join(connection.iterdump()))
            else:
                ledger.initializeDatabase(path)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(31, connection.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
                    self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                    self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            backups = set((path.parent / "backups").glob("pre-migration-v30-to-v31-*.db")) - previousBackups
            self.assertEqual(1, len(backups))
            with sqlite3.connect(backups.pop()) as connection:
                self.assertEqual(before, "\n".join(connection.iterdump()))

    def testEarlierRecoveryBackupGainsSessionsWithVerifiedRollback(self):
        for fail in (False, True):
            path = self.path.parent / ("earlier31-failure.db" if fail else "earlier31-success.db")
            ledger.initializeDatabase(path)
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER invalidateBrowserSessions")
                connection.execute("DROP TABLE browserSessions")
                before = "\n".join(connection.iterdump())
            previousBackups = set((path.parent / "backups").glob("pre-migration-v31-to-v31-*.db"))
            originalMigration = ledger.migrateBrowserSessions

            def partialUpgrade(connection):
                originalMigration(connection)
                connection.commit()
                raise RuntimeError("synthetic session migration failure")

            if fail:
                with patch.object(ledger, "migrateBrowserSessions", side_effect=partialUpgrade), self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "session migration failure"):
                    ledger.initializeDatabase(path)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(before, "\n".join(connection.iterdump()))
            else:
                ledger.initializeDatabase(path)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(31, connection.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
                    self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                ledger.initializeDatabase(path)
            backups = set((path.parent / "backups").glob("pre-migration-v31-to-v31-*.db")) - previousBackups
            self.assertEqual(1, len(backups))
            with sqlite3.connect(backups.pop()) as connection:
                self.assertEqual(before, "\n".join(connection.iterdump()))


class PersistentRecoveryTests(RecoveryFixture, unittest.TestCase):
    def setUp(self):
        RecoveryFixture.setUp(self)
        configurationPath = self.root / "platform.conf"
        configurationPath.write_text(configurationPath.read_text() + "\npersistLoginOnRestart = true\n")
        self.application = createApp(self.path)
        self.client = self.application.test_client()

    def testRecoveryCannotResumeOldCookiesAfterRestart(self):
        current = self.login()
        token, _ = self.issue()
        csrfToken = self.csrf(self.client, "/reset-password")
        self.assertEqual(200, self.client.post("/reset-password", data={"csrfToken": csrfToken, "token": token, "newPassword": "replacement passphrase", "confirmation": "replacement passphrase"}).status_code)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM browserSessions").fetchone()[0])
        restored = createApp(self.path).test_client()
        restored.set_cookie("npfSession", current.get_cookie("npfSession").value)
        self.assertEqual(401, restored.get("/api/session").status_code)


class PersistentProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX process control required; Windows uses the same session storage")
    def testSameCookieWorksAcrossTwoRealWaitressProcesses(self):
        import http.cookiejar
        import signal
        import socket
        import subprocess
        import sys
        import tempfile
        import time
        import urllib.parse
        import urllib.request
        from users import createUser

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "synthetic.db"
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            (root / "platform.conf").write_text(f"persistLoginOnRestart = true\nserverPort = {port}\nlistenAddress = localhost\n")
            createApp(path)
            connection = ledger.connectInitialLedgerDatabase(path)
            try:
                createUser(connection, "owner", "Owner", "fixture password", firstName="Fixture")
            finally:
                connection.close()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            url = f"http://127.0.0.1:{port}"
            runner = "import tests; from app import createApp,runApplication; import sys; runApplication(createApp(sys.argv[1]))"
            baseline = None
            originalId = None
            for iteration in range(2):
                process = subprocess.Popen([sys.executable, "-c", runner, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for attempt in range(100):
                        self.assertIsNone(process.poll(), "Fixture server exited before serving")
                        try:
                            # A public asset checks readiness without replacing an existing session cookie.
                            with opener.open(url + "/styles.css", timeout=1) as response:
                                self.assertEqual("waitress", response.headers["Server"])
                            break
                        except OSError:
                            time.sleep(0.05)
                    else:
                        self.fail("Fixture Waitress server did not become ready")
                    if iteration == 0:
                        with opener.open(url + "/sign-in") as response:
                            csrf = re.search('name="csrfToken" value="([^"]+)"', response.read().decode()).group(1)
                        body = urllib.parse.urlencode({"username": "Owner", "password": "fixture password", "csrfToken": csrf}).encode()
                        with opener.open(url + "/sign-in", body) as response:
                            self.assertEqual(200, response.status)
                    with opener.open(url + "/api/session") as response:
                        session = json.load(response)
                    with opener.open(url + "/api/account/sessions") as response:
                        rows = json.load(response)
                    if baseline is None:
                        baseline = session
                        originalId = rows[0]["id"]
                    else:
                        self.assertEqual(baseline, session)
                        self.assertEqual(originalId, rows[0]["id"])
                        self.assertTrue(rows[0]["current"])
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    try:
                        output, errors = process.communicate(timeout=10)
                        self.assertEqual(0, process.returncode, (output + errors).decode())
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                        raise
