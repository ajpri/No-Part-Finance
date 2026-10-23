from io import BytesIO
from packageApplication import pwaIconFiles
from tests.test_support import ownerClient
import sqlite3
import secrets
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import tests  # Activates production-database isolation before importing the application.
from app import createApp
from database import connectDatabase, createAccount, createCategory, createTransaction, createIntegrationSecret, initializeDatabase
from packageApplication import (
    PackagingError,
    applicationVersion,
    archiveRootName,
    createApplicationPackage,
    includedFiles,
    main,
)
from tests.test_support import connectFixtureDatabase as connectDatabase


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporaryDirectory.name)
        self.repositoryPath = self.root / "repository"
        self.repositoryPath.mkdir()
        for relativePath in includedFiles:
            sourcePath = self.repositoryPath / relativePath
            sourcePath.parent.mkdir(parents=True, exist_ok=True)
            sourcePath.write_text(f"maintained file: {relativePath}\n", encoding="utf-8")
        self.databasePath = self.repositoryPath / "finance.db"
        initializeDatabase(self.databasePath)
        connection = connectDatabase(self.databasePath)
        try:
            createAccount(connection, "Checking", "cash", "checking", openingBalance="123.45")
        finally:
            connection.close()
        self.createdAt = datetime(2026, 8, 22, 14, 25, 30)

    def tearDown(self):
        self.temporaryDirectory.cleanup()

    def createPackage(self, **overrides):
        arguments = {
            "repositoryPath": self.repositoryPath,
            "databasePath": self.databasePath,
            "outputPath": self.root / "output",
            "createdAt": self.createdAt,
        }
        arguments.update(overrides)
        return createApplicationPackage(**arguments)

    def testProductDocumentationIsIncluded(self):
        self.assertIn("SPEC.md", includedFiles)
        self.assertIn("ROADMAP.md", includedFiles)
        self.assertIn("PROMPTS.md", includedFiles)
        self.assertIn("AGENTS.md", includedFiles)

    def testNamingContentsAndExplicitExclusions(self):
        excludedPaths = (
            ".git/config",
            ".venv/secret.txt",
            "__pycache__/app.pyc",
            "packages/old.zip",
            "backups/old.db",
            "finance.db-wal",
            "ExpenseTracker.csv",
            "SPEC.md.diff.txt",
            "server.key",
            ".env",
            "unlisted.txt",
        )
        for relativePath in excludedPaths:
            artifactPath = self.repositoryPath / relativePath
            artifactPath.parent.mkdir(parents=True, exist_ok=True)
            artifactPath.write_text("excluded", encoding="utf-8")
        result = self.createPackage()
        self.assertEqual(
            f"No-Part-Finance-{applicationVersion}-2026-08-22-142530.zip",
            result["archivePath"].name,
        )
        with zipfile.ZipFile(result["archivePath"]) as archive:
            names = set(archive.namelist())
            expected = {f"{archiveRootName}/{path}" for path in includedFiles}
            expected.add(f"{archiveRootName}/finance.db")
            expected.add(f"{archiveRootName}/platform.conf.sample")
            self.assertEqual(expected, names)
            linuxMode = archive.getinfo(f"{archiveRootName}/packageLinux.sh").external_attr >> 16
            self.assertEqual(0o755, linuxMode & 0o777)

    def testRelativeOutputDirectoryAndCollisionSuffix(self):
        first = self.createPackage(outputPath="portable")
        second = self.createPackage(outputPath="portable")
        self.assertEqual((self.repositoryPath / "portable").resolve(), first["archivePath"].parent)
        self.assertTrue(first["archivePath"].name.endswith("142530.zip"))
        self.assertTrue(second["archivePath"].name.endswith("142530-2.zip"))

    def testDatabaseIsBackedUpAndVerifiedWithoutChangingSource(self):
        originalBytes = self.databasePath.read_bytes()
        result = self.createPackage()
        extractedPath = self.root / "extracted.db"
        with zipfile.ZipFile(result["archivePath"]) as archive:
            extractedPath.write_bytes(archive.read(f"{archiveRootName}/finance.db"))
        connection = sqlite3.connect(extractedPath)
        try:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(originalBytes, self.databasePath.read_bytes())

    def testPackageWithoutDatabaseDoesNotRequireOrIncludeDatabase(self):
        self.databasePath.unlink()
        result = self.createPackage(includeDatabase=False)
        self.assertFalse(result["includesDatabase"])
        self.assertIn("-no-db-", result["archivePath"].name)
        with zipfile.ZipFile(result["archivePath"]) as archive:
            self.assertNotIn(f"{archiveRootName}/finance.db", archive.namelist())
            self.assertEqual(
                {f"{archiveRootName}/{path}" for path in includedFiles} | {f"{archiveRootName}/platform.conf.sample"} | {f"{archiveRootName}/{path}" for path in pwaIconFiles if (self.repositoryPath / path).is_file()},
                set(archive.namelist()),
            )

    def testInvalidOrMissingDatabaseLeavesNoArchive(self):
        outputPath = self.root / "failed-output"
        self.databasePath.write_bytes(b"not sqlite")
        with self.assertRaises(PackagingError):
            self.createPackage(outputPath=outputPath)
        self.assertEqual([], list(outputPath.glob("*.zip")))
        self.databasePath.unlink()
        with self.assertRaises(PackagingError):
            self.createPackage(outputPath=outputPath)
        self.assertEqual([], list(outputPath.glob("*.zip")))

    def testMissingOutputDirectoryIsCreated(self):
        outputPath = self.root / "missing" / "nested"
        result = self.createPackage(outputPath=outputPath)
        self.assertEqual(outputPath.resolve(), result["archivePath"].parent)

    def testOutputPathMustBeWritableDirectory(self):
        outputPath = self.root / "not-a-directory"
        outputPath.write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(PackagingError, "not writable"):
            self.createPackage(outputPath=outputPath)

    def testHelpDocumentsOptionsWarningAndPlatformExamples(self):
        output = StringIO()
        with self.assertRaises(SystemExit) as exitContext, redirect_stdout(output):
            main(["--help"])
        self.assertEqual(0, exitContext.exception.code)
        helpText = output.getvalue()
        normalizedHelp = " ".join(helpText.split())
        self.assertIn("--output PATH", helpText)
        self.assertIn("--no-db", helpText)
        self.assertIn("complete unencrypted financial database", normalizedHelp)
        self.assertIn("packageWindows.bat", helpText)
        self.assertIn("./packageLinux.sh", helpText)


class PackageEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        root = Path(self.temporaryDirectory.name)
        self.databasePath = root / "finance.db"
        self.outputPath = root / "packages"
        self.app = createApp(self.databasePath)
        self.app.config["PACKAGE_OUTPUT_DIRECTORY"] = str(self.outputPath)
        self.client = ownerClient(self.app)

    def tearDown(self):
        self.temporaryDirectory.cleanup()

    def testPackageEndpointRetainsAndReturnsCompleteArchiveWithoutCaching(self):
        response = self.client.get("/api/package")
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/zip", response.mimetype)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        archives = list(self.outputPath.glob("*.zip"))
        self.assertEqual(0, len(archives))
        with zipfile.ZipFile(BytesIO(response.get_data())) as archive:
            self.assertEqual(
                {f"{archiveRootName}/{path}" for path in includedFiles} | {f"{archiveRootName}/platform.conf.sample"} | {f"{archiveRootName}/finance.db"} | {f"{archiveRootName}/{path}" for path in pwaIconFiles if (Path(__file__).resolve().parents[1] / path).is_file()},
                set(archive.namelist()),
            )
        response.close()

    def testPackageEndpointFailureReturnsNoArchive(self):
        with patch("app.createApplicationPackage", side_effect=PackagingError("validation failed")):
            response = self.client.get("/api/package")
        self.assertEqual(500, response.status_code)
        self.assertEqual({"error": "validation failed"}, response.get_json())
        self.assertFalse(self.outputPath.exists())

    def testPackageWithoutDatabaseEndpointReturnsApplicationOnlyArchive(self):
        response = self.client.get("/api/package-no-db")
        self.assertEqual(200, response.status_code)
        self.assertIn("-no-db-", response.headers["Content-Disposition"])
        archives = list(self.outputPath.glob("*.zip"))
        self.assertEqual(0, len(archives))
        with zipfile.ZipFile(BytesIO(response.get_data())) as archive:
            self.assertNotIn(f"{archiveRootName}/finance.db", archive.namelist())
            self.assertEqual(
                {f"{archiveRootName}/{path}" for path in includedFiles} | {f"{archiveRootName}/platform.conf.sample"} | {f"{archiveRootName}/{path}" for path in pwaIconFiles if (Path(__file__).resolve().parents[1] / path).is_file()},
                set(archive.namelist()),
            )
        response.close()


