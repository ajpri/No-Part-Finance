"""Trusted CLI User setup, deletion, recovery backups, and migration preservation."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import useHistoricalSchema21
import getpass
import runpy
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from app import createApp
from database import ValidationError, initializeDatabase, createAccount, createCategory, createTransaction, createTransfer, createTemplate, createIntegrationSecret, submitPendingEntry, schemaVersion
from users import configureOnStartup, manageUsers
from unittest.mock import patch
import database as ledger
from users import createUser, createOwner
from tests.test_support import connectFixtureDatabase as connectDatabase
from tests.test_support import signIn
from tests.test_support import PwaFixture

class UserManagementAndMigrationTests(unittest.TestCase):
    def testUsernamePasswordConstraintsAndCasefoldUniqueness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-users.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                for username in ["", "x" * 65, "bad\nname"]:
                    with self.assertRaises(ValidationError):
                        createUser(connection, "owner", username, "valid password", firstName="Test")
                for password in ["short", "x" * 129]:
                    with self.assertRaises(ValidationError):
                        createUser(connection, "owner", "Owner", password, firstName="Owner")
                createUser(connection, "owner", "  Straße  ", "  password α  ", firstName="Straße")
                self.assertEqual("Straße", connection.execute("SELECT username FROM users").fetchone()[0])
                with self.assertRaises(ValidationError):
                    createUser(connection, "auditor", "STRASSE", "valid password", firstName="STRASSE")
                with self.assertRaises(ValidationError):
                    createUser(connection, "owner", "Other", "valid password", firstName="Other")
            finally:
                connection.close()

    def testStartupDeclineInterruptionAndOptionalAuditorFailure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-setup.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                for response in ["", "n", EOFError(), KeyboardInterrupt(), OSError()]:
                    with patch("users.input", side_effect=[response]), patch("builtins.print") as output:
                        configureOnStartup(connection)
                    setupText = str(output.call_args_list)
                    self.assertIn("cannot sign in", setupText)
                    self.assertIn("Restart to retry Owner setup", setupText)
                    self.assertIn("startWindows.bat manage-users", setupText)
                    self.assertIn("./startLinux.sh manage-users", setupText)
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                with patch("users.input", side_effect=["y", "Owner", "Owner", "", "y", "Auditor", "Auditor", ""]), patch("users.getpass.getpass", side_effect=["valid password", "valid password", "mismatch", "different"]), patch("builtins.print") as output:
                    configureOnStartup(connection)
                setupText = str(output.call_args_list)
                self.assertIn("Your Owner is saved and can sign in", setupText)
                self.assertIn("Optional Auditor setup did not complete", setupText)
                self.assertIn("Passwords do not match", setupText)
                self.assertNotIn("No Owner was created", setupText)
                self.assertNotIn("valid password", setupText)
                self.assertNotIn("mismatch", setupText)
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
                with connection:
                    connection.execute("DROP TRIGGER preserveOwnerMembership")
                    connection.execute("DROP TRIGGER userEventsNoDELETE")
                    connection.execute("DELETE FROM userEvents")
                    connection.execute("DELETE FROM users")
                originalCreateUser = createUser
                def failOptionalAuditor(connection, role, username, password, firstName="", lastName=""):
                    if role == "auditor":
                        raise sqlite3.OperationalError("fixture write failure")
                    return originalCreateUser(connection, role, username, password, firstName, lastName)
                with patch("users.input", side_effect=["y", "Owner", "Owner", "", "y", "Auditor", "Auditor", ""]), patch("users.getpass.getpass", return_value="valid password"), patch("users.createUser", side_effect=failOptionalAuditor), patch("builtins.print") as output:
                    configureOnStartup(connection)
                self.assertIn("Your Owner is saved and can sign in", str(output.call_args_list))
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
                with patch("users.input", side_effect=["1", "1", "2", "0", "0"]), patch("users.getpass.getpass", side_effect=["new owner pass", "new owner pass", "auditor pass", "auditor pass", "new auditor pass", "new auditor pass"]), patch("builtins.print") as output:
                    manageUsers(path)
                    text = str(output.call_args_list)
                    self.assertNotIn("new owner pass", text)
                    self.assertNotIn("scrypt:", text)
                    self.assertIn("User management does not start the server", text)
                    self.assertIn("without manage-users", text)
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
            finally:
                connection.close()

    def testProtectedInputFailureAndSuccessfulSetupRetry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-protected-input.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                def unavailablePasswordInput(prompt):
                    warnings.warn("Password input may be echoed", getpass.GetPassWarning)
                    self.fail("Unprotected password input must never proceed")

                with patch("users.input", side_effect=["y", "Example Owner", "Example", ""]), patch("users.getpass.getpass", side_effect=unavailablePasswordInput), patch("builtins.print") as output:
                    configureOnStartup(connection)
                self.assertIn("Protected password input is unavailable", str(output.call_args_list))
                self.assertIn("No Owner was created", str(output.call_args_list))
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

                with patch("users.input", side_effect=["y", "Example Owner", "Example", "", ""]), patch("users.getpass.getpass", return_value="  password α  ") as passwordInput, patch("builtins.print") as output:
                    configureOnStartup(connection)
                setupText = str(output.call_args_list)
                self.assertIn("8–128 characters", setupText)
                self.assertIn("typing shows no characters", setupText)
                self.assertIn("Auditor setup skipped", setupText)
                self.assertNotIn("  password α  ", setupText)
                self.assertEqual([("Password: ",), ("Confirm password: ",)], [call.args for call in passwordInput.call_args_list])
                client = createApp(path).test_client()
                self.assertEqual(302, signIn(client, "Example Owner", "  password α  ").status_code)
                with patch("users.input") as startupInput, patch("builtins.print") as output:
                    configureOnStartup(connection)
                startupInput.assert_not_called()
                output.assert_not_called()

                with patch("users.input", side_effect=["3", "1", "Example Auditor", "Example", ""]), patch("users.getpass.getpass", side_effect=unavailablePasswordInput), patch("builtins.print") as output:
                    manageUsers(path)
                self.assertIn("User management closed", str(output.call_args_list))
                self.assertIn("interactive Command Prompt or terminal", str(output.call_args_list))
                self.assertEqual(["owner"], [row[0] for row in connection.execute("SELECT role FROM scopedusers")])
                with patch("users.input", side_effect=["3", "1", "Example Auditor", "Example", "", "0"]), patch("users.getpass.getpass", return_value="auditor password"), patch("builtins.print"):
                    manageUsers(path)
                self.assertEqual(302, signIn(client, "Example Auditor", "auditor password").status_code)
                self.assertEqual(403, client.post("/api/accounts", json={}).status_code)
            finally:
                connection.close()

    def testStartupExplainsBrowserAccessAfterSkippedOrCompletedSetup(self):
        scenarios = (
            ([""], []),
            (["y", "Example Owner", "Example", "", ""], ["owner"]),
            (["y", "Example Owner", "Example", "", "y", "Example Auditor", "Example", ""], ["auditor", "owner"]),
            (["y", "Example Owner", "Example", "", KeyboardInterrupt()], ["owner"]),
        )
        for responses, expectedRoles in scenarios:
            with self.subTest(expectedRoles=expectedRoles, interrupted=isinstance(responses[-1], KeyboardInterrupt)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "explicit-startup.db"
                with patch.dict("os.environ", {"FINANCE_DATABASE_PATH": str(path)}), patch("sys.argv", ["app.py"]), patch("users.input", side_effect=responses), patch("users.getpass.getpass", return_value="startup password"), patch("waitress.create_server") as runServer, patch("waitress.task.ThreadedTaskDispatcher") as dispatcher, patch("builtins.print") as output, self.assertLogs(level="INFO") as startupLogs:
                    runServer.return_value.adj.threads = 4
                    runServer.return_value.task_dispatcher = dispatcher.return_value
                    runpy.run_path(str(Path(__file__).resolve().parents[1] / "app.py"), run_name="__main__")
                runServer.assert_called_once()
                self.assertEqual("0.0.0.0", runServer.call_args.kwargs["host"])
                self.assertEqual(5000, runServer.call_args.kwargs["port"])
                self.assertFalse(runServer.call_args.kwargs["clear_untrusted_proxy_headers"])
                runServer.return_value.run.assert_called_once()
                dispatcher.return_value.shutdown.assert_called_once()
                logText = "\n".join(startupLogs.output)
                self.assertIn("open http://127.0.0.1:5000", logText)
                self.assertIn("Keep this terminal open", logText)
                self.assertNotIn("startup password", logText + str(output.call_args_list))
                connection = connectDatabase(path)
                try:
                    self.assertEqual(expectedRoles, [row[0] for row in connection.execute("SELECT role FROM scopedusers ORDER BY role")])
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
                    self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                finally:
                    connection.close()

    def testIntegrationMigrationPreservesPendingContentCredentialsAndLedger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-v19-migration.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                createUser(connection, "owner", "Migration Owner", "owner password", firstName="Migration Owner")
                cashId = createAccount(connection, "Migration Cash", "cash", openingBalance="100.00")
                categoryId = createCategory(connection, "Migration Expense")
                createTransaction(connection, cashId, "2026-09-30", "Existing ledger", categoryId, "10.00")
                secretId = createIntegrationSecret(connection, "Migration Agent")
                proposal, _ = submitPendingEntry(connection, secretId, {"submissionId": "existing", "date": "2026-09-30", "accountId": cashId, "description": "Existing pending", "categoryId": categoryId, "amount": "5.00"})
                useHistoricalSchema21(connection)
                with connection:
                    connection.execute("ALTER TABLE pendingSubmissions DROP COLUMN reviewContent")
                    connection.execute("ALTER TABLE pendingSubmissions DROP COLUMN reviewVersion")
                    useHistoricalSchema21(connection)
                    connection.execute("PRAGMA user_version = 19")
                before = {row[0]: [dict(record) for record in connection.execute('SELECT * FROM "' + row[0] + '"')] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                connection.close()
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                for table, records in before.items():
                    after = [dict(row) for row in connection.execute('SELECT * FROM ' + ('scopedusers' if table == 'users' else '"' + table + '"'))]
                    if table == "pendingSubmissions":
                        self.assertEqual(None, after[0].pop("reviewContent"))
                        self.assertEqual(0, after[0].pop("reviewVersion"))
                    after = [{key: row[key] for key in records[index]} for index, row in enumerate(after)]
                    if table == "sqlite_sequence":
                        records = sorted(records, key=lambda row: row["name"])
                        after = sorted(after, key=lambda row: row["name"])
                    self.assertEqual(records, after, table)
                self.assertEqual(schemaVersion, connection.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()
            backups = list((Path(directory) / "backups").glob("pre-migration-v19-to-v31-*.db"))
            self.assertEqual(1, len(backups))

    def testMigrationPreservesEveryExistingTableAndBackup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit-migration.db"
            initializeDatabase(path)
            connection = connectDatabase(path)
            cashId = createAccount(connection, "Fixture Cash", "cash", openingBalance="123.45")
            creditId = createAccount(connection, "Fixture Credit", "credit", "Credit Card")
            categoryId = createCategory(connection, "Fixture Expense")
            createTransaction(connection, cashId, "2026-10-01", "Fixture", categoryId, "10.00")
            createTransfer(connection, "2026-10-01", cashId, creditId, "5.00")
            createTemplate(connection, "Fixture Template", cashId, "Fixture", categoryId, "10.00")
            useHistoricalSchema21(connection)
            before = {row[0]: [tuple(record) for record in connection.execute('SELECT * FROM "' + row[0] + '"')] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('users', 'integrationSecrets', 'pendingSubmissions', 'sqlite_sequence')")}
            with connection:
                connection.execute("DROP TABLE pendingSubmissions")
                connection.execute("DROP TABLE integrationSecrets")
                connection.execute("DROP TABLE users")
                useHistoricalSchema21(connection)
                connection.execute("PRAGMA user_version = 18")
            connection.close()
            initializeDatabase(path)
            connection = connectDatabase(path)
            try:
                self.assertEqual(schemaVersion, connection.execute("PRAGMA user_version").fetchone()[0])
                for table, records in before.items():
                    self.assertEqual(records, [tuple(row)[1:] for row in connection.execute('SELECT * FROM "' + table + '"')])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()
            backups = list((Path(directory) / "backups").glob("pre-migration-v18-to-v31-*.db"))
            self.assertEqual(1, len(backups))
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(18, backup.execute("PRAGMA user_version").fetchone()[0])
            finally:
                backup.close()

class CliDeletionTests(PwaFixture, unittest.TestCase):
    def testDeletionBackupAndUserHistory(self):
        from cliDeletion import deleteTarget
        transactionId = ledger.createTransaction(self.connection, self.accountId, "2026-10-27", "Keep history", self.categoryId, "3")
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions")]
        backupDirectory = Path(self.directory.name) / "deletion-backups"
        with self.assertRaisesRegex(ledger.ValidationError, "owned ledgers"):
            deleteTarget(self.path, "user", self.ownerId, str(self.ownerId), backupDirectory)
        self.assertFalse(backupDirectory.exists())
        auditorSession = self.login("Auditor", "auditor password")
        backup = deleteTarget(self.path, "user", self.auditorId, str(self.auditorId), backupDirectory)
        with sqlite3.connect(backup) as saved:
            self.assertEqual("Auditor", saved.execute("SELECT username FROM users WHERE id = ?", (self.auditorId,)).fetchone()[0])
            self.assertEqual("ok", saved.execute("PRAGMA integrity_check").fetchone()[0])
        deleted = self.connection.execute("SELECT * FROM users WHERE id = ?", (self.auditorId,)).fetchone()
        self.assertEqual(("Deleted user", "", 0, 1), (deleted["username"], deleted["passwordHash"], deleted["enabled"], deleted["deleted"]))
        self.assertEqual(401, self.client.get("/api/session").status_code)
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions")])
        newUser = createUser(self.connection, "auditor", "Auditor", "new password", firstName="New")
        self.assertNotEqual(self.auditorId, newUser)
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("UPDATE users SET enabled = 1 WHERE id = ?", (self.auditorId,))

    def testLedgerDeletionPreservesOtherLedgerAndBackup(self):
        from cliDeletion import deleteTarget
        from users import createOwner
        foreignOwner = createOwner(self.connection, "Other", "other password", "Other", "")
        foreignLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (foreignOwner,)).fetchone()[0]
        ledger.bindLedger(self.connection, foreignLedger)
        foreignAccount = ledger.createAccount(self.connection, "Other cash", "cash", openingBalance="7")
        foreignBefore = [tuple(row) for row in self.connection.execute("SELECT * FROM accounts WHERE ledgerId = ?", (foreignLedger,))]
        backup = deleteTarget(self.path, "ledger", self.ledgerId, self.ledgerId, Path(self.directory.name) / "backups")
        self.assertTrue(backup.exists())
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM ledgers WHERE id = ?", (self.ledgerId,)).fetchone()[0])
        self.assertEqual(foreignBefore, [tuple(row) for row in self.connection.execute("SELECT * FROM accounts WHERE ledgerId = ?", (foreignLedger,))])
        self.assertTrue(self.connection.execute("SELECT 1 FROM users WHERE id = ?", (self.ownerId,)).fetchone())
        self.assertIsNone(self.connection.execute("PRAGMA foreign_key_check").fetchone())
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("DELETE FROM ledgerAccess WHERE ledgerId = ? AND role = 'owner'", (foreignLedger,))

    def testDeletionCancellationBackupFailureAndRollback(self):
        from cliDeletion import deleteTarget
        backupDirectory = Path(self.directory.name) / "backups"
        with self.assertRaises(ledger.ValidationError):
            deleteTarget(self.path, "ledger", self.ledgerId, "wrong", backupDirectory)
        self.assertFalse(backupDirectory.exists())
        with patch("cliDeletion.databaseBackup", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                deleteTarget(self.path, "ledger", self.ledgerId, self.ledgerId, backupDirectory)
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectAccountDelete BEFORE DELETE ON accounts BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            deleteTarget(self.path, "ledger", self.ledgerId, self.ledgerId, backupDirectory)
        self.assertTrue(self.connection.execute("SELECT 1 FROM ledgers WHERE id = ?", (self.ledgerId,)).fetchone())
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'auditEventsNoDELETE'").fetchone()[0])
        self.assertIsNone(self.connection.execute("PRAGMA foreign_key_check").fetchone())

    def testDeletedUserCannotWriteFromPreviouslyAuthorizedConnection(self):
        from cliDeletion import deleteTarget
        stale = ledger.connectDatabase(self.path)
        self.addCleanup(stale.close)
        ledger.bindLedger(stale, self.ledgerId, "user", self.auditorId, "Auditor")
        deleteTarget(self.path, "user", self.auditorId, str(self.auditorId), Path(self.directory.name) / "backups")
        with self.assertRaises(ledger.ValidationError):
            ledger.createAccount(stale, "Should not be created", "cash")


if __name__ == "__main__":
    unittest.main()
