import tests

import http.client
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from werkzeug.serving import make_server

from app import createApp, runApplication
from database import connectDatabase
from users import configureOnStartup
from tests.test_support import connectFixtureDatabase as connectDatabase


class StartupServingTests(unittest.TestCase):
    def requestServer(self, server, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8"), dict(response.getheaders())
        finally:
            connection.close()

    def testServerServesDuringOwnerAndAuditorPromptsWithoutRestart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup-serving.db"
            application = createApp(path)
            server = make_server("127.0.0.1", 0, application, threaded=True)
            terminalThreadId = threading.get_ident()

            def setupInput(prompt):
                self.assertEqual(terminalThreadId, threading.get_ident())
                if prompt.startswith("Create the required Owner"):
                    for route in ["/", "/sign-in", "/api/accounts", "/api/transactions"]:
                        status, responseBody, _ = self.requestServer(server, route)
                        self.assertEqual(503, status)
                        self.assertIn("An Owner is required", responseBody)
                    status, _, _ = self.requestServer(server, "/api/accounts", "{}", {"Content-Type": "application/json"})
                    self.assertEqual(503, status)
                    return "y"
                if prompt.startswith("Owner username"):
                    return "Example Owner"
                self.assertTrue(prompt.startswith("Create an optional"))
                # The Owner is already committed while optional setup awaits input.
                try:
                    status, responseBody, responseHeaders = self.requestServer(server, "/sign-in")
                    self.assertEqual(200, status)
                    csrfToken = re.search(r'name="csrfToken" value="([^"]+)"', responseBody).group(1)
                    loginBody = urlencode({"username": "Example Owner", "password": "startup password", "csrfToken": csrfToken})
                    status, _, loginHeaders = self.requestServer(server, "/sign-in", loginBody, {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cookie": responseHeaders["Set-Cookie"].split(";", 1)[0],
                    })
                    self.assertEqual(302, status)
                    status, _, _ = self.requestServer(server, "/api/accounts", headers={"Cookie": loginHeaders["Set-Cookie"].split(";", 1)[0]})
                    self.assertEqual(200, status)
                finally:
                    server.shutdown()
                return ""

            def protectedPassword(prompt):
                self.assertEqual(terminalThreadId, threading.get_ident())
                return "startup password"

            with patch("app.make_server", return_value=server), patch("users.input", side_effect=setupInput), patch("users.getpass.getpass", side_effect=protectedPassword), patch("builtins.print"):
                runApplication(application)
            connection = connectDatabase(path)
            try:
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()

    def testSkippedOrInterruptedSetupLeavesServerAvailable(self):
        for response in ["", EOFError(), KeyboardInterrupt(), OSError()]:
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as directory:
                application = createApp(Path(directory) / "skipped-setup.db")
                server = make_server("127.0.0.1", 0, application, threaded=True)

                def verifyAfterSetup(connection):
                    try:
                        configureOnStartup(connection)
                        status, responseBody, _ = self.requestServer(server, "/")
                        self.assertEqual(503, status)
                        self.assertIn("An Owner is required", responseBody)
                        self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                    finally:
                        server.shutdown()

                with patch("app.make_server", return_value=server), patch("app.configureOnStartup", side_effect=verifyAfterSetup), patch("users.input", side_effect=[response]), patch("builtins.print"):
                    runApplication(application)

    def testCtrlCClosesServerAfterSetup(self):
        with tempfile.TemporaryDirectory() as directory:
            application = createApp(Path(directory) / "shutdown.db")
            with patch("app.make_server") as createServer, patch("app.threading.Thread") as createThread, patch("app.configureOnStartup"), self.assertLogs(level="INFO"):
                createThread.return_value.is_alive.return_value = True
                createThread.return_value.join.side_effect = [KeyboardInterrupt(), None]
                runApplication(application)
            createServer.return_value.shutdown.assert_called_once()
            createServer.return_value.server_close.assert_called_once()

    def testStartupGuidanceReflectsExistingOwner(self):
        from users import createUser

        for ownerConfigured in [False, True]:
            with self.subTest(ownerConfigured=ownerConfigured), tempfile.TemporaryDirectory() as directory:
                databasePath = Path(directory) / "startup-guidance.db"
                backupsPath = Path(directory) / "backups"
                backupsPath.mkdir()
                application = createApp(databasePath)
                connection = connectDatabase(databasePath)
                try:
                    if ownerConfigured:
                        createUser(connection, "owner", "Example Owner", "startup password")
                    beforeStartup = "\n".join(connection.iterdump())
                finally:
                    connection.close()
                with patch("app.make_server"), patch("app.threading.Thread") as createThread, patch("app.configureOnStartup") as setup, self.assertLogs(level="INFO") as startupLogs:
                    createThread.return_value.is_alive.return_value = False
                    runApplication(application)
                setup.assert_called_once()
                messages = "\n".join(startupLogs.output)
                self.assertIn("open http://127.0.0.1:5000", messages)
                self.assertIn("Keep this terminal open", messages)
                if ownerConfigured:
                    self.assertIn("Sign in to use the application.", messages)
                    self.assertNotIn("Complete Owner setup", messages)
                    self.assertNotIn("reload to sign in", messages)
                else:
                    self.assertIn("Complete Owner setup below, then reload to sign in.", messages)
                connection = connectDatabase(databasePath)
                try:
                    self.assertEqual(beforeStartup, "\n".join(connection.iterdump()))
                finally:
                    connection.close()

    def testBindFailureDoesNotOfferSetup(self):
        with tempfile.TemporaryDirectory() as directory:
            application = createApp(Path(directory) / "bind-failure.db")
            with patch("app.make_server", side_effect=OSError("port unavailable")), patch("users.input") as setupInput:
                with self.assertRaisesRegex(OSError, "port unavailable"):
                    runApplication(application)
            setupInput.assert_not_called()
