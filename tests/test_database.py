from tests.test_support import useHistoricalSchema21
import sqlite3
import tempfile
import unittest
from pathlib import Path

import tests  # Activates production-database isolation before importing database code.
from database import connectDatabase, initializeDatabase, migrateToVersion11, schemaVersion
from tests.test_support import connectFixtureDatabase as connectDatabase


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporaryDirectory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.temporaryDirectory.name) / "finance.db"
        initializeDatabase(self.databasePath)
        self.connection = connectDatabase(self.databasePath)

    def tearDown(self):
        self.connection.close()
        self.temporaryDirectory.cleanup()

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
        backups = list((self.databasePath.parent / "backups").glob("pre-migration-v4-to-v26-*.db"))
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


if __name__ == "__main__":
    unittest.main()
