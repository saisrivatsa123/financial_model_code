"""Glue: email -> model -> Excel. Includes a background poller for the Streamlit app."""
import os
import threading
import time
from datetime import datetime

import core

REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "0") == "1"   # 1 = unauthenticated senders go to 'needs_review'


def row_from_text(text, meta):
    """Process one piece of text. Returns a row dict, or None for OTPs/promotions."""
    result = core.process_text(text)
    if result["message_type"] == "other":
        return None
    row = {**result, **meta, "message": text[:300]}
    if row["message_type"] in ("transaction", "balance_alert"):
        row["date"] = row.get("date") or (meta.get("received_at") or "")[:10] or None
    return row


def process_email(m):
    snippet = core.clean_email_text(m["body"])
    if not snippet:
        return None
    meta = {"received_at": m["received_at"], "sender": m["sender"],
            "subject": m["subject"], "message_id": m["message_id"]}
    if REQUIRE_AUTH and not m["auth_ok"]:
        return {"message_type": "needs_review", "reason": "sender not authenticated (SPF/DKIM)",
                **meta, "message": snippet[:300]}
    return row_from_text(snippet, meta)


def poll_once():
    """Fetch unseen HDFC/ICICI emails, append rows to Excel, then mark those emails as read."""
    import mail_reader
    stats = {"fetched": 0, "saved": 0, "skipped": 0, "errors": []}
    known = core.known_message_ids()
    rows, uids = [], []
    with mail_reader.MailBox() as box:
        for m in box.unseen():
            stats["fetched"] += 1
            try:
                if m["message_id"] in known:
                    stats["skipped"] += 1
                    uids.append(m["uid"])
                    continue
                row = process_email(m)
                if row:
                    rows.append(row)
                    known.add(m["message_id"])
                else:
                    stats["skipped"] += 1
                uids.append(m["uid"])
            except Exception as e:                        # leave this mail unread, try again next poll
                stats["errors"].append(f"{m.get('subject', '?')[:40]}: {e}")
        counts = core.append_rows(rows)                   # save first ...
        stats["saved"] = sum(counts.values())
        for uid in uids:                                  # ... then mark as read
            box.mark_seen(uid)
    return stats


class Poller:
    """Runs poll_once() every `interval` seconds in a background thread."""

    def __init__(self, interval_minutes=5):
        self.interval = interval_minutes * 60
        self.enabled = False
        self.state = {"last_run": None, "last_result": None, "error": None}
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def run_once(self):
        if not self._lock.acquire(blocking=False):        # a poll is already running
            return self.state["last_result"]
        try:
            res = poll_once()
            self.state.update(last_result=res, error=None)
            return res
        except Exception as e:
            self.state["error"] = str(e)
            return None
        finally:
            self.state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._lock.release()

    def _loop(self):
        last = 0.0
        while True:
            time.sleep(5)
            if self.enabled and time.time() - last >= self.interval:
                last = time.time()
                self.run_once()
