"""Database initialization, verified upgrade backups, and rollback integrity."""

import tests  # Enable the production-database guard before application imports.
from tests.test_support import initializeTestDatabase
from tests.test_support import useHistoricalSchema21
import sqlite3
import tempfile
import unittest
from pathlib import Path
from database import initializeDatabase, migrateToVersion11, schemaVersion
from tests.test_support import snapshotWithoutLoginTime
from unittest.mock import patch
import database
from tests.test_support import connectFixtureDatabase as connectDatabase

class DatabaseTests(unittest.TestCase):
    def setUp(self):
        initializeTestDatabase(self, "finance.db")

    def testInitializationCreatesSchemaWithoutCategories(self):
        tableNames = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {"accounts", "categories", "transactions", "transactionTemplates"}
            <= tableNames
        )
        self.assertEqual(
            0, self.connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        )
        self.assertEqual(
            schemaVersion, self.connection.execute("PRAGMA user_version").fetchone()[0]
        )

    def testRestartPreservesRenamedAndDeletedCategoriesWithoutRecreatingDefaults(self):
        self.connection.executemany(
            "INSERT INTO categories (name, displayOrder) VALUES (?, ?)",
            (("Utilities", 10), ("Needs", 20), ("Delete Me", 30)),
        )
        self.connection.execute(
            "UPDATE categories SET name = ? WHERE name = ?", ("Household Rollup", "Needs")
        )
        self.connection.execute("DELETE FROM categories WHERE name = ?", ("Delete Me",))
        self.connection.commit()
        expectedCategories = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT id, name, parentCategoryId, active, displayOrder, offerReimbursement FROM categories ORDER BY id"
            )
        ]
        self.connection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        self.assertEqual(
            expectedCategories,
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT id, name, parentCategoryId, active, displayOrder, offerReimbursement FROM categories ORDER BY id"
                )
            ],
        )
        self.assertIsNone(
            self.connection.execute("SELECT id FROM categories WHERE name = 'Needs'").fetchone()
        )
        self.assertIsNone(
            self.connection.execute("SELECT id FROM categories WHERE name = 'Delete Me'").fetchone()
        )

    def testForeignKeysAreEnabledAndEnforced(self):
        self.assertEqual(1, self.connection.execute("PRAGMA foreign_keys").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO transactions
                    (accountId, date, description, amountCents)
                VALUES (?, ?, ?, ?)
                """,
                (999, "2026-08-19", "Invalid account", 100),
            )

    def testMoneyRequiresIntegerCentsAndDatesRequireIsoCalendarDates(self):
        self.connection.execute(
            "INSERT INTO accounts (name, accountClass) VALUES (?, ?)",
            ("Checking", "cash"),
        )
        accountId = self.connection.execute(
            "SELECT id FROM accounts WHERE name = ?", ("Checking",)
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO transactions
                    (accountId, date, description, amountCents)
                VALUES (?, ?, ?, ?)
                """,
                (accountId, "2026-08-19", "Float amount", 12.34),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO transactions
                    (accountId, date, description, amountCents)
                VALUES (?, ?, ?, ?)
                """,
                (accountId, "2026-02-30", "Invalid date", 1234),
            )

    def testAccountClassAndSubtypeConstraints(self):
        for name, subtype in (("Card", "Credit Card"), ("Loan", "Personal Loan"), ("Credit Other", "Other")):
            self.connection.execute(
                "INSERT INTO accounts (name, accountClass, accountSubtype) VALUES (?, 'credit', ?)",
                (name, subtype),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO accounts (name, accountClass, accountSubtype)
                VALUES (?, ?, ?)
                """,
                ("Invalid credit", "credit", "checking"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO accounts (name, accountClass) VALUES (?, ?)",
                ("Invalid class", "loan"),
            )

    def testVersionTwoMigrationPreservesLedgerAndAddsReleaseFields(self):
        self.connection.close()
        self.databasePath.unlink()
        oldConnection = sqlite3.connect(self.databasePath)
        oldConnection.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                accountClass TEXT NOT NULL CHECK (accountClass IN ('cash', 'credit')),
                accountSubtype TEXT CHECK (
                    (accountClass = 'cash' AND (accountSubtype IS NULL OR accountSubtype IN ('checking', 'savings', 'other')))
                    OR (accountClass = 'credit' AND accountSubtype IS NULL)
                ), active INTEGER NOT NULL DEFAULT 1, displayOrder INTEGER NOT NULL DEFAULT 0,
                balanceOffsetCents INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                parentCategoryId INTEGER REFERENCES categories(id), active INTEGER NOT NULL DEFAULT 1,
                displayOrder INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY, accountId INTEGER NOT NULL REFERENCES accounts(id),
                date TEXT NOT NULL, description TEXT NOT NULL, amountCents INTEGER NOT NULL,
                categoryId INTEGER REFERENCES categories(id), transferId TEXT
            );
            CREATE TABLE transactionTemplates (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                accountId INTEGER NOT NULL REFERENCES accounts(id), description TEXT NOT NULL,
                categoryId INTEGER NOT NULL REFERENCES categories(id), enteredAmountCents INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, displayOrder INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO accounts VALUES (1, 'Existing Card', 'credit', NULL, 1, 0, -5000);
            INSERT INTO categories VALUES (1, 'Existing Expense', NULL, 1, 0);
            INSERT INTO transactions VALUES (1, 1, '2026-08-01', 'Existing row', -1234, 1, NULL);
            PRAGMA user_version = 2;
            """
        )
        oldConnection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        account = self.connection.execute("SELECT * FROM accounts WHERE id = 1").fetchone()
        transaction = self.connection.execute("SELECT * FROM transactions WHERE id = 1").fetchone()
        self.assertEqual(("credit", "Credit Card", -5000), (account["accountClass"], account["accountSubtype"], account["balanceOffsetCents"]))
        self.assertEqual((-1234, 0), (transaction["amountCents"], transaction["pinned"]))
        self.assertEqual(
            ["Existing Expense"],
            [row["name"] for row in self.connection.execute("SELECT name FROM categories")],
        )
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())

    def testAutomaticUpgradeCreatesVerifiedBackupAndPreservesData(self):
        accountId = self.connection.execute(
            "INSERT INTO accounts (name, accountClass) VALUES ('Preserved', 'cash')"
        ).lastrowid
        useHistoricalSchema21(self.connection)
        self.connection.execute("PRAGMA user_version = 4")
        self.connection.commit()
        self.connection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        backups = list((self.databasePath.parent / "backups").glob("pre-migration-v4-to-v31-*.db"))
        self.assertEqual(1, len(backups))
        self.assertEqual("Preserved", self.connection.execute("SELECT name FROM accounts WHERE id = ?", (accountId,)).fetchone()[0])
        self.assertEqual("ok", self.connection.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())

    def testVersionNineMigrationPreservesBudgetPlansAndAllowsMonthly(self):
        accountId = self.connection.execute(
            "INSERT INTO accounts (name, accountClass) VALUES (?, ?)", ("Checking", "cash")
        ).lastrowid
        categoryId = self.connection.execute(
            "INSERT INTO categories (name) VALUES (?)", ("Expense",)
        ).lastrowid
        templateId = self.connection.execute(
            """INSERT INTO transactionTemplates
               (name, accountId, description, categoryId, enteredAmountCents)
               VALUES (?, ?, ?, ?, ?)""",
            ("Quarterly", accountId, "Quarterly bill", categoryId, 30000),
        ).lastrowid
        planId = self.connection.execute(
            "INSERT INTO sinkingFundPlans (templateId, frequency, expectedDate) VALUES (?, ?, ?)",
            (templateId, "quarterly", "2026-10-31"),
        ).lastrowid
        useHistoricalSchema21(self.connection)
        self.connection.execute("PRAGMA user_version = 8")
        self.connection.commit()
        self.connection.close()
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        plan = self.connection.execute(
            "SELECT templateId, frequency, expectedDate, active FROM sinkingFundPlans WHERE id = ?",
            (planId,),
        ).fetchone()
        self.assertEqual((templateId, "quarterly", "2026-10-31", 1), tuple(plan))
        self.connection.execute(
            "INSERT INTO sinkingFundPlans (templateId, frequency, expectedDate, active) VALUES (?, ?, ?, ?)",
            (templateId, "monthly", "2026-11-30", 0),
        )
        self.assertEqual(schemaVersion, self.connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())

    def testNewerDatabaseVersionIsRefused(self):
        self.connection.execute("PRAGMA user_version = 999")
        self.connection.commit()
        self.connection.close()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)
        self.assertEqual(999, self.connection.execute("PRAGMA user_version").fetchone()[0])

    def testVersionElevenMigrationChecksExistingTemplatePrefillAmount(self):
        legacyPath = Path(self.temporaryDirectory.name) / "legacy-template.db"
        legacyConnection = connectDatabase(legacyPath)
        try:
            legacyConnection.executescript(
                """
                CREATE TABLE transactionTemplates (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, accountId INTEGER,
                    description TEXT, categoryId INTEGER, enteredAmountCents INTEGER,
                    active INTEGER NOT NULL DEFAULT 1, displayOrder INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO transactionTemplates
                    (name, enteredAmountCents) VALUES ('Existing', 1234);
                """
            )
            migrateToVersion11(legacyConnection)
            template = legacyConnection.execute(
                "SELECT enteredAmountCents, prefillAmount FROM transactionTemplates"
            ).fetchone()
            self.assertEqual((1234, 1), tuple(template))
        finally:
            legacyConnection.close()

class UpgradeBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.databasePath = Path(self.directory.name) / "explicit-upgrade.db"
        with sqlite3.connect(self.databasePath) as connection:
            connection.executescript((Path(__file__).parent / "schema21.sql").read_text())
            database.createExternalSystemAccount(connection)
            connection.execute("INSERT INTO accounts (name, accountClass, balanceOffsetCents) VALUES ('Synthetic Cash', 'cash', 12345)")
            connection.execute("INSERT INTO categories (name) VALUES ('Synthetic Expense')")
            connection.execute("INSERT INTO transactions (accountId, date, description, amountCents, categoryId) SELECT a.id, '2026-10-17', 'Synthetic purchase', -321, c.id FROM accounts a, categories c WHERE a.name = 'Synthetic Cash'")
            connection.execute("INSERT INTO users (role, username, usernameKey, passwordHash) VALUES ('owner', 'Fictional', 'fictional', 'fictional-hash')")
            connection.execute("INSERT INTO integrationSecrets (label, secret, active) VALUES ('Fictional revoked', 'fictional-secret', 0)")
            cashId = connection.execute("SELECT id FROM accounts WHERE name = 'Synthetic Cash'").fetchone()[0]
            categoryId = connection.execute("SELECT id FROM categories").fetchone()[0]
            connection.execute("INSERT INTO transactionTemplates (name, accountId, categoryId, enteredAmountCents) VALUES ('Synthetic template', ?, ?, 321)", (cashId, categoryId))
            connection.execute("INSERT INTO flexibleCategoryBudgets (categoryId, monthlyTargetCents) VALUES (?, 5000)", (categoryId,))
            connection.execute("INSERT INTO simulatorPlans (name, planDate) VALUES ('Synthetic plan', '2026-10-17')")
            connection.execute("INSERT INTO simulatorRows (planId, rowType, rowOrder, fromAccountId) VALUES (1, 'live', 0, ?)", (cashId,))
            connection.execute("INSERT INTO pendingSubmissions (integrationId, submissionId, originalContent, reviewContent, reviewVersion, originalRole) VALUES (1, 'synthetic-proposal', '{}', '{}', 4, 'expense')")
            useHistoricalSchema21(connection)
            connection.execute("PRAGMA user_version = 20")
        self.originalState = self.snapshot()

    def snapshot(self):
        connection = sqlite3.connect(self.databasePath)
        try:
            return snapshotWithoutLoginTime(connection)
        finally:
            connection.close()

    def failingConnection(self, failureMode):
        class FaultConnection(sqlite3.Connection):
            def backup(connection, target, **keywords):
                if failureMode == "empty":
                    return
                if failureMode == "corrupt":
                    super().backup(target, **keywords)
                    target.execute("PRAGMA writable_schema = ON")
                    target.execute("UPDATE sqlite_master SET rootpage = 999999 WHERE name = 'accounts'")
                    target.commit()
                    target.execute("PRAGMA writable_schema = OFF")
                    schemaRevision = target.execute("PRAGMA schema_version").fetchone()[0]
                    target.execute(f"PRAGMA schema_version = {schemaRevision + 1}")
                    return
                if failureMode == "partial":
                    target.execute("CREATE TABLE partial (id INTEGER)")
                    target.commit()
                raise RuntimeError("injected backup interruption")
        connection = sqlite3.connect(self.databasePath, factory=FaultConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def testFailedCandidateNeverRestoresOrMigrates(self):
        for failureMode in ["interrupted", "partial", "empty", "corrupt"]:
            with self.subTest(failureMode=failureMode):
                with patch.object(database, "connectDatabase", return_value=self.failingConnection(failureMode)), patch.object(database, "migrateToVersion21") as migration, patch.object(database.sqlite3, "connect", wraps=sqlite3.connect) as connect:
                    with self.assertRaises((RuntimeError, sqlite3.DatabaseError)):
                        database.initializeDatabase(self.databasePath)
                    migration.assert_not_called()
                    self.assertEqual(1, connect.call_count, "Failed candidate must never be reopened for recovery")
                self.assertEqual(self.originalState, self.snapshot())

    def testBackupCreationFailurePreservesSource(self):
        actualConnect = sqlite3.connect
        def failCreation(path, **keywords):
            if "pre-migration" in str(path):
                raise OSError("injected creation failure")
            return actualConnect(path, **keywords)
        with patch.object(database.sqlite3, "connect", side_effect=failCreation), patch.object(database, "migrateToVersion21") as migration:
            with self.assertRaisesRegex(OSError, "creation failure"):
                database.initializeDatabase(self.databasePath)
            migration.assert_not_called()
        self.assertEqual(self.originalState, self.snapshot())

    def testMigrationFailureRestoresCompleteSnapshot(self):
        def failMigration(connection):
            connection.execute("DELETE FROM transactions")
            connection.execute("UPDATE integrationSecrets SET active = 1")
            connection.commit()
            raise RuntimeError("injected migration failure")
        with patch.object(database, "migrateToVersion21", side_effect=failMigration):
            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                database.initializeDatabase(self.databasePath)
        self.assertEqual(self.originalState, self.snapshot())
        self.assertEqual(1, len(list((self.databasePath.parent / "backups").glob("*.db"))))

    def testPostMigrationVerificationFailureRestoresSnapshot(self):
        class VerifyFault(database.LedgerConnection):
            def execute(connection, statement, *arguments):
                if statement == "PRAGMA foreign_key_check":
                    raise RuntimeError("injected final verification failure")
                return super().execute(statement, *arguments)
        connection = sqlite3.connect(self.databasePath, factory=VerifyFault)
        connection.row_factory = sqlite3.Row
        with patch.object(database, "connectDatabase", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "final verification failure"):
                database.initializeDatabase(self.databasePath)
        self.assertEqual(self.originalState, self.snapshot())

    def testRecoveryFailureReportsBothErrorsAndRetainsVerifiedSnapshot(self):
        actualConnect = sqlite3.connect
        def failRecovery(path, **keywords):
            if str(path).startswith("file:"):
                raise OSError("injected recovery failure")
            return actualConnect(path, **keywords)
        with patch.object(database.sqlite3, "connect", side_effect=failRecovery), patch.object(database, "migrateToVersion21", side_effect=RuntimeError("injected migration failure")):
            with self.assertRaisesRegex(RuntimeError, "migration failure.*recovery failed.*recovery failure.*verified backup retained"):
                database.initializeDatabase(self.databasePath)
        backupPaths = list((self.databasePath.parent / "backups").glob("*.db"))
        self.assertEqual(1, len(backupPaths))
        with actualConnect(backupPaths[0]) as snapshot:
            self.assertEqual(self.originalState, list(snapshot.iterdump()))

    def testSuccessfulUpgradeAndCurrentInitialization(self):
        database.initializeDatabase(self.databasePath)
        database.initializeDatabase(self.databasePath)
        with connectDatabase(self.databasePath) as connection:
            self.assertEqual(database.schemaVersion, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(12345, connection.execute("SELECT balanceOffsetCents FROM accounts WHERE name = 'Synthetic Cash'").fetchone()[0])
        self.assertEqual(1, len(list((self.databasePath.parent / "backups").glob("*.db"))))

    def testNewerSchemaRejectedWithoutBackup(self):
        with connectDatabase(self.databasePath) as connection:
            connection.execute(f"PRAGMA user_version = {database.schemaVersion + 1}")
        originalState = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            database.initializeDatabase(self.databasePath)
        self.assertEqual(originalState, self.snapshot())
        self.assertFalse((self.databasePath.parent / "backups").exists())

    def testNewDatabaseDoesNotCreateMigrationBackup(self):
        newPath = Path(self.directory.name) / "new-installation" / "explicit-new.db"
        database.initializeDatabase(newPath)
        self.assertFalse((newPath.parent / "backups").exists())


if __name__ == "__main__":
    unittest.main()
