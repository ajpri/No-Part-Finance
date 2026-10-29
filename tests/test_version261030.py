"""Version Next uses synthetic databases; no production data is opened."""
import tests
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import database as ledger
from app import createApp
from users import createUser, createOwner


class NextMilestoneTests(unittest.TestCase):
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
        self.accountId = ledger.createAccount(self.connection, "Cash", "cash")
        self.categoryId = ledger.createCategory(self.connection, "Expenses")
        (Path(self.directory.name) / "platform.conf").write_text("pwaOfflineEnabled = true\npushNotificationsEnabled = true\n")
        self.application = createApp(self.path)
        self.client = self.application.test_client()
        self.login("Owner", "owner password")
        self.client.environ_base["HTTP_ORIGIN"] = "https://finance.example.org"

    def login(self, username, password):
        page = self.client.get("/sign-in").get_data(as_text=True)
        token = re.search('name="csrfToken" value="([^"]+)"', page).group(1)
        response = self.client.post("/sign-in", data={"username": username, "password": password, "csrfToken": token})
        self.assertEqual(302, response.status_code)
        session = self.client.get("/api/session").get_json()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = session["csrfToken"]
        return session

    def submit(self, requestId):
        return self.client.post("/api/pending/manual", json={"submissionId": requestId, "date": "2026-10-27", "description": "Pending fixture", "amount": "3.00", "accountId": self.accountId, "categoryId": self.categoryId, "pinned": False})

    def testPwaPolicyDefaultsFalseAndIndependentOverrides(self):
        from platformConfiguration import ConfigurationError, configurationSample, loadConfiguration
        root = Path(self.directory.name) / "default-installation"
        root.mkdir()
        defaults = loadConfiguration(root)
        self.assertFalse(defaults["pwaOfflineEnabled"])
        self.assertFalse(defaults["pushNotificationsEnabled"])
        sample = configurationSample()
        self.assertIn("pwaOfflineEnabled = false", sample)
        self.assertIn("pushNotificationsEnabled = false", sample)
        path = root / "platform.conf"
        for offline, push in [(False, False), (True, False), (False, True), (True, True)]:
            path.write_text(f"pwaOfflineEnabled = {str(offline).lower()}\npushNotificationsEnabled = {str(push).lower()}\n")
            saved = loadConfiguration(root)
            self.assertEqual(offline, saved["pwaOfflineEnabled"])
            self.assertEqual(push, saved["pushNotificationsEnabled"])
        for key in ("pwaOfflineEnabled", "pushNotificationsEnabled"):
            path.write_text(f"{key} = not-a-boolean\n")
            with self.assertRaises(ConfigurationError):
                loadConfiguration(root)
        path.write_text("pwaOfflineEnabled = true\npushNotificationsEnabled = true\n")
        running = createApp(root / "restart-test.db")
        path.write_text("pwaOfflineEnabled = false\npushNotificationsEnabled = false\n")
        self.assertTrue(running.test_client().get("/pwa-policy").get_json()["pwaOfflineEnabled"])
        restarted = createApp(root / "restart-test.db")
        self.assertEqual({"pwaOfflineEnabled": False, "pushNotificationsEnabled": False}, restarted.test_client().get("/pwa-policy").get_json())

    def testPwaPolicyPublicRoutesManifestAndWorkerCombinations(self):
        signedOut = self.application.test_client()
        configuration = self.application.config["PLATFORM_CONFIGURATION"]
        for offline, push in [(False, False), (True, False), (False, True), (True, True)]:
            with self.subTest(offline=offline, push=push):
                configuration.update(pwaOfflineEnabled=offline, pushNotificationsEnabled=push)
                response = signedOut.get("/pwa-policy")
                self.assertEqual(200, response.status_code)
                self.assertEqual({"pwaOfflineEnabled": offline, "pushNotificationsEnabled": push}, response.get_json())
                self.assertEqual("no-store", response.headers["Cache-Control"])
                with signedOut.get("/pwa.webmanifest") as manifest:
                    self.assertEqual(200, manifest.status_code)
                self.assertEqual(200 if offline else 404, signedOut.get("/offline-shell").status_code)
                worker = signedOut.get("/service-worker.js").get_data(as_text=True)
                self.assertIn(f'const offlineEnabled = "{str(offline).lower()}" === "true";', worker)
                self.assertIn(f'const pushEnabled = "{str(push).lower()}" === "true";', worker)
                self.assertNotIn("__PWA_OFFLINE_ENABLED__", worker)
                self.assertIn('/pwa.webmanifest', signedOut.get("/sign-in").get_data(as_text=True))

    def testPushMasterSwitchStopsEnrollmentAndBothDeliveryKinds(self):
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        self.client.post("/api/push", json=subscription)
        self.enrollInvitationRecipient()
        configuration = self.application.config["PLATFORM_CONFIGURATION"]
        configuration["pushNotificationsEnabled"] = False
        with patch("push.applicationKey") as key, patch("push.deliverPending") as pending, patch("push.deliverInvitation") as invitation:
            self.assertEqual({"enabled": False, "publicKey": None, "endpoints": []}, self.client.get("/api/push").get_json())
            self.assertEqual(403, self.client.post("/api/push", json=subscription).status_code)
            key.assert_not_called()
            self.assertEqual(201, self.submit("push-disabled").status_code)
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"})
            self.assertEqual(201, response.status_code)
            pending.assert_not_called()
            invitation.assert_not_called()
        self.assertEqual(200, self.client.delete("/api/push", json={"endpoint": subscription["endpoint"]}).status_code)
        self.assertEqual(1, self.client.get("/api/pending/status").get_json()["count"])

    def testDisabledWorkerClearsOnlyOwnedCachesAndDoesNotIntercept(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for disabled worker checks")
        source = Path("service-worker.js").read_text().replace("__PUSH_NOTIFICATIONS_ENABLED__", "false").replace("__PWA_OFFLINE_ENABLED__", "false")
        harness = r'''
const assert=require("node:assert/strict");
const names=new Set(["npf-shell-old","other-app-cache"]),handlers={};let pending;
const caches={async keys(){return [...names];},async delete(name){names.delete(name);},open(){throw new Error("Disabled worker must not populate cache");}};
const self={location:{origin:"https://finance.example.org"},addEventListener:(name,handler)=>handlers[name]=handler,async skipWaiting(){},clients:{async claim(){}},registration:{showNotification(){throw new Error("Push disabled");}}};
'''
        check = r'''
async function check(){
 handlers.install({waitUntil(promise){pending=promise;}});await pending;
 handlers.activate({waitUntil(promise){pending=promise;}});await pending;
 assert.deepEqual([...names],["other-app-cache"]);
 handlers.fetch({request:{url:"https://finance.example.org/",method:"GET",mode:"navigate"},respondWith(){throw new Error("Disabled cache intercepted navigation");}});
 handlers.push({waitUntil(){throw new Error("Disabled push processed notification");}});
}
check().catch(error=>{console.error(error);process.exit(1);});
'''
        result = subprocess.run([node, "-e", harness + source + check], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testPendingStatusIsSmallAuthorizedAndDetectsUnchangedCount(self):
        baseline = self.client.get("/api/pending/status").get_json()
        self.assertEqual([], baseline["ids"])
        response = self.submit("first")
        self.assertEqual(201, response.status_code, response.get_json())
        first = self.client.get("/api/pending/status").get_json()
        self.assertEqual(1, first["count"])
        self.assertNotIn("rows", first)
        with self.connection:
            self.connection.execute("UPDATE pendingSubmissions SET status = 'Rejected' WHERE id = ?", (first["ids"][0],))
        self.assertEqual(201, self.submit("second").status_code)
        second = self.client.get("/api/pending/status").get_json()
        self.assertEqual(first["count"], second["count"])
        self.assertGreater(second["latestId"], first["latestId"])
        self.login("Auditor", "auditor password")
        self.assertEqual(403, self.client.get("/api/pending/status").status_code)
        self.assertRegex(self.client.get("/api/calendar").get_json()["currentDate"], r"^\d{4}-\d{2}-\d{2}$")

    def testDashboardHasStableActionsAndServerCalendar(self):
        response = self.client.get("/api/dashboard").get_json()
        self.assertIn("currentDate", response)
        shell = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('id="pendingBanner"', shell)
        self.assertIn('id="dashboardReviewPendingButton"', shell)
        self.assertIn('id="dashboardRefreshRetry"', shell)

    def testDashboardLateResponseAndFailureRetainValues(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY to run asynchronous JavaScript checks")
        source = Path("app.js").read_text()
        functions = source[source.index("function dashboardQuery()"):source.index("function renderDashboard(dashboard)")]
        harness = r'''
const assert = require("node:assert/strict");
let dashboardRequestId=0, dashboardCurrentDate=null, lastDashboard=null, lastDashboardQuery=null;
let browserSession={ledgerId:"one"};
const controls={"#dashboardPeriod":{value:"thisMonth"},"#dashboardStartDate":{value:""},"#dashboardEndDate":{value:""},"#dashboardRefreshMessage":{},"#dashboardRefreshRetry":{}};
const document={querySelector:(key)=>controls[key]};
const requests=[]; function api(){return new Promise((resolve,reject)=>requests.push({resolve,reject}));}
async function cacheDashboard(){}
let displayed={value:100}; function renderDashboard(result){displayed=result;}
async function check(){
 const old=loadDashboard();
 assert.equal(displayed.value,100);
 controls["#dashboardPeriod"].value="lastMonth";
 const fresh=loadDashboard(); requests[1].resolve({value:200,currentDate:"2026-11-01"});await fresh;
 requests[0].resolve({value:999,currentDate:"2026-10-31"});await old;
 assert.equal(displayed.value,200);assert.equal(dashboardCurrentDate,"2026-11-01");
 const failure=loadDashboard(); requests[2].reject(new Error("network"));await failure;
 assert.equal(displayed.value,200);assert.equal(controls["#dashboardRefreshRetry"].hidden,false);
}
'''
        result = subprocess.run([node, "-e", harness + functions + "check().catch(error=>{console.error(error);process.exit(1)});"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def pushSubscription(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        return {"endpoint": "https://web.push.apple.com/test-fixture", "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}}

    def testPushPermissionEnrollmentRetryAndFailureIsolation(self):
        self.assertEqual(87, len(self.client.get("/api/push").get_json()["publicKey"]))
        subscription = self.pushSubscription()
        self.assertEqual(200, self.client.post("/api/push", json=subscription).status_code)
        with patch("push.webpush", return_value=type("Response", (), {"status_code": 201})()) as sender:
            self.assertEqual(201, self.submit("push-once").status_code)
            self.assertEqual(200, self.submit("push-once").status_code)
            self.assertEqual(1, sender.call_count)
            payload = json.loads(sender.call_args.kwargs["data"])
            self.assertEqual(self.ledgerId, payload["ledgerId"])
            self.assertEqual({"ledgerId", "proposalId"}, set(payload))
        with patch("push.webpush", side_effect=RuntimeError("private provider error")):
            self.assertEqual(201, self.submit("push-failure").status_code)
        self.assertEqual(2, self.client.get("/api/pending/status").get_json()["count"])
        self.assertEqual(200, self.client.delete("/api/push", json={"endpoint": subscription["endpoint"]}).status_code)
        with patch("push.webpush") as sender:
            self.submit("disabled")
            sender.assert_not_called()

    def testPushUsesEnrollmentOriginForInternalHttpSubmission(self):
        import base64
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        self.assertEqual(200, self.client.post("/api/push", json=subscription).status_code)
        # The proposal can arrive over plain HTTP from an internal integration.
        self.client.environ_base.pop("HTTP_ORIGIN")
        response = Mock(status_code=201, text="", headers={})
        with patch("push.NoRedirectSession.post", return_value=response) as send:
            self.assertEqual(201, self.submit("internal-http").status_code)
        self.assertEqual(1, send.call_count)
        headers = send.call_args.kwargs["headers"]
        token = headers["Authorization"].split("t=", 1)[1].split(",", 1)[0]
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual("https://finance.example.org", claims["sub"])
        self.assertEqual("https://web.push.apple.com", claims["aud"])
        self.assertEqual("300", headers["ttl"])
        self.assertEqual("aes128gcm", headers["content-encoding"])

    def testExistingEnrollmentRepairsSenderWithoutReplayingHistory(self):
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        self.client.environ_base.pop("HTTP_ORIGIN")
        self.client.post("/api/push", json=subscription)
        with patch("push.webpush") as sender:
            self.assertEqual(201, self.submit("before-repair").status_code)
            sender.assert_not_called()
        before = self.connection.execute("SELECT lastProposalId FROM pushSubscriptions").fetchone()[0]
        self.client.environ_base["HTTP_ORIGIN"] = "https://finance.example.org"
        self.client.post("/api/push", json=subscription)
        saved = self.connection.execute("SELECT lastProposalId, subscription FROM pushSubscriptions").fetchone()
        self.assertEqual(before, saved[0])
        self.assertEqual("https://finance.example.org", json.loads(saved[1])["vapidSubject"])
        with patch("push.webpush", return_value=Mock(status_code=201)) as sender:
            self.assertEqual(200, self.submit("before-repair").status_code)
            sender.assert_not_called()
            self.assertEqual(201, self.submit("after-repair").status_code)
            self.assertEqual(1, sender.call_count)

    def testPushLogsProviderRejectionWithoutPrivateResponse(self):
        from pywebpush import WebPushException
        self.client.get("/api/push")
        self.client.post("/api/push", json=self.pushSubscription())
        response = Mock(status_code=403)
        response.json.return_value = {"reason": "BadJwtToken", "private": "secret endpoint"}
        with patch("push.webpush", side_effect=WebPushException("secret endpoint", response=response)):
            self.assertEqual(201, self.submit("rejected-token").status_code)
        text = (Path(self.directory.name) / "logs" / "startup.log").read_text()
        self.assertIn("HTTP 403", text)
        self.assertIn("BadJwtToken", text)
        self.assertNotIn("secret endpoint", text)

    def testPushWorkerShowsNotificationBeforeUpdatingClients(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for push worker checks")
        source = Path("service-worker.js").read_text().replace("__PUSH_NOTIFICATIONS_ENABLED__", "true").replace("__PWA_OFFLINE_ENABLED__", "true")
        harness = r'''
const assert=require("node:assert/strict");
const handlers={},calls=[];let pending;
const self={addEventListener:(name,handler)=>handlers[name]=handler,
 registration:{async showNotification(title,options){calls.push("notification");assert.equal(title,"Pending Review");assert.equal(options.body,"A new pending item is ready to review. Tap to open.");}},
 clients:{async matchAll(){calls.push("clients");throw new Error("A client disappeared");}}};
'''
        check = r'''
handlers.push({data:{json:()=>({ledgerId:"selected",proposalId:1})},waitUntil(promise){pending=promise;}});
pending.then(()=>assert.deepEqual(calls,["notification","clients"])).catch(error=>{console.error(error);process.exit(1);});
'''
        result = subprocess.run([node, "-e", harness + source + check], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def enrollInvitationRecipient(self):
        recipientId = createOwner(self.connection, "Recipient", "recipient password", firstName="Recipient")
        self.login("Recipient", "recipient password")
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        subscription["endpoint"] += "/recipient"
        self.assertEqual(200, self.client.post("/api/push", json=subscription).status_code)
        self.login("Owner", "owner password")
        return recipientId, subscription

    def testInvitationPushRecipientDeduplicationAndDurableClaim(self):
        recipientId, subscription = self.enrollInvitationRecipient()
        # The sender's device must not receive someone else's invitation.
        self.client.post("/api/push", json=self.pushSubscription())
        thirdOwner = createOwner(self.connection, "Third", "third password", firstName="Third")
        thirdLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (thirdOwner,)).fetchone()[0]
        with self.connection:
            self.connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, ?, 'member')", (recipientId, thirdLedger))
            self.connection.execute("""INSERT INTO pushSubscriptions (userId, ledgerId, endpoint, subscription, lastProposalId)
                SELECT userId, ?, endpoint, subscription, lastProposalId FROM pushSubscriptions WHERE userId = ? LIMIT 1""", (thirdLedger, recipientId))
        with patch("push.webpush", return_value=Mock(status_code=201)) as sender:
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"})
            self.assertEqual(201, response.status_code)
            invitationId = response.get_json()["id"]
            self.assertEqual(1, sender.call_count)
            self.assertEqual(subscription["endpoint"], sender.call_args.args[0]["endpoint"])
            self.assertEqual({"endpoint", "keys"}, set(sender.call_args.args[0]))
            self.assertEqual({"type": "ledger-invitation", "invitationId": invitationId}, json.loads(sender.call_args.kwargs["data"]))
            from push import deliverInvitation
            deliverInvitation(self.connection, invitationId, None)
            self.assertEqual(1, sender.call_count)
            self.assertEqual(400, self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"}).status_code)
            self.assertEqual(1, sender.call_count)
        self.login("Recipient", "recipient password")
        self.client.post("/api/push", json=subscription)
        with patch("push.webpush") as sender:
            deliverInvitation(self.connection, invitationId, None)
            sender.assert_not_called()
        self.assertEqual(1, len(self.client.get("/api/invitations").get_json()))

    def testInvitationPushFailureKeepsInviteAndGuardsStaleRecipients(self):
        import time
        from push import deliverInvitation
        recipientId, _ = self.enrollInvitationRecipient()
        with patch("push.webpush", side_effect=RuntimeError("private provider details")):
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "auditor"})
        self.assertEqual(201, response.status_code)
        invitationId = response.get_json()["id"]
        self.assertEqual("Pending", self.connection.execute("SELECT status FROM sharingInvitations WHERE id = ?", (invitationId,)).fetchone()[0])
        # Clear the test-only delivery claim to independently exercise eligibility guards.
        for condition in ["cancelled", "expired", "disabled", "revoked"]:
            with self.subTest(condition=condition):
                with self.connection:
                    self.connection.execute("UPDATE pushSubscriptions SET subscription = json_remove(subscription, '$.lastInvitationId') WHERE userId = ?", (recipientId,))
                    now = int(time.time())
                    self.connection.execute("UPDATE sharingInvitations SET status = 'Pending', createdAt = ?, expiresAt = ? WHERE id = ?", (now, now + 604800, invitationId))
                    if condition == "cancelled":
                        self.connection.execute("UPDATE sharingInvitations SET status = 'Cancelled' WHERE id = ?", (invitationId,))
                    elif condition == "expired":
                        self.connection.execute("UPDATE sharingInvitations SET createdAt = 0, expiresAt = 604800 WHERE id = ?", (invitationId,))
                    elif condition == "disabled":
                        self.connection.execute("UPDATE users SET enabled = 0 WHERE id = ?", (recipientId,))
                    else:
                        self.connection.execute("UPDATE users SET enabled = 1 WHERE id = ?", (recipientId,))
                        self.connection.execute("DELETE FROM pushSubscriptions WHERE userId = ?", (recipientId,))
                with patch("push.webpush") as sender:
                    deliverInvitation(self.connection, invitationId, None)
                    sender.assert_not_called()

    def testInvitationAuditRollbackSendsNoPush(self):
        self.enrollInvitationRecipient()
        with self.connection:
            self.connection.execute("CREATE TRIGGER failInvitationAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'sharingInvitations' BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
        with patch("push.webpush") as sender:
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"})
            self.assertEqual(500, response.status_code)
            sender.assert_not_called()
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM sharingInvitations").fetchone()[0])

    def testInvitationWorkerCopyAndReviewDestination(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for invitation notification checks")
        source = Path("service-worker.js").read_text().replace("__PUSH_NOTIFICATIONS_ENABLED__", "true").replace("__PWA_OFFLINE_ENABLED__", "true")
        harness = r'''
const assert=require("node:assert/strict");
const handlers={},notifications=[],messages=[],opened=[];let pending;
const self={location:{origin:"https://finance.example.org"},addEventListener:(name,handler)=>handlers[name]=handler,
 registration:{async showNotification(title,options){notifications.push({title,...options});}},
 clients:{async matchAll(){return [{visibilityState:"visible",url:"https://finance.example.org/?ledger=existing",postMessage(message){messages.push(message);}}];},async openWindow(url){opened.push(url);}}};
'''
        check = r'''
async function check(){
 handlers.push({data:{json:()=>({type:"ledger-invitation",invitationId:7})},waitUntil(promise){pending=promise;}});await pending;
 assert.equal(notifications[0].title,"Ledger Invitation");assert.equal(notifications[0].body,"You’ve been invited to join a ledger. Tap to review.");assert.equal(notifications[0].data.url,"/pending");assert.deepEqual(messages,[{type:"invitation-arrival"}]);
 handlers.notificationclick({notification:{close(){},data:notifications[0].data},waitUntil(promise){pending=promise;}});await pending;assert.deepEqual(opened,["https://finance.example.org/pending"]);
}
check().catch(error=>{console.error(error);process.exit(1);});
'''
        result = subprocess.run([node, "-e", harness + source + check], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testPushGuardsAndExpiredSubscriptionCleanup(self):
        subscription = self.pushSubscription()
        self.client.get("/api/push")
        for endpoint in ("http://web.push.apple.com/x", "https://127.0.0.1/x", "https://web.push.apple.com.attacker.example/x", "https://web.push.apple.com:5000/x"):
            self.assertEqual(400, self.client.post("/api/push", json={**subscription, "endpoint": endpoint}).status_code)
        self.client.post("/api/push", json=subscription)
        with patch("push.webpush", return_value=type("Response", (), {"status_code": 410})()):
            self.submit("expired-push")
        self.assertEqual([], self.client.get("/api/push").get_json()["endpoints"])
        self.client.post("/api/push", json=subscription)
        with self.connection:
            self.connection.execute("UPDATE users SET enabled = 0 WHERE id = ?", (self.ownerId,))
        from push import deliverPending
        with patch("push.webpush") as sender:
            deliverPending(self.connection, self.ledgerId, 100, "https://example.test")
            sender.assert_not_called()
        self.login("Auditor", "auditor password")
        self.assertEqual(403, self.client.get("/api/push").status_code)
        self.assertEqual(403, self.client.post("/api/push", json=subscription).status_code)

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

    def testOfflineSnapshotIsolationSelectionsAndNoCredentials(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for offline storage behavior checks")
        source = Path("offline.js").read_text()
        harness = r'''
const assert=require("node:assert/strict");
const memory=new Map();
const window={isSecureContext:true};const navigator={serviceWorker:{}};
const localStorage={getItem(){return null;},setItem(){}};
let serverPolicy={pwaOfflineEnabled:true,pushNotificationsEnabled:true},connected=true;
const fetch=async()=>{if(!connected)throw new TypeError("offline");return {ok:true,json:async()=>serverPolicy};};
const cacheNames=new Set(["npf-shell-old","other-cache"]);
const caches={async keys(){return [...cacheNames];},async delete(name){cacheNames.delete(name);}};window.caches=caches;
const connection={createObjectStore(){},transaction(){const tx={};tx.objectStore=()=>({
 put(value){memory.set(value.key,structuredClone(value));return{};},
 get(key){return{result:structuredClone(memory.get(key))};},
 getAll(){return{result:structuredClone([...memory.values()])};},
 delete(key){memory.delete(key);return{};},clear(){memory.clear();return{};}
});queueMicrotask(()=>tx.oncomplete());return tx;}};
const indexedDB={open(){const request={result:connection};queueMicrotask(()=>request.onsuccess());return request;}};
async function check(){
 const session={userId:1,ledgerId:"A",role:"owner",ledgers:[{id:"A",label:"One"},{id:"B",label:"Two"}],csrfToken:"SECRET",username:"PRIVATE"};
 const controls={dashboardPeriod:{value:"custom"},dashboardStartDate:{value:"2026-01-01"}};
 await OfflineFinance.save(session,{expensesCents:100},controls);
 await OfflineFinance.save({...session,ledgerId:"B"},{expensesCents:200},controls);
 await OfflineFinance.save({...session,userId:2,ledgerId:"C"},{expensesCents:300},controls);
 assert.equal((await OfflineFinance.available(1)).snapshots.length,2);
 assert.equal((await OfflineFinance.available()).snapshots[0].ledgerId,"C");
 const serialized=JSON.stringify([...memory.values()]);assert(!serialized.includes("SECRET"));assert(!serialized.includes("PRIVATE"));
 assert.equal((await OfflineFinance.available(1)).snapshots[0].controls.dashboardPeriod.value,"custom");
 await OfflineFinance.clear(1,"A");assert.deepEqual((await OfflineFinance.available(1)).snapshots.map(s=>s.ledgerId),["B"]);
 await OfflineFinance.clear(1);assert.equal((await OfflineFinance.available(1)).snapshots.length,0);
 assert.equal((await OfflineFinance.available(2)).snapshots.length,1);
 connected=false;await OfflineFinance.refreshPolicy();assert.equal((await OfflineFinance.available(2)).snapshots.length,1);
 connected=true;serverPolicy={pwaOfflineEnabled:false,pushNotificationsEnabled:true};await OfflineFinance.refreshPolicy();
 assert.equal(memory.size,0);assert.deepEqual([...cacheNames],["other-cache"]);assert.equal(OfflineFinance.policy.pushNotificationsEnabled,true);
 await OfflineFinance.save(session,{expensesCents:400},controls);assert.equal(memory.size,0);
 connected=false;await OfflineFinance.refreshPolicy();assert.equal((await OfflineFinance.available(2)).snapshots.length,0);
 connected=true;serverPolicy={pwaOfflineEnabled:true,pushNotificationsEnabled:false};await OfflineFinance.refreshPolicy();
 await OfflineFinance.save(session,{expensesCents:500},controls);assert.equal((await OfflineFinance.available(1)).snapshots.length,1);assert.equal(OfflineFinance.policy.pushNotificationsEnabled,false);
}
'''
        result = subprocess.run([node, "-e", harness + source + "\ncheck().catch(e=>{console.error(e);process.exit(1)});"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testDeletedUserCannotWriteFromPreviouslyAuthorizedConnection(self):
        from cliDeletion import deleteTarget
        stale = ledger.connectDatabase(self.path)
        self.addCleanup(stale.close)
        ledger.bindLedger(stale, self.ledgerId, "user", self.auditorId, "Auditor")
        deleteTarget(self.path, "user", self.auditorId, str(self.auditorId), Path(self.directory.name) / "backups")
        with self.assertRaises(ledger.ValidationError):
            ledger.createAccount(stale, "Should not be created", "cash")

    def testPortableExportHasNoPushCredentials(self):
        self.client.get("/api/push")
        self.client.post("/api/push", json=self.pushSubscription())
        response = self.client.get("/api/backup")
        self.assertEqual(200, response.status_code)
        exported = Path(self.directory.name) / "export.db"
        exported.write_bytes(response.get_data())
        with sqlite3.connect(exported) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM pushSubscriptions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM pushConfiguration").fetchone()[0])
            self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())

    def testVisibleLifecyclePausesAndHandlesCalendarRollover(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for browser lifecycle checks")
        source = Path("app.js").read_text()
        functions = source[source.index("async function refreshDashboardOnReturn()"):source.index('document.addEventListener("visibilitychange"')]
        harness = r'''
const assert=require("node:assert/strict");
let lifecycleReady=true,offlineMode=false,visibleCheckTimer=null,dashboardRefreshPromise=null;
let currentPath="/",dashboardCurrentDate="2026-10-31",refreshes=0,counts=0,periods=0;
const OfflineFinance={policy:{pwaOfflineEnabled:true,pushNotificationsEnabled:true},async refreshPolicy(){return this.policy;}};
const controls={"#dashboardRefreshMessage":{},"#dashboardRefreshRetry":{}};
const document={visibilityState:"hidden",querySelector:(key)=>controls[key]};
async function api(){return {currentDate:"2026-11-01"};}
async function refreshPendingCount(){counts++;}
async function loadDashboard(){refreshes++;dashboardCurrentDate="2026-11-01";}
async function initializeDashboardPeriods(){periods++;}
async function recoverOffline(){}
async function check(){
 await checkVisiblePage(true);assert.equal(refreshes,0);assert.equal(counts,0);
 document.visibilityState="visible";await checkVisiblePage();assert.equal(refreshes,1);assert.equal(counts,1);
 await checkVisiblePage();assert.equal(refreshes,1);
 await Promise.all([refreshDashboardOnReturn(),refreshDashboardOnReturn()]);assert.equal(refreshes,2);
 currentPath="/transactions";await checkVisiblePage(true);assert.equal(refreshes,2);assert.equal(counts,3);
}
'''
        result = subprocess.run([node, "-e", harness + functions + "check().catch(e=>{console.error(e);process.exit(1)});"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testPublicOfflineShellAndWorkerContainNoFinancialState(self):
        signedOut = self.application.test_client()
        response = signedOut.get("/offline-shell")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("owner password", response.get_data(as_text=True))
        worker = signedOut.get("/service-worker.js")
        self.assertEqual(200, worker.status_code)
        self.assertIn("26.10.30", worker.get_data(as_text=True))
        self.assertNotIn("__APPLICATION_VERSION__", worker.get_data(as_text=True))
        self.assertEqual("no-cache", worker.headers["Cache-Control"])

    def testCompactOfflineTimestampKeepsOlderDates(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for timestamp presentation checks")
        source = Path("app.js").read_text()
        localDate = source[source.index("function localCalendarDate("):source.index("const today =")]
        formatter = source[source.index("function offlineUpdatedLabel("):source.index("function offlineStatus(")]
        harness = r'''
const assert=require("node:assert/strict");
const now=new Date(2026,9,27,16,0);
assert.equal(offlineUpdatedLabel(new Date(2026,9,27,14,35).toISOString(),now),"Updated 2:35 PM");
assert.match(offlineUpdatedLabel(new Date(2026,9,26,14,35).toISOString(),now),/^Updated Oct 26, 2:35 PM$/);
assert.match(offlineUpdatedLabel(new Date(2025,9,26,14,35).toISOString(),now),/2025/);
assert.equal(offlineUpdatedLabel("malformed",now),"Updated time unavailable");
'''
        result = subprocess.run([node, "-e", localDate + formatter + harness], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testOfflineDashboardLinkProtectsDraftsWithoutNetwork(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for offline navigation checks")
        source = Path("app.js").read_text()
        navigation = source[source.index("async function openCachedDashboard()"):source.index("async function showOfflineDashboard(")]
        harness = r'''
const assert=require("node:assert/strict");
let offlineSnapshot={ledgerId:"selected"},offlineColdStart=false,allowDiscard=false,shown=null,closed=0,url=null;
const simulatorDirtyRows=new Set(["draft"]);
const document={querySelectorAll:()=>[{close(){closed++;}}]};
const history={replaceState(_state,_title,path){url=path;}};
function hasLedgerDrafts(){return true;}
function confirm(){return allowDiscard;}
function ledgerUrl(path,id){return `${path}?ledger=${id}`;}
async function showOfflineDashboard(snapshot){shown=snapshot;}
async function api(){throw new Error("Offline navigation must not request the server");}
async function check(){
 await openCachedDashboard();assert.equal(shown,null);assert.equal(closed,0);assert.equal(simulatorDirtyRows.size,1);
 allowDiscard=true;await openCachedDashboard();assert.equal(shown,offlineSnapshot);assert.equal(url,"/?ledger=selected");assert.equal(closed,1);assert.equal(simulatorDirtyRows.size,0);assert.equal(offlineColdStart,true);
 shown=null;offlineSnapshot=null;await openCachedDashboard();assert.equal(shown,null);
}
'''
        result = subprocess.run([node, "-e", harness + navigation + "check().catch(e=>{console.error(e);process.exit(1)});"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
