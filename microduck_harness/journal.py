"""Durable receipts and bounded execution history, with no automatic resumption."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

TERMINAL = {"succeeded", "failed", "cancelled", "blocked", "interrupted", "budget_exhausted"}


class Journal:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                request TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                at REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL
            );
        """)
        # Power or process loss is not evidence of completion.
        for row in self.db.execute("SELECT id,body FROM tasks WHERE status IN ('planning','running','accepted')").fetchall():
            body = json.loads(row["body"])
            body.update(status="interrupted", verified_success=False, reason="gateway restarted; explicit new task required")
            self.save(body)

    def save(self, task: dict):
        self.db.execute("INSERT INTO tasks(id,request_id,request,status,body) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET status=excluded.status,body=excluded.body",
                        (task["id"], task["request_id"], task["request_canonical"], task["status"], json.dumps(task, ensure_ascii=False)))
        self.db.commit()

    def by_request(self, request_id: str):
        row = self.db.execute("SELECT body FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, task_id: str):
        row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def tasks(self, limit: int = 30):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,))]

    def event(self, task_id: str | None, kind: str, body: dict):
        self.db.execute("INSERT INTO events(task_id,at,kind,body) VALUES(?,?,?,?)",
                        (task_id, time.time(), kind, json.dumps(body, ensure_ascii=False)))
        self.db.commit()

    def events(self, after: int = 0, limit: int = 100):
        return [{**dict(r), "body": json.loads(r["body"])} for r in self.db.execute(
            "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, min(limit, 500)))]

    def close(self):
        self.db.close()
