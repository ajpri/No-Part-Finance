"""Small Web Push boundary: subscriptions are private, delivery follows commit."""
import base64
import json
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import webpush, WebPushException
from requests import Session

from database import ValidationError


def migratePush(connection):
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS pushConfiguration (id INTEGER PRIMARY KEY CHECK(id = 1), privateKey TEXT NOT NULL)")
        connection.execute("""CREATE TABLE IF NOT EXISTS pushSubscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, userId INTEGER NOT NULL REFERENCES users(id),
            ledgerId TEXT NOT NULL REFERENCES ledgers(id), endpoint TEXT NOT NULL,
            subscription TEXT NOT NULL, lastProposalId INTEGER NOT NULL,
            UNIQUE(endpoint, ledgerId))""")
        connection.execute("PRAGMA user_version = 29")


def applicationKey(connection):
    with connection:
        row = connection.execute("SELECT privateKey FROM pushConfiguration WHERE id = 1").fetchone()
        if not row:
            vapid = Vapid()
            vapid.generate_keys()
            privateKey = vapid.private_pem().decode()
            connection.execute("INSERT OR IGNORE INTO pushConfiguration VALUES (1, ?)", (privateKey,))
            row = connection.execute("SELECT privateKey FROM pushConfiguration WHERE id = 1").fetchone()
    private = serialization.load_pem_private_key(row[0].encode(), password=None)
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return row[0], base64.urlsafe_b64encode(public).decode().rstrip("=")


def validateSubscription(submitted):
    if not isinstance(submitted, dict) or set(submitted) != {"endpoint", "keys"}:
        raise ValidationError("Submit the browser push subscription")
    endpoint, keys = submitted["endpoint"], submitted["keys"]
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValidationError("Invalid push endpoint")
    try:
        address = urlsplit(endpoint)
        host = address.hostname or ""
        # Only browser vendors' push services may receive outbound requests.
        permitted = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"} or host.endswith(".push.apple.com") or host.endswith(".notify.windows.com")
        if address.scheme != "https" or not permitted or address.port not in {None, 443} or address.username or address.password or address.fragment:
            raise ValueError()
        if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
            raise ValueError()
        decoded = {}
        for key in ("p256dh", "auth"):
            if not isinstance(keys[key], str) or len(keys[key]) > 150:
                raise ValueError()
            decoded[key] = base64.b64decode(keys[key] + "=" * (-len(keys[key]) % 4), altchars=b"-_", validate=True)
        if len(decoded["auth"]) != 16:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded["p256dh"])
    except (ValueError, TypeError):
        raise ValidationError("Invalid or unsupported browser push subscription") from None
    return {"endpoint": endpoint, "keys": keys}


def publicPushSubject(origin):
    """Use the enrollment's browser origin, never an internal proposal request URL."""
    try:
        address = urlsplit(origin or "")
        host = address.hostname or ""
        if address.scheme != "https" or "." not in host or host.endswith((".invalid", ".localhost", ".local")) or address.username or address.password or address.path not in {"", "/"} or address.query or address.fragment:
            return None
        if any(character.isspace() for character in origin):
            return None
        address.port  # Reject malformed ports.
        return f"https://{address.netloc}"
    except (ValueError, TypeError):
        return None


def enroll(connection, userId, ledgerId, submitted, subject=None):
    subscription = validateSubscription(submitted)
    if publicPushSubject(subject):
        subscription["vapidSubject"] = publicPushSubject(subject)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM pushSubscriptions WHERE endpoint = ? AND userId != ?", (subscription["endpoint"], userId)).fetchone():
            raise ValidationError("This installation belongs to another login. Disable its subscription and enable notifications again.")
        existing = connection.execute("SELECT subscription FROM pushSubscriptions WHERE endpoint = ? AND userId = ?", (subscription["endpoint"], userId)).fetchall()
        subscription["lastInvitationId"] = max((json.loads(row[0]).get("lastInvitationId", 0) for row in existing), default=0)
        baseline = connection.execute("SELECT COALESCE(MAX(id), 0) FROM pendingSubmissions WHERE ledgerId = ?", (ledgerId,)).fetchone()[0]
        connection.execute("""INSERT INTO pushSubscriptions (userId, ledgerId, endpoint, subscription, lastProposalId) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint, ledgerId) DO UPDATE SET subscription = excluded.subscription""", (userId, ledgerId, subscription["endpoint"], json.dumps(subscription), baseline))


class NoRedirectSession(Session):
    def request(self, *arguments, **keywords):
        keywords["allow_redirects"] = False
        return super().request(*arguments, **keywords)


