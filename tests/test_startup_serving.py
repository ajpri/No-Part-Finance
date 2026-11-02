import tests

import http.client
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from waitress import wasyncore

from app import createApp, createHttpServer, runApplication
from database import connectDatabase
from users import configureOnStartup
from tests.test_support import connectFixtureDatabase as connectDatabase


class StartupServingTests(unittest.TestCase):
    def closeFixtureServer(self, server):
        server.task_dispatcher.shutdown()
        wasyncore.close_all(server._map)

    def stopServing(self, server):
        server.trigger.pull_trigger(lambda: wasyncore.close_all(server._map))

    def requestServer(self, server, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", int(server.effective_port), timeout=5)
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
            server = createHttpServer(application, "127.0.0.1", 0)
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
                if prompt.startswith("First name"):
                    return "Example"
                if prompt.startswith("Last name"):
                    return ""
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
                    self.stopServing(server)
                return ""

            def protectedPassword(prompt):
                self.assertEqual(terminalThreadId, threading.get_ident())
                return "startup password"

            with patch("app.createHttpServer", return_value=server), patch("users.input", side_effect=setupInput), patch("users.getpass.getpass", side_effect=protectedPassword), patch("builtins.print"):
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
                server = createHttpServer(application, "127.0.0.1", 0)

                def verifyAfterSetup(connection):
                    try:
                        configureOnStartup(connection)
                        status, responseBody, _ = self.requestServer(server, "/")
                        self.assertEqual(503, status)
                        self.assertIn("An Owner is required", responseBody)
                        self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                    finally:
                        self.stopServing(server)

                with patch("app.createHttpServer", return_value=server), patch("app.configureOnStartup", side_effect=verifyAfterSetup), patch("users.input", side_effect=[response]), patch("builtins.print"):
                    runApplication(application)

    def testCtrlCClosesServerAfterSetup(self):
        with tempfile.TemporaryDirectory() as directory:
            application = createApp(Path(directory) / "shutdown.db")
            with patch("app.createHttpServer") as createServer, patch("app.threading.Thread") as createThread, patch("app.configureOnStartup"), self.assertLogs(level="INFO"):
                createThread.return_value.is_alive.return_value = True
                createThread.return_value.join.side_effect = [KeyboardInterrupt(), None]
                runApplication(application)
            createServer.return_value.task_dispatcher.shutdown.assert_called_once()
            createServer.return_value.trigger.pull_trigger.assert_called_once()

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
                        createUser(connection, "owner", "Example Owner", "startup password", firstName="Example Owner")
                    beforeStartup = "\n".join(connection.iterdump())
                finally:
                    connection.close()
                with patch("app.createHttpServer"), patch("app.threading.Thread") as createThread, patch("app.configureOnStartup") as setup, self.assertLogs(level="INFO") as startupLogs:
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
            with patch("app.createHttpServer", side_effect=OSError("port unavailable")), patch("users.input") as setupInput:
                with self.assertRaisesRegex(OSError, "port unavailable"):
                    runApplication(application)
            setupInput.assert_not_called()

    def testConfiguredListenerAndBrowserGuidance(self):
        for address, expectedAddress, browserAddress in (("0.0.0.0", "0.0.0.0", "127.0.0.1"), ("localhost", "127.0.0.1", "127.0.0.1"), ("127.0.0.1", "127.0.0.1", "127.0.0.1"), ("192.168.50.20", "192.168.50.20", "192.168.50.20")):
            with self.subTest(address=address), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "platform.conf").write_text(f"serverPort = 8080\nlistenAddress = {address}\n")
                application = createApp(root / "synthetic.db")
                with patch("app.createHttpServer") as createServer, patch("app.threading.Thread") as createThread, patch("app.configureOnStartup") as setup, self.assertLogs(level="INFO") as startupLogs:
                    createThread.return_value.is_alive.return_value = False
                    runApplication(application)
                createServer.assert_called_once_with(application, expectedAddress, 8080)
                setup.assert_called_once()
                messages = "\n".join(startupLogs.output)
                self.assertIn(f"Binding server to {expectedAddress}:8080", messages)
                self.assertIn(f"open http://{browserAddress}:8080", messages)
                self.assertNotIn(":5000", messages)

    def testOccupiedConfiguredPortFailsBeforeSetup(self):
        from contextlib import redirect_stderr
        from io import StringIO

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A guarded fixture server reserves an OS-selected port without touching production.
            application = createApp(root / "synthetic.db")
            occupied = createHttpServer(application, "127.0.0.1", 0)
            self.addCleanup(self.closeFixtureServer, occupied)
            (root / "platform.conf").write_text(f"serverPort = {int(occupied.effective_port)}\nlistenAddress = localhost\n")
            application = createApp(root / "synthetic.db")
            errors = StringIO()
            with patch("app.configureOnStartup") as setup, redirect_stderr(errors), self.assertLogs(level="INFO") as startupLogs, self.assertRaises(OSError):
                runApplication(application)
            setup.assert_not_called()
            self.assertIn(str(occupied.effective_port), "\n".join(startupLogs.output))

    def testCustomLocalListenerServesBeforeSetupAndCloses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = createApp(root / "synthetic.db")
            server = createHttpServer(application, "127.0.0.1", 0)
            self.addCleanup(self.closeFixtureServer, server)
            (root / "platform.conf").write_text(f"serverPort = {int(server.effective_port)}\nlistenAddress = localhost\n")
            application = createApp(root / "synthetic.db")

            def verifySetup(connection):
                try:
                    status, responseBody, _ = self.requestServer(server, "/sign-in")
                    self.assertEqual(503, status)
                    self.assertIn("An Owner is required", responseBody)
                finally:
                    self.stopServing(server)

            with patch("app.createHttpServer", return_value=server) as createServer, patch("app.configureOnStartup", side_effect=verifySetup):
                runApplication(application)
            createServer.assert_called_once_with(application, "127.0.0.1", int(server.effective_port))
            self.assertEqual({}, server._map)
            self.assertEqual(set(), server.task_dispatcher.threads)

    def testWaitressPreservesApplicationProxyTrustOverHttp(self):
        from flask import request
        from users import createUser

        for trusted in (False, True):
            with self.subTest(trusted=trusted), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "platform.conf").write_text("trustedProxyAddresses = " + ("127.0.0.1" if trusted else "") + "\n")
                application = createApp(root / "synthetic.db")
                connection = connectDatabase(root / "synthetic.db")
                try:
                    createUser(connection, "owner", "Fixture Owner", "fixture password", firstName="Fixture")
                finally:
                    connection.close()
                originalSignIn = application.view_functions["signIn"]

                def echoPeer():
                    response = application.make_response(originalSignIn())
                    response.headers["X-Fixture-Ip"] = request.remote_addr
                    response.headers["X-Fixture-Scheme"] = request.scheme
                    return response

                application.view_functions["signIn"] = echoPeer
                server = createHttpServer(application, "127.0.0.1", 0)
                self.addCleanup(self.closeFixtureServer, server)

                def verifyProxy(connection):
                    try:
                        status, _, headers = self.requestServer(server, "/sign-in", headers={"X-Forwarded-For": "spoofed, 198.51.100.42", "X-Forwarded-Proto": "https"})
                        self.assertEqual(200, status)
                        self.assertEqual("waitress", headers["Server"])
                        self.assertEqual("198.51.100.42" if trusted else "127.0.0.1", headers["X-Fixture-Ip"])
                        self.assertEqual("https" if trusted else "http", headers["X-Fixture-Scheme"])
                        self.assertEqual(trusted, "Secure;" in headers["Set-Cookie"])
                        status, _, _ = self.requestServer(server, "/sign-in", headers={"X-Forwarded-For": "bad", "X-Forwarded-Proto": "https"})
                        self.assertEqual(400 if trusted else 200, status)
                        status, _, headers = self.requestServer(server, "/sign-in")
                        self.assertEqual(200, status)
                        self.assertEqual("127.0.0.1", headers["X-Fixture-Ip"])
                    finally:
                        self.stopServing(server)

                with patch("app.createHttpServer", return_value=server), patch("app.configureOnStartup", side_effect=verifyProxy):
                    runApplication(application)

    def testBindFailureClosesWaitressResources(self):
        from unittest.mock import Mock
        from app import createHttpServer

        dispatcher = Mock()
        registered = Mock()

        def failBind(application, **options):
            options["map"][123] = registered
            raise OSError("fixture unavailable address")

        with patch("app.ThreadedTaskDispatcher", return_value=dispatcher), patch("app.create_server", side_effect=failBind):
            with self.assertRaisesRegex(OSError, "unavailable address"):
                application = Mock(config={"MAX_CONTENT_LENGTH": 1024 * 1024})
                createHttpServer(application, "192.0.2.1", 5000)
        registered.close.assert_called_once()
        dispatcher.shutdown.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "Requires POSIX signal delivery; Windows shutdown uses the shared close path")
    def testRealProcessCtrlCClosesPortAndWorkers(self):
        import signal
        import socket
        import subprocess
        import sys
        import time
        from users import createUser

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            (root / "platform.conf").write_text(f"serverPort = {port}\nlistenAddress = localhost\n")
            databasePath = root / "synthetic.db"
            application = createApp(databasePath)
            connection = connectDatabase(databasePath)
            try:
                createUser(connection, "owner", "Fixture Owner", "fixture password", firstName="Fixture")
            finally:
                connection.close()
            runner = "import tests; from app import createApp, runApplication; import sys; runApplication(createApp(sys.argv[1]))"
            process = subprocess.Popen([sys.executable, "-c", runner, str(databasePath)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for attempt in range(100):
                    self.assertIsNone(process.poll(), "Fixture server exited before serving")
                    try:
                        request = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                        request.request("GET", "/sign-in")
                        response = request.getresponse()
                        self.assertEqual(200, response.status)
                        self.assertEqual("waitress", response.getheader("Server"))
                        response.read()
                        request.close()
                        break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Fixture server did not start")
                process.send_signal(signal.SIGINT)
                output, errors = process.communicate(timeout=10)
                self.assertEqual(0, process.returncode, (output + errors).decode())
                self.assertNotIn(b"Traceback", errors)
                with socket.socket() as check:
                    check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    check.bind(("127.0.0.1", port))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def testExtractedPackageKeepsRequirementsLaunchersAndCli(self):
        import subprocess
        import sys
        import zipfile
        from packageApplication import applicationVersion, createApplicationPackage

        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packaged = createApplicationPackage(repositoryPath=repository, outputPath=root / "packages", includeDatabase=False, includeServerFiles=False)
            with zipfile.ZipFile(packaged["archivePath"]) as archive:
                archive.extractall(root / "extracted")
            extracted = root / "extracted" / "No-Part-Finance"
            self.assertIn("waitress>=3.0.2,<4.0", (extracted / "requirements.txt").read_text())
            linux = (extracted / "startLinux.sh").read_text()
            windows = (extracted / "startWindows.bat").read_text()
            self.assertIn("pip install -r requirements.txt", linux)
            self.assertIn("pip install -r requirements.txt", windows)
            self.assertIn('app.py "$@"', linux)
            self.assertIn("app.py %*", windows)
            self.assertFalse((extracted / "platform.conf").exists())
            environment = dict(os.environ, FINANCE_DATABASE_PATH=str(root / "synthetic.db"))
            environment.pop("FINANCE_TEST_SOURCE_DATABASE", None)
            bootstrap = "import tests,runpy,sys; sys.argv=['app.py',sys.argv[1]]; runpy.run_path('app.py',run_name='__main__')"
            version = subprocess.run([sys.executable, "-c", bootstrap, "--version"], cwd=extracted, env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual(applicationVersion, version.stdout.strip())
            self.assertFalse((root / "synthetic.db").exists())
            management = subprocess.run([sys.executable, "-c", bootstrap, "manage-users"], cwd=extracted, env=environment, input="0\n", capture_output=True, text=True, timeout=20)
            self.assertEqual(0, management.returncode, management.stdout + management.stderr)
            self.assertNotIn("Binding server", management.stderr)
            self.assertFalse((extracted / "finance.db").exists())

    def testWaitressEnforcesExistingRequestBodyLimitBeforeApplication(self):
        with tempfile.TemporaryDirectory() as directory:
            application = createApp(Path(directory) / "synthetic.db")
            server = createHttpServer(application, "127.0.0.1", 0)
            self.addCleanup(self.closeFixtureServer, server)

            def verifyLimit(connection):
                try:
                    request = http.client.HTTPConnection("127.0.0.1", int(server.effective_port), timeout=5)
                    try:
                        request.putrequest("POST", "/api/accounts")
                        request.putheader("Content-Length", str(application.config["MAX_CONTENT_LENGTH"] + 1))
                        request.endheaders()
                        response = request.getresponse()
                        self.assertEqual(413, response.status)
                        response.read()
                    finally:
                        request.close()
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
                finally:
                    self.stopServing(server)

            with patch("app.createHttpServer", return_value=server), patch("app.configureOnStartup", side_effect=verifyLimit):
                runApplication(application)
