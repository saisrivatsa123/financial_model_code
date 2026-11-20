"""Neon PostgreSQL persistence for bank alerts."""
import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv

load_dotenv()

KINDS = {'transaction': 'transactions', 'balance_alert': 'balances', 'needs_review': 'needs_review'}


def get_connection():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=15)


def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS transactions (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                transaction_key TEXT NOT NULL UNIQUE,
                message_id TEXT,
                bank_name TEXT,
                account_number TEXT,
                transaction_type TEXT,
                amount NUMERIC(18,2),
                currency TEXT DEFAULT 'INR',
                transaction_date TIMESTAMP,
                transaction_mode TEXT,
                merchant TEXT,
                reference_number TEXT,
                category TEXT,
                raw_data JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS bank_messages (
                message_id TEXT PRIMARY KEY,
                message_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )''')
            cur.execute('''CREATE INDEX IF NOT EXISTS idx_transactions_date
                ON transactions(transaction_date DESC)''')


def _str(value):
    return str(value).strip().lower() if value is not None else ''


def transaction_key(row):
    bank = _str(row.get('bank') or row.get('bank_name'))
    account = _str(row.get('account') or row.get('account_number'))
    reference = _str(row.get('reference') or row.get('reference_number'))
    direction = _str(row.get('type') or row.get('transaction_type'))
    if reference:
        identity = ['reference', bank, account, reference, direction]
    else:
        # Without a bank reference, an exact duplicate cannot be guaranteed.
        # Prefer email identity to avoid conflating two same-amount payments.
        mid = _str(row.get('message_id'))
        if mid:
            identity = ['message', mid]
        else:
            identity = ['manual', bank, account, _str(row.get('date') or row.get('transaction_date')),
                        _str(row.get('amount')), direction, _str(row.get('merchant'))]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _timestamp(value):
    if value is None or str(value).strip() == '':
        return None
    if isinstance(value, datetime):
        return value
    return str(value)


def save_rows(rows):
    """Atomically save rows and processed message IDs. Returns inserted counts."""
    counts = {'transactions': 0, 'balances': 0, 'needs_review': 0}
    if not rows:
        return counts
    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                kind = row.get('message_type')
                if kind not in KINDS:
                    continue
                mid = str(row.get('message_id') or '').strip()
                # Message ID is unique across all message types.
                if mid:
                    cur.execute('''INSERT INTO bank_messages (message_id, message_type, payload)
                        VALUES (%s,%s,%s) ON CONFLICT (message_id) DO NOTHING
                        RETURNING message_id''', (mid, kind, Jsonb(row)))
                    if cur.fetchone() is None:
                        continue
                elif kind != 'transaction':
                    raise ValueError('Non-transaction rows require message_id')

                if kind == 'transaction':
                    cur.execute('''INSERT INTO transactions (
                        transaction_key, message_id, bank_name, account_number,
                        transaction_type, amount, currency, transaction_date,
                        transaction_mode, merchant, reference_number, category, raw_data
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (transaction_key) DO NOTHING RETURNING id''', (
                        transaction_key(row), mid or None, row.get('bank') or row.get('bank_name'),
                        row.get('account') or row.get('account_number'),
                        row.get('type') or row.get('transaction_type'),
                        row.get('amount'), row.get('currency') or 'INR',
                        _timestamp(row.get('date') or row.get('transaction_date')),
                        row.get('transaction_mode'), row.get('merchant'),
                        row.get('reference') or row.get('reference_number'),
                        row.get('category'), Jsonb(row)))
                    if cur.fetchone() is not None:
                        counts['transactions'] += 1
                else:
                    counts[KINDS[kind]] += 1
    return counts


def known_message_ids():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT message_id FROM bank_messages')
            return {r[0] for r in cur.fetchall()}


def load_frames():
    import pandas as pd
    import core
    frames = {name: [] for name in core.SHEETS}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT raw_data FROM transactions ORDER BY created_at DESC')
            for (row,) in cur.fetchall():
                frames['transactions'].append(row)
            cur.execute("SELECT message_type, payload FROM bank_messages WHERE message_type <> 'transaction' ORDER BY created_at DESC")
            for kind, payload in cur.fetchall():
                frames[KINDS[kind]].append(payload)
    return {name: pd.DataFrame(rows, columns=cols) if not rows else
            pd.DataFrame(rows).reindex(columns=cols)
            for name, (_, cols) in core.SHEETS.items()
            for rows in [frames[name]]}


def insert_transaction(row):
    if row.get('message_type', 'transaction') != 'transaction':
        raise ValueError('Expected transaction row')
    return save_rows([{**row, 'message_type': 'transaction'}])['transactions'] > 0
