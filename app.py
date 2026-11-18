import os

import pandas as pd
import streamlit as st

import core
import service

st.set_page_config(page_title="Bank Alerts Analyzer", page_icon="💳", layout="wide")


# ---------------------------------------------------------------- optional password gate
def gate():
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw or st.session_state.get("authed"):
        return
    entered = st.text_input("Password", type="password")
    if entered and entered == pw:
        st.session_state["authed"] = True
        st.rerun()
    st.stop()


gate()


@st.cache_resource
def get_poller():
    core.restore_backup()                # pull the saved sheet if this container started empty
    return service.Poller(interval_minutes=5)


poller = get_poller()


@st.cache_data(ttl=5)
def load(mtime):
    return core.load_frames()


def mtime():
    p = core.xlsx_path()
    return os.path.getmtime(p) if os.path.exists(p) else 0


# ---------------------------------------------------------------- sidebar
st.sidebar.title("Mailbox")
poller.enabled = st.sidebar.toggle("Auto-fetch new emails", value=poller.enabled)
poller.interval = st.sidebar.number_input("Every (minutes)", 1, 120, int(poller.interval // 60)) * 60

if st.sidebar.button("Fetch emails now", width='stretch'):
    with st.spinner("Reading mailbox and running the model (the first run loads the model)..."):
        res = poller.run_once()
    if res is not None:
        st.sidebar.success(f"Fetched {res['fetched']}, saved {res['saved']}, skipped {res['skipped']}")
        for e in res["errors"]:
            st.sidebar.warning(e)

if poller.state["error"]:
    st.sidebar.error(poller.state["error"])
st.sidebar.caption(f"Last run: {poller.state['last_run'] or 'never'}")
st.sidebar.caption(f"Model: {core.model_status()}")
if st.sidebar.button("Reload data", width='stretch'):
    st.cache_data.clear()
    st.rerun()

p = core.xlsx_path()
if os.path.exists(p):
    with open(p, "rb") as f:
        st.sidebar.download_button("Download Excel", f.read(), file_name=os.path.basename(p),
                                   width='stretch')
    st.sidebar.caption("Storage here is temporary unless you enabled persistent storage or the dataset backup.")

# ---------------------------------------------------------------- data
frames = load(mtime())
tx = frames["transactions"].copy()
bal = frames["balances"].copy()
rev = frames["needs_review"].copy()
tx["amount"] = pd.to_numeric(tx["amount"], errors="coerce")
tx["date"] = pd.to_datetime(tx["date"], errors="coerce")
bal["balance"] = pd.to_numeric(bal["balance"], errors="coerce")
bal["date"] = pd.to_datetime(bal["date"], errors="coerce")

st.title("💳 Bank Alerts Analyzer")
tab_dash, tab_tx, tab_bal, tab_rev, tab_try = st.tabs(
    ["Dashboard", "Transactions", "Balances", "Needs review", "Try a message"])

# ---------------------------------------------------------------- dashboard
with tab_dash:
    if tx.empty:
        st.info("No transactions yet. Click “Fetch emails now”, or try a message in the last tab.")
    else:
        c1, c2, c3 = st.columns(3)
        banks = sorted(tx["bank"].dropna().unique())
        sel = c1.multiselect("Bank", banks, default=banks)
        dmin, dmax = tx["date"].min(), tx["date"].max()
        rng = c2.date_input("Date range", (dmin.date(), dmax.date())) if pd.notna(dmin) else ()
        f = tx[tx["bank"].isin(sel) | tx["bank"].isna()]
        if len(rng) == 2:
            f = f[(f["date"] >= pd.Timestamp(rng[0])) & (f["date"] <= pd.Timestamp(rng[1]))]

        debit = f.loc[f["type"] == "debit", "amount"].sum()
        credit = f.loc[f["type"] == "credit", "amount"].sum()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total spent", f"{debit:,.2f}")
        m2.metric("Total received", f"{credit:,.2f}")
        m3.metric("Net", f"{credit - debit:,.2f}")
        m4.metric("Transactions", len(f))

        left, right = st.columns(2)
        spend = f[f["type"] == "debit"].copy()
        spend["category"] = spend["category"].fillna("uncategorized")
        left.subheader("Spend by category")
        if not spend.empty:
            left.bar_chart(spend.groupby("category")["amount"].sum().sort_values(ascending=False))
        right.subheader("Monthly debit vs credit")
        monthly = f.dropna(subset=["date"]).assign(month=lambda d: d["date"].dt.to_period("M").astype(str))
        if not monthly.empty:
            right.bar_chart(monthly.pivot_table(index="month", columns="type", values="amount",
                                                aggfunc="sum", fill_value=0))
        st.subheader("Top merchants")
        if not spend.empty and spend["merchant"].notna().any():
            st.bar_chart(spend.groupby("merchant")["amount"].sum().sort_values(ascending=False).head(10))

# ---------------------------------------------------------------- tables
with tab_tx:
    st.dataframe(tx.sort_values("date", ascending=False), width='stretch', hide_index=True)

with tab_bal:
    if bal.empty:
        st.info("No balance alerts yet.")
    else:
        b = bal.dropna(subset=["date", "balance"])
        if not b.empty:
            b["acct"] = b["bank"].fillna("") + " " + b["account"].astype(str)
            st.line_chart(b.pivot_table(index="date", columns="acct", values="balance", aggfunc="last"))
        st.dataframe(bal.sort_values("date", ascending=False), width='stretch', hide_index=True)

with tab_rev:
    st.caption("Messages the checks rejected (amount not in the text, wording contradicts the type, ...). Review by hand.")
    st.dataframe(rev, width='stretch', hide_index=True)

# ---------------------------------------------------------------- manual test
with tab_try:
    text = st.text_area("Paste an SMS or email text", height=120)
    save = st.checkbox("Also save the result to the Excel file")
    if st.button("Extract") and text.strip():
        with st.spinner("Running the model..."):
            row = service.row_from_text(text, {"received_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
                                               "sender": "manual", "subject": "manual",
                                               "message_id": f"manual-{pd.Timestamp.now().value}"})
        if row is None:
            st.warning("Not a transaction or balance alert (OTP, promotion, ...). Nothing extracted.")
        else:
            st.json(row)
            if save:
                st.success(f"Saved: {core.append_rows([row])}")
