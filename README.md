---
title: Bank Alerts Analyzer
emoji: 💳
colorFrom: blue
colorTo: green
sdk: streamlit
app_file: app.py
pinned: false
---

Reads HDFC/ICICI alert emails over IMAP, extracts the data with a small fine-tuned GGUF model
(LangChain `LlamaCpp`), appends it to an Excel file, and shows it in Streamlit.

The model is NOT stored in this Space. It is downloaded from `MODEL_URL` on first use.

Variables: `MODEL_URL` (GitHub raw link to the .gguf; several links can be comma-separated),
`IMAP_HOST`, `ALLOWED_SENDERS`, `HF_DATASET_REPO`, `REQUIRE_AUTH`
Secrets: `IMAP_USER`, `IMAP_PASSWORD`, `APP_PASSWORD`, `HF_TOKEN`, `GITHUB_TOKEN` (private repo only)
