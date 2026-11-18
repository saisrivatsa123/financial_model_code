"""Core logic: model (LangChain LlamaCpp), balance-alert rules, validation, Excel storage."""
import json
import os
import re
import threading
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

# ----------------------------------------------------------------------------
# Settings (override with Space "Variables and secrets")
# ----------------------------------------------------------------------------
GGUF_PATH = os.environ.get("GGUF_PATH", "models/finee-gpt2-q4_k_m.gguf")
MODEL_URL = os.environ.get("MODEL_URL", "https://github.com/saisrivatsa123/LLM_Finance_Model/blob/main/finee-gpt2-f16.gguf")          # one or more comma-separated links, tried in order (GitHub raw/blob links or Hugging Face resolve links)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")    # only if the GitHub repo is private
XLSX_NAME = os.environ.get("XLSX_NAME", "bank_messages.xlsx")
HF_DATASET_REPO = os.environ.get("HF_DATASET_REPO", "")   # optional private dataset repo used as a backup of the sheet
HF_TOKEN = os.environ.get("HF_TOKEN", "")


def data_dir():
    d = os.environ.get("DATA_DIR")
    if not d:
        d = "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else "data"
    os.makedirs(d, exist_ok=True)
    return d


def xlsx_path():
    return os.path.join(data_dir(), XLSX_NAME)


# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------
_LLM = None
_LLM_LOCK = threading.Lock()
_GEN_LOCK = threading.Lock()      # llama.cpp is not thread-safe: one generation at a time


def _is_gguf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"GGUF" and os.path.getsize(path) > 1_000_000
    except OSError:
        return False


def _normalize_url(u):
    """A github.com/.../blob/... page link is turned into the raw download link."""
    u = u.strip()
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)", u)
    return f"https://github.com/{m[1]}/{m[2]}/raw/{m[3]}" if m else u


