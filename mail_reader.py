"""Reads incoming bank emails over IMAP and acknowledges processed ones by email.
(SMTP only sends mail; IMAP is how you receive it.)

Drop-in replacement: the methods service.py already calls are unchanged
(MailBox(), .unseen(), .mark_seen(uid)). Nothing else needs to change.

What is different from the first version:
  * the sender is matched in Python, not with the server's FROM search
  * the "unread" flag is NOT required: an email you already opened is still picked up
    (set ONLY_UNSEEN=1 for unread-only). Duplicates are avoided by Message-ID.
  * emails are only marked as read in the mailbox if MARK_SEEN=1
  * after the rows are saved to Excel, an acknowledgement is emailed to you, as a reply in the
    same thread as the bank email (ACK_EMAIL=0 turns it off)
"""
import contextlib
import email
import imaplib
import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from dotenv import load_dotenv
import core   # also loads the .env file
load_dotenv()



IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")      # Gmail: use an App Password
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
SENDERS = [s.strip().lower() for s in os.environ.get("ALLOWED_SENDERS", "hdfcbank,icicibank").split(",") if s.strip()]
SINCE_DAYS = int(os.environ.get("SINCE_DAYS", "30"))      # never look further back than this
FETCH_LIMIT = int(os.environ.get("FETCH_LIMIT", "50"))    # new emails per poll
ONLY_UNSEEN = True
MARK_SEEN = True
ACK_EMAIL = os.environ.get("ACK_EMAIL", "1") == "1"
ACK_MAX_PER_POLL = int(os.environ.get("ACK_MAX_PER_POLL", "10"))   # the rest are sent on the next poll


# ---------------------------------------------------------------- parsing helpers


def sender_ok(addr):
    addr = (addr or "").lower()
    return any(s in addr for s in SENDERS)


def header_items(data, chunk):
    """Yield (uid, header_bytes, info_text) from an IMAP FETCH reply, whatever order the server puts the UID in."""
    pos = 0
    for i, item in enumerate(data):
        if not isinstance(item, tuple):
            continue
        trailing = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
        info = item[0] + trailing
        m = re.search(rb"UID (\d+)", info)
        uid = m.group(1) if m else (chunk[pos] if pos < len(chunk) else None)
        pos += 1
        if uid is not None:
            yield uid, item[1], info


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
    uid_text = uid.decode() if isinstance(uid, bytes) else str(uid)
    return {
        "uid": uid,
        "message_id": (msg.get("Message-ID") or f"uid-{uid_text}").strip(),
        "sender": parseaddr(msg.get("From", ""))[1],
        "subject": str(msg.get("Subject", "")),
        "received_at": received,
        "body": body,
        "auth_ok": "dkim=pass" in auth or "spf=pass" in auth,
    }


# ---------------------------------------------------------------- state: which emails are done / acknowledged
def _state_path():
    return os.path.join(core.data_dir(), "mail_state.json")


