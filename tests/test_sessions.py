"""Session management acceptance with guarded synthetic identities only."""
import tests

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as ledger
from app import createApp
from users import createUser, passwordHash


class SessionManagementTests(unittest.TestCase):
    def setUp(self):
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

    def testOwnSessionsPublicIdentifiersClientsAndIndividualRevocation(self):
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

    def testLogoutOthersAndCurrentSession(self):
        first, second, third = self.login(), self.login(), self.login()
        self.assertEqual({"status": "ok", "loggedOut": False}, first.post("/api/account/sessions/logout", json={"others": True}).get_json())
        self.assertEqual(401, second.get("/api/session").status_code)
        self.assertEqual(401, third.get("/api/session").status_code)
        currentId = self.sessions(first)[0]["id"]
        self.assertEqual({"status": "ok", "loggedOut": True}, first.post("/api/account/sessions/logout", json={"id": currentId}).get_json())
        self.assertIsNone(first.get_cookie("npfSession"))
        self.assertEqual(401, first.get("/api/session").status_code)

    def testBackgroundRequestsUpdateIpWithoutLastActivity(self):
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

    def testTrustedProxyIpUsesValidatedMiddleware(self):
        self.application.config["PLATFORM_CONFIGURATION"]["trustedProxyAddresses"] = frozenset({"192.0.2.1"})
        (Path(self.directory.name) / "platform.conf").write_text("trustedProxyAddresses = 192.0.2.1\n")
        proxyApplication = createApp(self.path)
        client = self.login(application=proxyApplication)
        response = client.get("/api/account/sessions", headers={"X-Forwarded-For": "198.51.100.77", "X-Forwarded-Proto": "https"}, environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
        self.assertEqual("198.51.100.77", response.get_json()[0]["lastIp"])

    def testFixedExpiryAndServerRestart(self):
        client = self.login()
        session = client.get("/api/session").get_json()
        current = self.sessions(client)[0]
        self.assertAlmostEqual(168 * 3600, session["expiresAt"] - current["signedInAt"], delta=1)
        with patch("app.time.time", return_value=session["expiresAt"] - 1):
            self.assertEqual(session["expiresAt"], client.get("/api/session").get_json()["expiresAt"])
        with patch("app.time.time", return_value=session["expiresAt"]):
            self.assertEqual(401, client.get("/api/account/sessions").status_code)
        live = self.login()
        restarted = createApp(self.path).test_client()
        restarted.set_cookie("npfSession", live.get_cookie("npfSession").value)
        self.assertEqual(401, restarted.get("/api/account/sessions").status_code)
        self.assertEqual(200, live.get("/api/session").status_code)

    def testAllRolesNoLedgerAndDisabledSelfService(self):
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

    def testAuthorizationCsrfAndInvalidPayloads(self):
        client = self.login()
        self.assertEqual(401, self.application.test_client().get("/api/account/sessions").status_code)
        self.assertEqual(403, client.get("/api/account/sessions", headers={"Authorization": "Bearer wrong-auth"}).status_code)
        for submitted in ({}, {"others": False}, {"others": 1}, {"others": "true"}, {"others": True, "id": "anything"}, {"id": "missing"}, None):
            with self.subTest(submitted=submitted):
                self.assertIn(client.post("/api/account/sessions/logout", json=submitted).status_code, (400, 404))
        client.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, client.post("/api/account/sessions/logout", json={"others": True}).status_code)

    def testInvalidatedVersionsAndDisabledUsersAreExcluded(self):
        first, second = self.login(), self.login()
        with self.connection:
            self.connection.execute("UPDATE users SET authVersion = authVersion + 1 WHERE id = ?", (self.ownerId,))
        self.assertEqual(401, first.get("/api/account/sessions").status_code)
        fresh = self.login()
        self.assertEqual(1, len(self.sessions(fresh)))
        self.assertEqual(401, second.get("/api/session").status_code)
        with self.connection:
            self.connection.execute("UPDATE users SET enabled = 0 WHERE id = ?", (self.ownerId,))
        self.assertEqual(401, fresh.get("/api/account/sessions").status_code)

    def testIdentityUpdatesPreserveCurrentSessionMetadata(self):
        client = self.login()
        before = self.sessions(client)[0]
        self.assertEqual(200, client.put("/api/account/name", json={"firstName": "Changed", "lastName": ""}).status_code)
        afterName = self.sessions(client)[0]
        self.assertEqual(200, client.post("/api/account/password", json={"currentPassword": "owner password", "newPassword": "new private passphrase", "confirmation": "new private passphrase"}).status_code)
        afterPassword = self.sessions(client)[0]
        for after in (afterName, afterPassword):
            for field in ("id", "client", "signedInAt", "current"):
                self.assertEqual(before[field], after[field])
