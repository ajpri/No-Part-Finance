import hashlib
import os
import sqlite3
from pathlib import Path

import tests  # Activates production-database isolation before importing database code.
from database import createCategory


testCategoryDefinitions = (
    ("Needs", None, 10, False),
    ("Recurring Needs", "Needs", 11, False),
    ("Groceries/Gas/Household", "Needs", 12, False),
    ("Random Needs", "Needs", 13, False),
    ("Interest", "Needs", 14, False),
    ("Wants", None, 20, False),
    ("Experiences", None, 30, False),
    ("Giving", None, 40, False),
    ("Savings/Future", None, 50, False),
    ("Pass-Through", None, 60, True),
    ("Income/Reimbursement", None, 70, False),
    ("Utilities", None, 80, False),
)


def createScenarioCategories(connection):
    """Create explicit category fixtures for tests that exercise the August behavior."""
    categoryIds = {}
    for name, parentName, displayOrder, offerReimbursement in testCategoryDefinitions:
        categoryIds[name] = createCategory(
            connection,
            name,
            categoryIds.get(parentName),
            displayOrder,
            offerReimbursement,
            "income" if name == "Income/Reimbursement" else "expense",
        )
    connection.execute(
        "INSERT OR REPLACE INTO applicationSettings (key, value) VALUES ('reimbursementCategoryId', ?)",
        (str(categoryIds["Income/Reimbursement"]),),
    )
    connection.commit()
    return categoryIds


def ownerClient(application, copiedDatabase=False):
    """Use the real sign-in and CSRF flow for legacy financial regression tests."""
    from users import createUser, resetPassword
    username = "Test Owner"
    connection = connectDatabaseForOwner(application)
    try:
        owner = connection.execute("SELECT username FROM scopedusers WHERE role = 'owner'").fetchone()
        if not owner:
            createUser(connection, "owner", "Test Owner", "test-owner-password", firstName="Test Owner")
        elif copiedDatabase:
            # Only disposable integration copies receive a known test password.
            # Preserve the copied identity and never require the source Owner's credentials.
            username = owner["username"]
            resetPassword(connection, connection.execute("SELECT id FROM scopedusers WHERE role = 'owner'").fetchone()[0], "test-owner-password")
    finally:
        connection.close()
    client = application.test_client()
    signInHtml = client.get("/sign-in").get_data(as_text=True)
    import re
    csrfToken = re.search(r'name="csrfToken" value="([^"]+)"', signInHtml).group(1)
    response = client.post("/sign-in", data={"username": username, "password": "test-owner-password", "csrfToken": csrfToken})
    if response.status_code != 302:
        raise AssertionError("Test Owner login failed")
    client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
    return client


def connectDatabaseForOwner(application):
    return connectFixtureDatabase(application.config["DATABASE_PATH"])


def openReadOnlyDatabase(databasePath):
    databaseUri = Path(databasePath).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(databaseUri, uri=True)


def copyProductionDatabase(testCase, destinationPath):
    configuredSourcePath = os.environ.get("FINANCE_TEST_SOURCE_DATABASE")
    if not configuredSourcePath:
        testCase.skipTest(
            "Real-data integration requires an explicit FINANCE_TEST_SOURCE_DATABASE path"
        )
    sourcePath = Path(configuredSourcePath).resolve()
    if not sourcePath.exists():
        testCase.skipTest("Configured real-data integration source is absent")
    destinationPath = Path(destinationPath).resolve()
    if destinationPath == sourcePath.resolve():
        raise AssertionError("Production integration destination must be a separate temporary path")
    destinationPath.parent.mkdir(parents=True, exist_ok=True)
    sourceHashBefore = hashlib.sha256(sourcePath.read_bytes()).digest()
    sourceConnection = openReadOnlyDatabase(sourcePath)
    targetConnection = sqlite3.connect(destinationPath)
    try:
        sourceConnection.backup(targetConnection)
    finally:
        sourceConnection.close()
        targetConnection.close()
    sourceHashAfter = hashlib.sha256(sourcePath.read_bytes()).digest()
    testCase.assertEqual(sourceHashBefore, sourceHashAfter, "Production finance.db changed during read-only backup")


def connectFixtureDatabase(databasePath):
    """Explicit single-ledger synthetic fixtures; legacy migration inputs stay raw."""
    from database import connectDatabase, bindInitialLedger
    connection = connectDatabase(databasePath)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'ledgers'").fetchone():
        bindInitialLedger(connection)
    return connection


