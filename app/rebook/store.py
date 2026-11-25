"""改期退款案件的 SQLite 存储。

案件按原票号唯一，整份领域状态以 JSON 落盘；写操作在事务内完成，
配合领域层的幂等键，保证乱序回执与签发重试不产生重复款项。
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainIssue


class RebookStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS rebook_cases(
              original_ticket_no TEXT PRIMARY KEY,
              rebook_no TEXT,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rebook_audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              original_ticket_no TEXT NOT NULL,
              action TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            ''')

    def get_state(self, ticket_no):
        with self.connect() as db:
            row = db.execute(
                "SELECT state FROM rebook_cases WHERE original_ticket_no=?",
                (ticket_no,)).fetchone()
        return json.loads(row["state"]) if row else None

    def exists(self, ticket_no):
        return self.get_state(ticket_no) is not None

    def list_states(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT state FROM rebook_cases ORDER BY original_ticket_no").fetchall()
        return [json.loads(row["state"]) for row in rows]

    def insert_case(self, case):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO rebook_cases(original_ticket_no,rebook_no,status,created_at,updated_at,state)"
                    " VALUES(?,?,?,?,?,?)",
                    (case["original_ticket_no"], case.get("rebook_no"), case["status"],
                     now, now, json.dumps(case, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                raise DomainIssue("REBOOK_DUPLICATE_TICKET", 409)
        self.audit(case["original_ticket_no"], "OPEN_CASE", {"status": case["status"]})

    def save_state(self, ticket_no, case, action, detail):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT original_ticket_no FROM rebook_cases WHERE original_ticket_no=?",
                (ticket_no,)).fetchone()
            if row is None:
                raise DomainIssue("REBOOK_CASE_NOT_FOUND", 404)
            db.execute(
                "UPDATE rebook_cases SET status=?, rebook_no=?, updated_at=?, state=?"
                " WHERE original_ticket_no=?",
                (case["status"], case.get("rebook_no"), now,
                 json.dumps(case, ensure_ascii=False), ticket_no))
        self.audit(ticket_no, action, detail)

    def audit(self, ticket_no, action, detail):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO rebook_audit(original_ticket_no,action,detail,created_at)"
                " VALUES(?,?,?,?)",
                (ticket_no, action, json.dumps(detail, ensure_ascii=False), now))

    def list_audit(self, ticket_no):
        with self.connect() as db:
            rows = db.execute(
                "SELECT action, detail, created_at FROM rebook_audit"
                " WHERE original_ticket_no=? ORDER BY id", (ticket_no,)).fetchall()
        return [{"action": row["action"], "detail": json.loads(row["detail"]),
                 "created_at": row["created_at"]} for row in rows]