class ActualDistributionTests(unittest.TestCase):
    def testActualApplicationOnlyArchiveExcludesPrivateStateAndRunsExtracted(self):
        repositoryPath = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="npf-release-verification-") as directory:
            root = Path(directory)
            syntheticPath = root / "synthetic.db"
            application = createApp(syntheticPath)
            ownerClient(application)
            connection = connectDatabase(syntheticPath)
            try:
                accountId = createAccount(connection, "Fictional release wallet", "cash", openingBalance="500.00")
                categoryId = createCategory(connection, "Fictional release Expense")
                historyMarker = secrets.token_urlsafe(32)
                createTransaction(connection, accountId, "2026-10-01", historyMarker, categoryId, "12.50")
                secretId = createIntegrationSecret(connection, "Fictional release agent")
                secret = connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (secretId,)).fetchone()[0]
                passwordHash = connection.execute("SELECT passwordHash FROM users").fetchone()[0]
            finally:
                connection.close()
            originalBytes = syntheticPath.read_bytes()
            expectedNames = {f"{archiveRootName}/{relativePath}" for relativePath in includedFiles} | {f"{archiveRootName}/platform.conf.sample"} | {f"{archiveRootName}/{path}" for path in pwaIconFiles if (repositoryPath / path).is_file()}
            for sourcePath in (syntheticPath, root / "absent.db"):
                with self.subTest(sourceExists=sourcePath.exists()), patch("packageApplication.createVerifiedDatabaseCopy", side_effect=AssertionError("Application-only packaging must not open a database")):
                    result = createApplicationPackage(repositoryPath, sourcePath, root / "public", includeDatabase=False, includeServerFiles=False)
                with zipfile.ZipFile(result["archivePath"]) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(expectedNames, set(archive.namelist()))
                    for name in archive.namelist():
                        self.assertFalse(any(part in {".git", ".venv", "backups", "__pycache__"} for part in Path(name).parts))
                        self.assertNotIn(Path(name).suffix.lower(), {".db", ".csv", ".key", ".pem", ".pyc"})
                        contents = archive.read(name)
                        self.assertFalse(any(value.encode() in contents for value in (historyMarker, secret, passwordHash)), "Private synthetic state leaked into public archive")
                    readme = archive.read(f"{archiveRootName}/README.md").decode()
                    self.assertIn("application-only", readme)
                    self.assertIn("-no-db-", readme)
                    self.assertIn("recoverable credentials", readme)
            private = createApplicationPackage(repositoryPath, syntheticPath, root / "private", includeServerFiles=False)
            with zipfile.ZipFile(private["archivePath"]) as archive:
                self.assertNotIn("-no-db-", private["archivePath"].name)
                self.assertEqual(expectedNames | {f"{archiveRootName}/finance.db"}, set(archive.namelist()))
                privatePath = root / "private-copy.db"
                privatePath.write_bytes(archive.read(f"{archiveRootName}/finance.db"))
            connection = connectDatabase(privatePath)
            try:
                self.assertEqual(secret, connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (secretId,)).fetchone()[0])
                self.assertEqual(historyMarker, connection.execute("SELECT description FROM transactions").fetchone()[0])
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()
            self.assertEqual(originalBytes, syntheticPath.read_bytes())
            with zipfile.ZipFile(result["archivePath"]) as archive:
                archive.extractall(root / "extracted")
            extractedPath = root / "extracted" / archiveRootName
            self.assertFalse((extractedPath / "finance.db").exists())
            completed = subprocess.run([
                sys.executable, "-m", "unittest", "-v",
                "tests.test_startup_serving",
                "tests.test_app.ApplicationTests.testFreshOrdinaryEntryPreservesOpeningBalanceSignsAndRestart",
                "tests.test_app.ApplicationTests.testMinimalIndependentConfigurationSupportsAllOrdinaryEntrySigns",
                "tests.test_release_2610.UserManagementAndMigrationTests.testProtectedInputFailureAndSuccessfulSetupRetry",
                "tests.test_release_2610.UserManagementAndMigrationTests.testStartupExplainsBrowserAccessAfterSkippedOrCompletedSetup",
            ], cwd=extractedPath, capture_output=True, text=True, timeout=120)
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("Ran 9 tests", completed.stderr)
            self.assertFalse((extractedPath / "finance.db").exists())


if __name__ == "__main__":
    unittest.main()