def initializeTestDatabase(testCase, filename="finance.db"):
    """Own a fresh synthetic database and close it before removing its directory."""
    import tempfile
    from database import initializeDatabase

    testCase.temporaryDirectory = tempfile.TemporaryDirectory()
    testCase.addCleanup(testCase.temporaryDirectory.cleanup)
    testCase.databasePath = Path(testCase.temporaryDirectory.name) / filename
    initializeDatabase(testCase.databasePath)
    testCase.connection = connectFixtureDatabase(testCase.databasePath)
    # Restart tests replace the connection, so cleanup must close the current handle.
    testCase.addCleanup(lambda: testCase.connection.close())


def useHistoricalSchema21(connection):
    """Rebuild a synthetic fixture in its genuine pre-ledger schema before downgrades."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'ledgers'").fetchone():
        return
    connection.commit()
    historical = sqlite3.connect(":memory:")
    historical.row_factory = sqlite3.Row
    historical.executescript((Path(__file__).parent / "schema21.sql").read_text())
    historicalTriggers = [row[0] for row in historical.execute("SELECT sql FROM sqlite_master WHERE type='trigger'")]
    for row in historical.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        historical.execute(f'DROP TRIGGER "{row[0]}"')
    for row in historical.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'").fetchall():
        table = row[0]
        columns = [column["name"] for column in historical.execute(f"PRAGMA table_info({table})")]
        if table == "users":
            source = "SELECT u.id, a.role, u.username, u.usernameKey, u.passwordHash, u.authVersion FROM users u JOIN ledgerAccess a ON a.userId=u.id"
        else:
            source = "SELECT " + ", ".join(columns) + f" FROM {table}"
        records = connection.execute(source).fetchall()
        if records:
            historical.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", [tuple(record) for record in records])
    for statement in historicalTriggers:
        historical.execute(statement)
    historical.commit()
    for row in connection.execute("SELECT name FROM sqlite_temp_master WHERE type='view'").fetchall():
        connection.execute(f'DROP VIEW "{row[0]}"')
    historical.backup(connection)
    historical.close()


def snapshotWithoutLoginTime(connection):
    """Exclude only the recorded login timestamp, independent of schema column order."""
    statements = list(connection.iterdump())
    if any(row[1] == "lastLoginAt" for row in connection.execute("PRAGMA table_info(users)")):
        loginTimes = [row[0] for row in connection.execute("SELECT lastLoginAt FROM users WHERE lastLoginAt IS NOT NULL")]
        for index, statement in enumerate(statements):
            if statement.startswith('INSERT INTO "users"'):
                for timestamp in loginTimes:
                    statement = statement.replace("'" + timestamp.replace("'", "''") + "'", "NULL")
                statements[index] = statement
    return statements


def createPreProfileOwner(connection, username, password, **ignored):
    """Explicit historical identity fixture, before profile fields existed."""
    from users import passwordHash
    from database import bindInitialLedger
    bindInitialLedger(connection)
    with connection:
        userId = connection.execute("INSERT INTO users (username, usernameKey, passwordHash) VALUES (?, ?, ?)", (username, username.casefold(), passwordHash(password))).lastrowid
        connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, currentLedgerId(), 'owner')", (userId,))
    return userId


def signIn(client, username="Owner", password="owner password"):
    import re

    html = client.get("/sign-in").get_data(as_text=True)
    token = re.search(r'name="csrfToken" value="([^"]+)"', html).group(1)
    response = client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token})
    if response.status_code == 302:
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
    return response


class PwaFixture:
    """Shared synthetic setup for policy, offline, push, and CLI deletion checks."""

    def setUp(self):
        import tempfile
        from app import createApp
        import database as ledger
        from users import createUser

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.ledgerId = self.connection.ledgerId
        self.ownerId = createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        self.auditorId = createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.accountId = ledger.createAccount(self.connection, "Cash", "cash")
        self.categoryId = ledger.createCategory(self.connection, "Expenses")
        (Path(self.directory.name) / "platform.conf").write_text("pwaOfflineEnabled = true\npushNotificationsEnabled = true\n")
        self.application = createApp(self.path)
        self.client = self.application.test_client()
        self.login("Owner", "owner password")
        self.client.environ_base["HTTP_ORIGIN"] = "https://finance.example.org"

    def login(self, username, password):
        import re

        page = self.client.get("/sign-in").get_data(as_text=True)
        token = re.search('name="csrfToken" value="([^"]+)"', page).group(1)
        response = self.client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token})
        self.assertEqual(302, response.status_code)
        session = self.client.get("/api/session").get_json()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = session["csrfToken"]
        return session

    def submit(self, requestId):
        return self.client.post("/api/pending/manual", json={"submissionId": requestId, "date": "2026-10-27", "description": "Pending fixture", "amount": "3.00", "accountId": self.accountId, "categoryId": self.categoryId, "pinned": False})

    def pushSubscription(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        return {"endpoint": "https://web.push.apple.com/test-fixture", "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}}

    def enrollInvitationRecipient(self):
        from users import createOwner

        recipientId = createOwner(self.connection, "Recipient", "recipient password", firstName="Recipient")
        self.login("Recipient", "recipient password")
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        subscription["endpoint"] += "/recipient"
        self.assertEqual(200, self.client.post("/api/push", json=subscription).status_code)
        self.login("Owner", "owner password")
        return recipientId, subscription


class IdentityFixture:
    """Synthetic identity and platform-configuration setup."""

    def setUp(self):
        from app import createApp
        import database as ledger
        import tempfile
        import users

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "synthetic.db"
        self.app = createApp(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.connection.configuration = self.app.config["PLATFORM_CONFIGURATION"]
        self.connection.operationalLogs = self.app.config["OPERATIONAL_LOGS"]
        self.owner = users.createOwner(self.connection, "Owner", "owner password", "Jane", "Smith")
        self.client = self.app.test_client()

    def configure(self, content):
        from app import createApp

        (self.root / "platform.conf").write_text(content)
        self.app = createApp(self.path)
        self.connection.configuration = self.app.config["PLATFORM_CONFIGURATION"]
        self.connection.operationalLogs = self.app.config["OPERATIONAL_LOGS"]
        self.client = self.app.test_client()


class PendingFixture:
    """Synthetic Pending entries and shared submission/review helpers."""

    def setUp(self):
        from app import createApp
        from users import createUser
        import database as ledger
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "synthetic.db"
        self.backupsPath = Path(self.directory.name) / "backups"
        self.backupsPath.mkdir()
        ledger.initializeDatabase(self.path)
        self.connection = connectFixtureDatabase(self.path)
        createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.cash = ledger.createAccount(self.connection, "Example Cash", "cash", openingBalance="100.00")
        self.card = ledger.createAccount(self.connection, "Example Card", "credit", "Credit Card", openingBalance="20.00")
        self.expense = ledger.createCategory(self.connection, "Example Expense")
        self.income = ledger.createCategory(self.connection, "Example Income", reportingRole="income")
        self.external = self.connection.execute("SELECT id FROM accounts WHERE systemIdentity = 'external'").fetchone()[0]
        self.integration = ledger.createIntegrationSecret(self.connection, "Example Agent")
        secret = self.connection.execute("SELECT secret FROM integrationSecrets WHERE id = ?", (self.integration,)).fetchone()[0]
        self.headers = {"Authorization": "Bearer " + secret}
        self.app = createApp(self.path)
        self.owner = self.app.test_client()
        self.assertEqual(302, signIn(self.owner).status_code)
        self.agent = self.app.test_client()
        self.ordinary = {"submissionId": "ordinary", "date": "2028-02-29", "accountId": self.cash, "description": "Example purchase", "categoryId": self.expense, "amount": "10.00"}
        self.transfer = {"submissionId": "transfer", "type": "transfer", "date": "2028-02-29", "fromAccountId": self.cash, "toAccountId": self.card, "description": "Example payment", "amount": "50.00"}

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def submit(self, content, manual=False):
        import database as ledger

        return ledger.submitPendingEntry(self.connection, None if manual else self.integration, content)[0]

    def rows(self):
        import database as ledger

        return [{"id": row["id"], **{key: row[key] for key in ledger.pendingFields(row["kind"])}, "reviewedRole": row["currentRole"], "reviewVersion": row["reviewVersion"]} for row in ledger.pendingReviewRows(self.connection)]

    def snapshot(self):
        return tuple(snapshotWithoutLoginTime(self.connection))

    def integrationUpdateFields(self, row):
        import database as ledger

        fields = {key: row[key] for key in ledger.pendingFields(row["kind"])}
        fields["reviewVersion"] = row["reviewVersion"]
        if row["kind"] == "transaction":
            fields["reviewedRole"] = row["currentRole"]
        return fields

    def integrationPendingRow(self, proposalId):
        response = self.agent.get("/api/integration/pending", headers=self.headers)
        self.assertEqual(200, response.status_code)
        return next(row for row in response.get_json()["rows"] if row["id"] == proposalId)

    def updateThroughIntegration(self, proposalId, fields):
        return self.agent.put(f"/api/integration/submissions/{proposalId}", headers=self.headers, json=fields)


class LedgerNameFixture:
    """Synthetic ledger identity and authenticated browser helpers."""

    def setUp(self):
        from app import createApp
        from users import createOwner
        from users import createUser
        import database as ledger
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.owner = createOwner(self.connection, "Owner", "owner password", firstName="Owner")
        self.ledgerId = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.owner,)).fetchone()[0]
        ledger.bindLedger(self.connection, self.ledgerId)
        self.auditor = createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.application = createApp(self.path)

    def login(self, username="Owner", password="owner password"):
        import re

        client = self.application.test_client()
        token = re.search('name="csrfToken" value="([^"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        self.assertEqual(302, client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token}).status_code)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client


class SharingFixture(LedgerNameFixture):
    """Synthetic two-ledger sharing setup and invitation helpers."""

    def setUp(self):
        from users import createOwner
        import database as ledger

        LedgerNameFixture.setUp(self)
        self.otherOwner = createOwner(self.connection, "Other", "other password", firstName="Other")
        self.otherLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (self.otherOwner,)).fetchone()[0]
        ledger.bindLedger(self.connection, self.ledgerId, "user", self.owner, "Owner")

    def invite(self, username="Other", role="member", client=None):
        client = client or self.login()
        client.environ_base["HTTP_X_LEDGER_ID"] = self.ledgerId
        response = client.post("/api/invitations", json={"username": username, "role": role})
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()["id"]

    def accept(self, invitationId):
        client = self.login("Other", "other password")
        self.assertEqual(200, client.post(f"/api/invitations/{invitationId}/decision", json={"decision": "accept"}).status_code)
        client.environ_base["HTTP_X_LEDGER_ID"] = self.ledgerId
        return client


class SessionFixture:
    """Shared setup and session scenarios for both login-persistence settings."""

    def setUp(self):
        from app import createApp
        from users import createUser
        import database as ledger
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.ledgerId = self.connection.ledgerId
        self.ownerId = createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        self.auditorId = createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        self.application = createApp(self.path)

    def login(self, username="Owner", password="owner password", agent="", application=None):
        import re

        client = (application or self.application).test_client()
        if agent:
            client.environ_base["HTTP_USER_AGENT"] = agent
        csrf = re.search('name="csrfToken" value="([^"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        self.assertEqual(302, client.post("/sign-in", data={"username": username, "password": password, "csrfToken": csrf}).status_code)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client

    def sessions(self, client):
        response = client.get("/api/account/sessions")
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        return response.get_json()

    def checkOwnSessionsPublicIdentifiersClientsAndIndividualRevocation(self):
        chrome = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        first, second, auditor = self.login(agent=chrome), self.login(), self.login("Auditor", "auditor password")
        rows = self.sessions(first)
        self.assertEqual(2, len(rows))
        self.assertTrue(rows[0]["current"], "The current session stays first even after newer logins")
        current = next(row for row in rows if row["current"])
        remote = next(row for row in rows if not row["current"])
        self.assertIn("Chrome", current["client"])
        self.assertIn("Windows", current["client"])
        self.assertEqual("Unknown client", remote["client"])
        self.assertNotIn(first.get_cookie("npfSession").value, str(rows))
        self.assertNotIn(second.get_cookie("npfSession").value, str(rows))
        self.assertEqual(1, len(self.sessions(auditor)))
        foreignId = self.sessions(auditor)[0]["id"]
        self.assertIn(first.post("/api/account/sessions/logout", json={"id": foreignId}).status_code, (400, 404))
        self.assertEqual(200, auditor.get("/api/session").status_code)
        response = first.post("/api/account/sessions/logout", json={"id": remote["id"]})
        self.assertEqual({"status": "ok", "loggedOut": False}, response.get_json())
        self.assertEqual(401, second.get("/api/session").status_code)
        self.assertEqual(1, len(self.sessions(first)))

    def checkBackgroundRequestsUpdateIpWithoutLastActivity(self):
        from unittest.mock import patch

        first, second = self.login(), self.login()
        baseline = self.sessions(first)
        current = next(row for row in baseline if row["current"])
        remote = next(row for row in baseline if not row["current"])
        later = current["lastActiveAt"] + 20
        with patch("app.time.time", return_value=later):
            self.assertEqual(200, first.get("/api/session", headers={"X-Background-Request": "true", "X-Forwarded-For": "203.0.113.250"}, environ_overrides={"REMOTE_ADDR": "192.0.2.44"}).status_code)
            rows = first.get("/api/account/sessions", headers={"X-Background-Request": "true"}, environ_overrides={"REMOTE_ADDR": "192.0.2.44"}).get_json()
        observed = next(row for row in rows if row["current"])
        self.assertEqual("192.0.2.44", observed["lastIp"])
        self.assertEqual(current["lastActiveAt"], observed["lastActiveAt"])
        self.assertEqual(remote["lastActiveAt"], next(row for row in rows if not row["current"])["lastActiveAt"])
        with patch("app.time.time", return_value=later + 10):
            active = next(row for row in self.sessions(first) if row["current"])
        self.assertEqual(later + 10, active["lastActiveAt"])
        self.assertEqual(current["signedInAt"], active["signedInAt"])
        self.assertEqual(200, second.get("/api/session").status_code)

    def checkAllRolesNoLedgerAndDisabledSelfService(self):
        from users import passwordHash

        with self.connection:
            memberId = self.connection.execute("INSERT INTO users(username,usernameKey,passwordHash,firstName) VALUES ('Member','member',?,'Member')", (passwordHash("member password"),)).lastrowid
            self.connection.execute("INSERT INTO ledgerAccess(userId,ledgerId,role) VALUES (?,?,'member')", (memberId, self.ledgerId))
            self.connection.execute("INSERT INTO users(username,usernameKey,passwordHash,firstName) VALUES ('Unattached','unattached',?,'Unattached')", (passwordHash("unattached password"),))
        self.application.config["PLATFORM_CONFIGURATION"]["allowAccountSelfService"] = False
        for username, password in (("Owner", "owner password"), ("Auditor", "auditor password"), ("Member", "member password"), ("Unattached", "unattached password")):
            with self.subTest(username=username):
                client = self.login(username, password)
                self.assertEqual(1, len(self.sessions(client)))
                self.assertEqual(200, client.post("/api/account/sessions/logout", json={"others": True}).status_code)
                currentId = self.sessions(client)[0]["id"]
                self.assertEqual(200, client.post("/api/account/sessions/logout", json={"id": currentId}).status_code)

    def checkAuthorizationCsrfAndInvalidPayloads(self):
        client = self.login()
        self.assertEqual(401, self.application.test_client().get("/api/account/sessions").status_code)
        self.assertEqual(403, client.get("/api/account/sessions", headers={"Authorization": "Bearer wrong-auth"}).status_code)
        for submitted in ({}, {"others": False}, {"others": 1}, {"others": "true"}, {"others": True, "id": "anything"}, {"id": "missing"}, None):
            with self.subTest(submitted=submitted):
                self.assertIn(client.post("/api/account/sessions/logout", json=submitted).status_code, (400, 404))
        client.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, client.post("/api/account/sessions/logout", json={"others": True}).status_code)


class RecoveryFixture:
    """Synthetic password-recovery setup and captured-email helpers."""

    def setUp(self):
        from database import connectInitialLedgerDatabase
        from app import createApp
        from users import createUser
        import tempfile
        from users import trustRecoveryEmail

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
        import re

        page = client.get(route).get_data(as_text=True)
        return re.search(r'name="csrfToken" value="([^"]+)"', page).group(1)

    def login(self):
        client = self.application.test_client()
        csrfToken = self.csrf(client, "/sign-in")
        self.assertEqual(302, client.post("/sign-in", data={"username": "owner@example.test", "password": "original passphrase", "csrfToken": csrfToken}).status_code)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client

    def issue(self):
        from unittest.mock import patch

        csrfToken = self.csrf(self.client, "/forgot-password")
        with patch("users.sendRecoveryEmail") as send:
            response = self.client.post("/forgot-password", data={"username": "owner@example.test", "csrfToken": csrfToken}, headers={"X-Forwarded-Host": "untrusted.example.test"})
        self.assertEqual(200, response.status_code)
        link = send.call_args.args[2]
        self.assertTrue(link.startswith("https://finance.example.test/reset-password#"))
        return link.split("#", 1)[1], response