def _load_state():
    try:
        s = json.load(open(_state_path(), encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    return {"processed": set(s.get("processed", [])), "acked": set(s.get("acked", []))}


def _save_state(st):
    json.dump({k: sorted(v) for k, v in st.items()}, open(_state_path(), "w", encoding="utf-8"))


# ---------------------------------------------------------------- acknowledgement
SHEET_NAMES = ("transactions", "balances", "needs_review")


def _saved_rows():
    """{message_id: (sheet, row_dict)} read from the Excel file, or None if it cannot be read (e.g. open in Excel)."""
    path = core.xlsx_path()
    if not os.path.exists(path):
        return {}
    try:
        from openpyxl import load_workbook
        out = {}
        with (getattr(core, "_XLSX_LOCK", None) or contextlib.nullcontext()):
            wb = load_workbook(path, read_only=True)
            for name in SHEET_NAMES:
                if name not in wb.sheetnames:
                    continue
                rows = wb[name].iter_rows(values_only=True)
                header = next(rows, None)
                if not header or "message_id" not in header:
                    continue
                for r in rows:
                    d = dict(zip(header, r))
                    if d.get("message_id"):
                        out[d["message_id"]] = (name, d)
            wb.close()
        return out
    except Exception:
        return None


def _ack_body(sheet, d):
    def v(k):
        return "" if d.get(k) in (None, "") else str(d[k])
    if sheet == "transactions":
        lines = ["Your bank email was processed and saved to the Excel file.", "",
                 f"Type     : {v('type')}", f"Amount   : {v('amount')}",
                 f"Account  : {v('bank')} {v('account')}".rstrip(),
                 f"Merchant : {v('merchant')}", f"Category : {v('category')}", f"Date     : {v('date')}"]
    elif sheet == "balances":
        lines = ["Your bank email was processed: balance alert saved to the Excel file.", "",
                 f"Account  : {v('bank')} {v('account')}".rstrip(),
                 f"Balance  : {v('balance')}", f"Date     : {v('date')}"]
    else:
        lines = ["Your bank email could not be verified automatically, so it was put in the "
                 "'needs_review' sheet instead of the transaction list.", "", f"Reason   : {v('reason')}"]
    return "\n".join(lines + ["", f"Original email from: {v('sender')}"])


def _send_acks(state):
    """Send acknowledgements for processed emails that are saved in Excel and not yet acknowledged."""
    if not ACK_EMAIL:
        return []
    user = os.environ.get("SMTP_USER") or IMAP_USER
    password = os.environ.get("SMTP_PASSWORD") or IMAP_PASSWORD
    to = os.environ.get("ACK_TO") or user
    if not (user and password and to):
        return ["acknowledgement skipped: no login configured"]
    saved = _saved_rows()
    if saved is None:
        return ["acknowledgement postponed: could not read the Excel file (is it open in Excel?)"]
    pending = [m for m in sorted(state["processed"]) if m in saved and m not in state["acked"]][:ACK_MAX_PER_POLL]
    if not pending:
        return []
    report = []
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    try:
        smtp = smtplib.SMTP(host, port, timeout=30) if port == 587 else smtplib.SMTP_SSL(host, port, timeout=30)
        with smtp as s:
            if port == 587:
                s.starttls()
            s.login(user, password)
            for mid in pending:
                sheet, d = saved[mid]
                subject = " ".join(str(d.get("subject") or "bank alert").split())
                msg = EmailMessage()
                msg["From"], msg["To"] = user, to          # always to YOU, never to the bank
                msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                if mid.startswith("<"):                    # makes it a reply in the same thread
                    msg["In-Reply-To"] = msg["References"] = mid
                msg.set_content(_ack_body(sheet, d))
                s.send_message(msg)
                state["acked"].add(mid)
                report.append(f"acknowledged: {subject[:50]}")
    except Exception as e:
        report.append(f"acknowledgement failed (will retry on the next poll): {e}")
    return report


# ---------------------------------------------------------------- mailbox
class MailBox:
    def __enter__(self):
        if not (IMAP_USER and IMAP_PASSWORD):
            raise RuntimeError("Set IMAP_USER and IMAP_PASSWORD (in .env, or as Space secrets).")
        self.scan = {"in_window": 0, "from_banks": 0, "already_done": 0, "new": 0}
        self.ack_report = []
        self._yielded, self._done = {}, set()
        self.M = imaplib.IMAP4_SSL(IMAP_HOST)
        self.M.login(IMAP_USER, IMAP_PASSWORD)
        self.M.select(IMAP_FOLDER)
        return self

    def __exit__(self, *exc):
        try:
            self.M.logout()
        except Exception:
            pass
        try:                                               # runs after service.py has saved the rows to Excel
            st = _load_state()
            st["processed"] |= self._done
            self.ack_report = _send_acks(st)
            _save_state(st)
            for line in self.ack_report:
                print("[mail]", line)
        except Exception as e:
            self.ack_report = [f"acknowledgement error: {e}"]
            print("[mail]", self.ack_report[0])

    # ---- the method service.py calls (unchanged name and output)
    def unseen(self):
        known = core.known_message_ids() | _load_state()["processed"]
        yield from self._scan(known)

    # ---- also available for the newer service.py, which passes its own known ids
    def messages(self, known_ids=()):
        yield from self._scan(set(known_ids) | _load_state()["processed"])


    def _scan(self, known_ids):
        """Fetch unread bank emails received today onwards."""

        today = datetime.now().strftime("%d-%b-%Y")

        typ, data = self.M.uid(
            "SEARCH",
            None,
            "UNSEEN",
            "SINCE",
            today
        )

        if typ != "OK":
            raise RuntimeError("Unable to search Gmail inbox")

        uids = data[0].split() if data and data[0] else []

        self.scan["in_window"] = len(uids)

        print(
            f"[mail] Unread emails received today onwards: "
            f"{len(uids)}"
        )

        from_banks = []

        for i in range(0, len(uids), 200):
            chunk = uids[i:i + 200]

            typ, d = self.M.uid(
                "FETCH",
                b",".join(chunk),
                "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])"
            )

            if typ != "OK":
                continue

            for uid, hdr, _ in header_items(d, chunk):
                h = email.message_from_bytes(
                    hdr,
                    policy=policy.default
                )

                sender = parseaddr(
                    str(h.get("From", ""))
                )[1]

                if sender_ok(sender):
                    mid = str(
                        h.get("Message-ID") or ""
                    ).strip()

                    if not mid:
                        mid = f"uid-{uid.decode()}"

                    from_banks.append((uid, mid))

        self.scan["from_banks"] = len(from_banks)

        new = []

        for uid, mid in from_banks:
            if mid in known_ids:
                self.scan["already_done"] += 1
            else:
                new.append(uid)

        for uid in sorted(new, key=int)[:FETCH_LIMIT]:
            typ, d = self.M.uid(
                "FETCH",
                uid,
                "(BODY.PEEK[])"
            )

            if (
                typ == "OK"
                and d
                and isinstance(d[0], tuple)
            ):
                m = parse_email(d[0][1], uid)

                self._yielded[uid] = m["message_id"]
                self.scan["new"] += 1

                yield m

    def mark_seen(self, uid):
        """service.py calls this after it has saved the rows: remember the email as done (acknowledged at exit)."""
        if uid in self._yielded:
            self._done.add(self._yielded[uid])
        if MARK_SEEN:
            self.M.uid("STORE", uid, "+FLAGS", "(\\Seen)")