def _headers_for(url):
    host = urlparse(url).netloc
    if host.endswith("github.com") or host.endswith("githubusercontent.com"):
        return {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    if host.endswith("huggingface.co"):
        return {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    return {}


def _download(url, dest):
    import requests
    tmp = dest + ".part"
    with requests.get(url, headers=_headers_for(url), stream=True, timeout=120, allow_redirects=True) as r:
        r.raise_for_status()
        chunks = r.iter_content(1 << 20)
        first = next(chunks, b"")
        if not first.startswith(b"GGUF"):
            if first.startswith(b"version https://git-lfs"):
                raise RuntimeError("the link returned a Git LFS pointer, not the model "
                                   "(the file was pushed without LFS, or the repo is private)")
            raise RuntimeError(f"the link did not return a GGUF file (starts with {first[:30]!r})")
        with open(tmp, "wb") as f:
            f.write(first)
            for c in chunks:
                f.write(c)
    os.replace(tmp, dest)


def model_status():
    if _is_gguf(GGUF_PATH):
        return f"ready ({os.path.getsize(GGUF_PATH) / 1e6:.0f} MB)"
    return "downloads on first use" if MODEL_URL else "missing: set MODEL_URL"


def ensure_model():
    if _is_gguf(GGUF_PATH):
        return GGUF_PATH
    urls = [_normalize_url(u) for u in MODEL_URL.split(",") if u.strip()]
    if os.path.exists(GGUF_PATH):                          # e.g. an LFS pointer committed by mistake
        if not urls:
            raise RuntimeError(f"{GGUF_PATH} exists but is not a GGUF file (a Git LFS pointer?). "
                               "Set MODEL_URL or re-upload the model with Git LFS.")
        os.remove(GGUF_PATH)
    if not urls:
        raise FileNotFoundError(f"{GGUF_PATH} not found. Set MODEL_URL to the GitHub link of the .gguf file.")
    os.makedirs(os.path.dirname(GGUF_PATH) or ".", exist_ok=True)
    errors = []
    for u in urls:
        try:
            _download(u, GGUF_PATH)
            return GGUF_PATH
        except Exception as e:
            errors.append(f"{u} -> {e}")
    raise RuntimeError("Could not download the model:\n" + "\n".join(errors))


def get_llm():
    global _LLM
    with _LLM_LOCK:
        if _LLM is None:
            from langchain_community.llms import LlamaCpp
            _LLM = LlamaCpp(
                model_path=ensure_model(),
                n_ctx=1024,
                n_batch=256,                       # the default (8) makes prompt reading slow
                n_threads=os.cpu_count() or 2,
                max_tokens=200,                    # JSON answers are short; 1000 only invites rambling
                temperature=0,                     # deterministic extraction (0.3 can corrupt numbers)
                verbose=False,
            )
        return _LLM


def prompt_of(text):
    # Must match the training format exactly
    return f"Extract financial entities from: {text}\n### JSON:\n"


def first_json(text):
    i = text.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
        return obj
    except json.JSONDecodeError:
        return None


def run_model(text):
    llm = get_llm()
    with _GEN_LOCK:
        out = llm.invoke(prompt_of(text), stop=["###", "<|endoftext|>"])
    return first_json(out)


# ----------------------------------------------------------------------------
# Key names: the model may use different names than the sheet; map everything to one set
# ----------------------------------------------------------------------------
CANDIDATES = {
    "amount": ["amount"],
    "type": ["type", "transaction_type"],
    "bank": ["bank", "bank_name"],
    "account": ["account", "account_number", "account_last4", "account_no"],
    "merchant": ["merchant"],
    "category": ["category"],
    "reference": ["reference", "reference_number", "ref_no", "ref"],
    "balance": ["balance", "available_balance"],
    "date": ["date", "transaction_date"],
}
_INV = {name: canon for canon, names in CANDIDATES.items() for name in names}


def to_canonical(d):
    return {_INV.get(k, k): v for k, v in d.items()}


# ----------------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------------
DEBIT = re.compile(r"\b(debited|spent|paid|sent|withdrawn|withdrawal|purchase|transferred|EMI|charged|deducted)\b", re.I)
CREDIT = re.compile(r"\b(credited|received|refund|reversal|deposited|deposit)\b", re.I)
CURRENCY = re.compile(r"(?:rs\.?|inr|₹)\s*[\d,]+", re.I)
BAL_KW = re.compile(r"\b(avail(?:able)?\.?\s*bal(?:ance)?|avl\.?\s*bal(?:ance)?|a/?c\.?\s*bal(?:ance)?|balance)\b", re.I)
BAL_AMT = re.compile(r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)", re.I)
ACCT = re.compile(r"[Xx*]{2,}(\d{3,4})")
BANK_NAME = re.compile(r"\b(HDFC|SBI|ICICI|Axis|Kotak|PNB|Canara|Union|IDFC|Yes Bank|Federal|IndusInd|Bank of Baroda|BOB)\b", re.I)
DATE_RE = re.compile(r"(\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})")


def parse_date(s):
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d/%b/%y", "%d/%b/%Y",
                "%d-%m-%y", "%d-%m-%Y", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.title(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_balance_alert(msg):
    if not BAL_KW.search(msg):
        return None
    amt = BAL_AMT.search(msg)
    if not amt:
        return None
    acct, bank, dt = ACCT.search(msg), BANK_NAME.search(msg), DATE_RE.search(msg)
    return {"message_type": "balance_alert",
            "bank": bank.group(1).upper() if bank else None,
            "account": acct.group(1) if acct else None,
            "balance": float(amt.group(1).replace(",", "")),
            "date": parse_date(dt.group(1)) if dt else None}


def numbers_in(text):
    out = []
    for n in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.append(float(n.replace(",", "")))
        except ValueError:
            pass
    return out


def validate_transaction(data, message):
    """Check the model's answer against the text. Returns (clean, None) or (None, reason)."""
    if not isinstance(data, dict) or not data:
        return None, "empty"
    try:
        amt = float(data.get("amount"))
    except (TypeError, ValueError):
        return None, "no valid amount"
    if not any(abs(n - amt) < 0.01 for n in numbers_in(message)):
        return None, "amount not found in message"
    has_d, has_c = bool(DEBIT.search(message)), bool(CREDIT.search(message))
    t = data.get("type")
    if t not in ("debit", "credit"):
        t = "debit" if has_d and not has_c else "credit" if has_c and not has_d else None
    if t is None:
        return None, "type unclear"
    if (t == "debit" and not has_d) or (t == "credit" and not has_c):
        return None, f"type '{t}' contradicts message wording"
    data = dict(data)
    data["type"] = t
    return data, None


def process_text(message):
    """One message -> {message_type: transaction | balance_alert | needs_review | other, ...}"""
    message = message.strip()
    if DEBIT.search(message) or CREDIT.search(message):
        if not CURRENCY.search(message):
            return {"message_type": "other"}
        data = run_model(message)
        if not data:
            return {"message_type": "other"}
        data, why = validate_transaction(to_canonical(data), message)
        if data is None:
            return {"message_type": "needs_review", "reason": why}
        data["message_type"] = "transaction"
        return data
    return parse_balance_alert(message) or {"message_type": "other"}


# ----------------------------------------------------------------------------
# Email text cleaning: keep only the sentences that matter (emails are long, the model is small)
# ----------------------------------------------------------------------------
class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    p = _Strip()
    p.feed(html)
    return "".join(p.parts)


def clean_email_text(body, max_chars=700):
    text = body.replace("\xa0", " ").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    pieces = []
    for line in text.split("\n"):
        pieces += re.split(r"(?<=[.!?])\s+(?=[A-Z])", line.strip())
    keep = [s.strip() for s in pieces
            if s.strip() and (DEBIT.search(s) or CREDIT.search(s) or CURRENCY.search(s) or BAL_KW.search(s))]
    return " ".join(keep[:4])[:max_chars].strip()


# ----------------------------------------------------------------------------
# Excel storage: the file and its sheets are created on the first insert
# ----------------------------------------------------------------------------
TX_COLS = ["date", "received_at", "type", "amount", "bank", "account", "merchant", "category",
           "reference", "balance", "sender", "subject", "message_id", "message"]
BAL_COLS = ["date", "received_at", "bank", "account", "balance", "sender", "subject", "message_id", "message"]
REV_COLS = ["received_at", "reason", "sender", "subject", "message_id", "message"]
SHEETS = {"transactions": ("transaction", TX_COLS),
          "balances": ("balance_alert", BAL_COLS),
          "needs_review": ("needs_review", REV_COLS)}
_XLSX_LOCK = threading.Lock()


def _backup_up(path):
    if not (HF_DATASET_REPO and HF_TOKEN):
        return
    try:
        from huggingface_hub import HfApi
        HfApi().upload_file(path_or_fileobj=path, path_in_repo=os.path.basename(path),
                            repo_id=HF_DATASET_REPO, repo_type="dataset", token=HF_TOKEN)
    except Exception as e:                       # backup must never break the pipeline
        print("backup upload failed:", e)


def restore_backup():
    """Call once at startup: if the local sheet is missing, pull the backup copy."""
    path = xlsx_path()
    if os.path.exists(path) or not (HF_DATASET_REPO and HF_TOKEN):
        return
    try:
        from huggingface_hub import hf_hub_download
        hf_hub_download(repo_id=HF_DATASET_REPO, filename=os.path.basename(path),
                        repo_type="dataset", token=HF_TOKEN, local_dir=os.path.dirname(path))
    except Exception as e:
        print("no backup restored:", e)


def _open_book(path):
    from openpyxl import Workbook, load_workbook
    if os.path.exists(path):
        wb = load_workbook(path)
    else:
        wb = Workbook()
        wb.remove(wb.active)
    for name, (_, cols) in SHEETS.items():
        if name not in wb.sheetnames:
            wb.create_sheet(name).append(cols)
    return wb


def append_rows(rows):
    """Append rows to the right sheet; creates the workbook on first use. Returns counts."""
    counts = {k: 0 for k in SHEETS}
    if not rows:
        return counts
    path = xlsx_path()
    with _XLSX_LOCK:
        wb = _open_book(path)
        for r in rows:
            for name, (mtype, cols) in SHEETS.items():
                if r.get("message_type") == mtype:
                    wb[name].append([r.get(c) for c in cols])
                    counts[name] += 1
        wb.save(path)
    _backup_up(path)
    return counts


def known_message_ids():
    path = xlsx_path()
    ids = set()
    if not os.path.exists(path):
        return ids
    with _XLSX_LOCK:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True)
        for name, (_, cols) in SHEETS.items():
            if name in wb.sheetnames and "message_id" in cols:
                idx = cols.index("message_id")
                for row in wb[name].iter_rows(min_row=2, values_only=True):
                    if len(row) > idx and row[idx]:
                        ids.add(row[idx])
        wb.close()
    return ids


def load_frames():
    import pandas as pd
    path = xlsx_path()
    if not os.path.exists(path):
        return {name: pd.DataFrame(columns=cols) for name, (_, cols) in SHEETS.items()}
    with _XLSX_LOCK:
        return pd.read_excel(path, sheet_name=None)
