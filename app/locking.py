"""Process-wide serialization for check-then-write state mutations.

The single supported uvicorn worker runs synchronous endpoints in a thread
pool, so one re-entrant lock serializes registrations, scans and fixture-plan
adoptions.  SQLite ``BEGIN IMMEDIATE`` transactions additionally make the same
check-then-write sections correct across processes.

Plan *evaluation* is read-only and deliberately does not take this lock, so
box registration stays fully available while a fixture plan is computed
("规划期间照常挂牌"); adoption re-verifies against the current registry under
the lock/transaction.
"""
from __future__ import annotations

import threading

OPERATION_LOCK = threading.RLock()
