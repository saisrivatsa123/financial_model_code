"""Offline checks: no model, no mailbox. Run: python test_offline.py"""
import os, tempfile, email
from email.message import EmailMessage

os.environ["DATA_DIR"] = tempfile.mkdtemp()
import core, service, mail_reader

# --- 1. balance alert (rules, no model) ---
msg = ("Available Bal in HDFC Bank A/c XX8902 as on yesterday:16-NOV-26 is INR 45.13. "
       "Cheques are subject to clearing.For updated A/C Bal dial 18002703333")
r = core.process_text(msg)
print("balance:", r)
assert r == {"message_type": "balance_alert", "bank": "HDFC", "account": "8902", "balance": 45.13, "date": "2026-11-16"}

# --- 2. OTP / promo are ignored ---
assert core.process_text("Your OTP is 482913. Do not share it with anyone.")["message_type"] == "other"
assert core.process_text("Get a pre-approved personal loan from HDFC Bank. Apply now.")["message_type"] == "other"

# --- 3. transaction path with a stubbed model (checks the guards) ---
text = "HDFC Bank: Rs.2,500.00 debited from A/c XX1234 on 15-NOV-26 towards UPI payment to SWIGGY."
core.run_model = lambda t: {"amount": 2500, "type": "debit", "bank": "HDFC", "account": "1234", "merchant": "Swiggy", "category": "food"}
r = core.process_text(text); print("txn ok:", r); assert r["message_type"] == "transaction" and r["amount"] == 2500
core.run_model = lambda t: {"amount": 999, "type": "debit"}                       # hallucinated amount
r = core.process_text(text); print("bad amount:", r); assert r["message_type"] == "needs_review"
core.run_model = lambda t: {"amount": 2500, "type": "credit"}                     # wrong direction
r = core.process_text(text); print("bad type:", r); assert r["message_type"] == "needs_review"
core.run_model = lambda t: {"transaction_type": "debit", "amount": 2500, "bank_name": "HDFC"}   # other key names
r = core.process_text(text); print("key names:", r); assert r["type"] == "debit" and r["bank"] == "HDFC"
core.run_model = lambda t: {"amount": 2500, "type": "debit", "bank": "HDFC", "account": "1234", "merchant": "Swiggy", "category": "food"}

# --- 4. email parsing: HTML body, footer noise ---
em = EmailMessage()
em["From"] = "HDFC Bank <alerts@hdfcbank.net>"; em["To"] = "me@example.com"
em["Subject"] = "Alert"; em["Date"] = "Sun, 15 Nov 2026 10:30:00 +0530"; em["Message-ID"] = "<abc123@hdfc>"
em["Authentication-Results"] = "mx.google.com; dkim=pass; spf=pass"
em.set_content("fallback")
em.add_alternative("<html><body><p>Dear Customer,</p><p>Rs.2,500.00 was debited from A/c XX1234 on 15-NOV-26 towards UPI payment to SWIGGY.</p>"
                   "<p>Please do not share your OTP. Terms and conditions apply. Visit www.hdfcbank.com</p></body></html>", subtype="html")
parsed = mail_reader.parse_email(em.as_bytes(), b"7")
print("parsed:", {k: parsed[k] for k in ("message_id", "sender", "subject", "received_at", "auth_ok")})
snippet = core.clean_email_text(parsed["body"]); print("snippet:", snippet)
assert "debited" in snippet and "Terms" not in snippet and parsed["auth_ok"]

# --- 5. Excel: first insert creates the file and the sheets, second appends ---
path = core.xlsx_path(); assert not os.path.exists(path)
row = service.process_email(parsed); print("row:", row)
print("counts:", core.append_rows([row, {**r, "message_type": "balance_alert", "balance": 45.13, "date": "2026-11-16", "message_id": "<b@x>"}]))
assert os.path.exists(path)
core.append_rows([row])
import pandas as pd
frames = core.load_frames(); print({k: len(v) for k, v in frames.items()})
assert len(frames["transactions"]) == 2 and len(frames["balances"]) == 1 and "needs_review" in frames
assert "<abc123@hdfc>" in core.known_message_ids()
print("\nALL OFFLINE TESTS PASSED")
