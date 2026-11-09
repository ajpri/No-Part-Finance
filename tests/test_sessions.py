"""Session management acceptance with guarded synthetic identities only."""
import tests
from tests.test_support import SessionFixture

import unittest
from pathlib import Path
from unittest.mock import patch

from app import createApp


class SessionManagementTests(SessionFixture, unittest.TestCase):
    testOwnSessionsPublicIdentifiersClientsAndIndividualRevocation = SessionFixture.checkOwnSessionsPublicIdentifiersClientsAndIndividualRevocation

    def testLogoutOthersAndCurrentSession(self):
        first, second, third = self.login(), self.login(), self.login()
        self.assertEqual({"status": "ok", "loggedOut": False}, first.post("/api/account/sessions/logout", json={"others": True}).get_json())
        self.assertEqual(401, second.get("/api/session").status_code)
        self.assertEqual(401, third.get("/api/session").status_code)
        currentId = self.sessions(first)[0]["id"]
        self.assertEqual({"status": "ok", "loggedOut": True}, first.post("/api/account/sessions/logout", json={"id": currentId}).get_json())
        self.assertIsNone(first.get_cookie("npfSession"))
        self.assertEqual(401, first.get("/api/session").status_code)

    testBackgroundRequestsUpdateIpWithoutLastActivity = SessionFixture.checkBackgroundRequestsUpdateIpWithoutLastActivity

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

    testAllRolesNoLedgerAndDisabledSelfService = SessionFixture.checkAllRolesNoLedgerAndDisabledSelfService

    testAuthorizationCsrfAndInvalidPayloads = SessionFixture.checkAuthorizationCsrfAndInvalidPayloads

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
