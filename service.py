
import os
import threading
import time
from datetime import datetime

import core
import database

REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "0") == "1"


def row_from_text(text, meta):
    result = core.process_text(text)

    if result["message_type"] == "other":
        return None

    row = {
        **result,
        **meta,
        "message": text[:300]
    }

    if row["message_type"] in ("transaction", "balance_alert"):
        row["date"] = (
            row.get("date")
            or (meta.get("received_at") or "")[:10]
            or None
        )

    return row


def process_email(m):
    snippet = core.clean_email_text(m["body"])

    if not snippet:
        return None

    meta = {
        "received_at": m["received_at"],
        "sender": m["sender"],
        "subject": m["subject"],
        "message_id": m["message_id"]
    }

    if REQUIRE_AUTH and not m["auth_ok"]:
        return {
            "message_type": "needs_review",
            "reason": "sender not authenticated (SPF/DKIM)",
            **meta,
            "message": snippet[:300]
        }

    return row_from_text(snippet, meta)


def poll_once():
    import mail_reader

    stats = {
        "fetched": 0,
        "saved": 0,
        "skipped": 0,
        "errors": []
    }

    known = database.known_message_ids()
    rows = []
    uids = []

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

            except Exception as e:
                stats["errors"].append(
                    f"{m.get('subject', '?')[:40]}: {e}"
                )

        # Save extracted rows to Excel first.
        counts = database.save_rows(rows)
        stats["saved"] = sum(counts.values())

        # Mark handled messages as read only after saving.
        for uid in uids:
            box.mark_seen(uid)

    return stats


class Poller:
    def __init__(self, interval_minutes=1):
        self.interval = interval_minutes * 60
        self.enabled = True

        self.state = {
            "last_run": None,
            "last_result": None,
            "error": None
        }

        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="GmailBackgroundPoller"
        )
        self._thread.start()

    def run_once(self):
        if not self._lock.acquire(blocking=False):
            return self.state["last_result"]

        try:
            result = poll_once()
            self.state.update(
                last_result=result,
                error=None
            )
            return result

        except Exception as e:
            self.state["error"] = str(e)
            print(f"[poller] Error: {e}")
            return None

        finally:
            self.state["last_run"] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            self._lock.release()

    def _loop(self):
        next_run = 0

        while not self._stop_event.is_set():
            if self.enabled and time.monotonic() >= next_run:
                self.run_once()
                next_run = time.monotonic() + self.interval

            self._stop_event.wait(2)

    def stop(self):
        self._stop_event.set()
