"""Reads incoming bank emails over IMAP. (SMTP only sends mail; IMAP is how you receive it.)"""
import email
import imaplib
import os
from datetime import datetime, timedelta
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

import core

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")      # Gmail: use an App Password
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
SENDERS = [s.strip().lower() for s in os.environ.get("ALLOWED_SENDERS", "hdfcbank,icicibank").split(",") if s.strip()]
SINCE_DAYS = int(os.environ.get("SINCE_DAYS", "30"))      # never look further back than this
FETCH_LIMIT = int(os.environ.get("FETCH_LIMIT", "50"))    # emails per poll


def parse_email(raw, uid):
    msg = email.message_from_bytes(raw, policy=policy.default)
    # Bank emails often carry a useless plain-text stub next to the real HTML body:
    # use the first part that actually contains transaction wording.
    candidates = []
    for kind in ("plain", "html"):
        part = msg.get_body(preferencelist=(kind,))
        if part is not None:
            text = part.get_content()
            candidates.append(core.html_to_text(text) if kind == "html" else text)
    body = next((c for c in candidates if core.clean_email_text(c)), candidates[0] if candidates else "")
    try:
        received = parsedate_to_datetime(msg["Date"]).strftime("%Y-%m-%d %H:%M")
    except Exception:
        received = datetime.now().strftime("%Y-%m-%d %H:%M")
    auth = (msg.get("Authentication-Results") or "").lower()
    return {
        "uid": uid,
        "message_id": (msg.get("Message-ID") or f"uid-{uid.decode() if isinstance(uid, bytes) else uid}").strip(),
        "sender": parseaddr(msg.get("From", ""))[1],
        "subject": str(msg.get("Subject", "")),
        "received_at": received,
        "body": body,
        "auth_ok": "dkim=pass" in auth or "spf=pass" in auth,
    }


class MailBox:
    def __enter__(self):
        if not (IMAP_USER and IMAP_PASSWORD):
            raise RuntimeError("Set IMAP_USER and IMAP_PASSWORD as Space secrets.")
        self.M = imaplib.IMAP4_SSL(IMAP_HOST)
        self.M.login(IMAP_USER, IMAP_PASSWORD)
        self.M.select(IMAP_FOLDER)
        return self

    def __exit__(self, *exc):
        try:
            self.M.logout()
        except Exception:
            pass

    def unseen(self):
        since = (datetime.now() - timedelta(days=SINCE_DAYS)).strftime("%d-%b-%Y")
        ids = set()
        for s in SENDERS:
            typ, data = self.M.uid("SEARCH", None, "UNSEEN", "SINCE", since, "FROM", f'"{s}"')
            if typ == "OK" and data and data[0]:
                ids.update(data[0].split())
        for uid in sorted(ids, key=int)[:FETCH_LIMIT]:
            typ, d = self.M.uid("FETCH", uid, "(BODY.PEEK[])")     # PEEK: does not mark as read
            if typ == "OK" and d and d[0]:
                yield parse_email(d[0][1], uid)

    def mark_seen(self, uid):
        self.M.uid("STORE", uid, "+FLAGS", "(\\Seen)")