def deliverPending(connection, ledgerId, proposalId, subject):
    """Claim once after commit. Failures never retry or undo an accepted proposal."""
    privateRow = connection.execute("SELECT privateKey FROM pushConfiguration WHERE id = 1").fetchone()
    if not privateRow:
        return
    private = Vapid.from_pem(privateRow[0].encode())
    subscriptions = connection.execute("SELECT id FROM pushSubscriptions WHERE ledgerId = ? AND lastProposalId < ?", (ledgerId, proposalId)).fetchall()
    for subscriptionId in subscriptions:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            subscription = connection.execute("""SELECT p.* FROM pushSubscriptions p JOIN users u ON u.id = p.userId
                JOIN ledgerAccess a ON a.userId = p.userId AND a.ledgerId = p.ledgerId
                WHERE p.id = ? AND p.lastProposalId < ? AND u.enabled = 1 AND u.passwordSetupRequired = 0
                AND a.enabled = 1 AND a.role IN ('owner', 'member')
                AND EXISTS(SELECT 1 FROM pendingSubmissions s WHERE s.id = ? AND s.ledgerId = p.ledgerId AND s.status = 'Pending')""", (subscriptionId[0], proposalId, proposalId)).fetchone()
            if not subscription:
                continue
            connection.execute("UPDATE pushSubscriptions SET lastProposalId = ? WHERE id = ?", (proposalId, subscription["id"]))
        sendPush(connection, subscription, private, {"ledgerId": ledgerId, "proposalId": proposalId}, subject)


def deliverInvitation(connection, invitationId, subject):
    """Notify only the recipient's already enrolled installations after invite commit."""
    privateRow = connection.execute("SELECT privateKey FROM pushConfiguration WHERE id = 1").fetchone()
    if not privateRow:
        return
    private = Vapid.from_pem(privateRow[0].encode())
    # One physical subscription can be enrolled for several ledgers.
    endpoints = connection.execute("""SELECT DISTINCT p.endpoint FROM pushSubscriptions p
        JOIN sharingInvitations i ON i.recipientUserId = p.userId WHERE i.id = ?""", (invitationId,)).fetchall()
    for endpoint in endpoints:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            subscriptions = connection.execute("""SELECT p.* FROM pushSubscriptions p
                JOIN users u ON u.id = p.userId
                JOIN ledgerAccess a ON a.userId = p.userId AND a.ledgerId = p.ledgerId
                JOIN sharingInvitations i ON i.recipientUserId = p.userId
                WHERE i.id = ? AND i.status = 'Pending' AND i.expiresAt > ? AND p.endpoint = ?
                AND u.enabled = 1 AND u.passwordSetupRequired = 0
                AND a.enabled = 1 AND a.role IN ('owner', 'member') ORDER BY p.id""", (invitationId, int(time.time()), endpoint[0])).fetchall()
            if not subscriptions:
                continue
            if any(json.loads(row["subscription"]).get("lastInvitationId", 0) >= invitationId for row in subscriptions):
                continue
            for row in subscriptions:
                saved = json.loads(row["subscription"])
                saved["lastInvitationId"] = invitationId
                connection.execute("UPDATE pushSubscriptions SET subscription = ? WHERE id = ?", (json.dumps(saved), row["id"]))
            subscription = subscriptions[0]
        sendPush(connection, subscription, private, {"type": "ledger-invitation", "invitationId": invitationId}, subject)


def sendPush(connection, subscription, private, payload, subject):
    try:
        with NoRedirectSession() as session:
            browserSubscription = json.loads(subscription["subscription"])
            senderSubject = browserSubscription.get("vapidSubject") or publicPushSubject(subject)
            if not senderSubject:
                logPushFailure(connection, "Sender address unavailable. Open Settings System in the HTTPS app to refresh notification enrollment.")
                return
            response = webpush({key: browserSubscription[key] for key in ("endpoint", "keys")},
                data=json.dumps(payload),
                vapid_private_key=private, vapid_claims={"sub": senderSubject}, timeout=4, ttl=300, requests_session=session)
        if not 200 <= response.status_code < 300:
            logPushFailure(connection, f"Push service rejected delivery (HTTP {response.status_code}).")
        if response.status_code in {404, 410}:
            with connection:
                connection.execute("DELETE FROM pushSubscriptions WHERE id = ?", (subscription["id"],))
    except WebPushException as error:
        status = error.response.status_code if error.response is not None else None
        reason = ""
        if error.response is not None:
            try:
                providerReason = error.response.json().get("reason")
                if providerReason in {"BadJwtToken", "BadTtl", "BadUrgency", "BadWebPushTopic", "DeviceTokenNotForTopic", "ExpiredToken", "Unregistered"}:
                    reason = f" {providerReason}."
            except (ValueError, AttributeError):
                pass
        logPushFailure(connection, f"Push service rejected delivery (HTTP {status}).{reason}" if status else "Push delivery failed before a provider response.")
        if error.response is not None and error.response.status_code in {404, 410}:
            with connection:
                connection.execute("DELETE FROM pushSubscriptions WHERE id = ?", (subscription["id"],))
    except Exception:
        # Endpoint, encryption keys, and provider errors are never logged.
        logPushFailure(connection, "Check outbound internet access and notification configuration.")


def logPushFailure(connection, detail):
    logs = getattr(connection, "operationalLogs", None)
    if logs:
        logs.write("startup", f"Notification not delivered. {detail} The saved item is unaffected.")
