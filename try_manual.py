"""Local test: type or paste a message, the model processes it, the result is appended to Excel.
The Excel file and its sheets are created on the first insert (default: data/bank_messages.xlsx).

Windows PowerShell:
    pip install langchain-community langchain-core openpyxl requests
    pip install llama-cpp-python --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
    $env:GGUF_PATH = "C:\\path\\to\\finee-gpt2-q4_k_m.gguf"      # or: $env:MODEL_URL = "https://github.com/<user>/<repo>/raw/main/<path>.gguf"
    python try_manual.py                       # paste a message, press Enter twice; type q to quit
    python try_manual.py --file messages.txt   # one message per line

Close the Excel file before running: Windows will not let the script save while it is open.
"""
import json
import os
import sys
import time

import core
import service


def handle(text):
    """Process one message and append it to Excel. Returns a short status line."""
    meta = {"received_at": time.strftime("%Y-%m-%d %H:%M"), "sender": "manual", "subject": "manual",
            "message_id": f"manual-{time.time_ns()}"}
    row = service.row_from_text(text, meta)
    if row is None:
        return "-> not a transaction or balance alert (OTP, promotion, ...). Nothing saved."
    print(json.dumps(row, indent=2, ensure_ascii=False))
    try:
        counts = core.append_rows([row])
    except PermissionError:
        return "-> could not save: close the Excel file and try again."
    return f"-> saved {counts} to {os.path.abspath(core.xlsx_path())}"


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")      # Windows consoles choke on the rupee sign otherwise
    except Exception:
        pass
    print("Model:", core.model_status(), "(the first message loads the model, so it takes a while)\n")
    if len(argv) > 1 and argv[0] == "--file":
        for line in open(argv[1], encoding="utf-8").read().splitlines():
            if line.strip():
                print("\nINPUT:", line)
                print(handle(line))
        return
    while True:
        print("Paste a message, then press Enter on an empty line (q = quit):")
        lines = []
        try:
            while True:
                line = input()
                if not lines and line.strip().lower() == "q":
                    return
                if not line.strip():
                    break
                lines.append(line)
        except EOFError:
            if not lines:
                return
        text = " ".join(lines).strip()
        if not text:
            return
        print(handle(text), "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
