#!/usr/bin/env python3
"""Safe starter queue for an isolated autonomous project worker.

This module deliberately does not execute shell commands or modify project files.
Provider-backed code editing is a later milestone, after credentials and a
disposable test workspace are configured.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("WORKER_DB", "worker_tasks.sqlite3"))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            checkpoint TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    db.commit()
    return db


def add_task(title: str, details: str) -> int:
    now = time.time()
    with connect() as db:
        cur = db.execute(
            "INSERT INTO tasks(title, details, created_at, updated_at) VALUES(?,?,?,?)",
            (title.strip(), details.strip(), now, now),
        )
        return int(cur.lastrowid)


def list_tasks() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT id,title,status,attempts,checkpoint,result,created_at,updated_at "
            "FROM tasks ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def set_checkpoint(task_id: int, checkpoint: str) -> bool:
    with connect() as db:
        cur = db.execute(
            "UPDATE tasks SET checkpoint=?, updated_at=? WHERE id=?",
            (checkpoint, time.time(), task_id),
        )
        return cur.rowcount == 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="queue a task")
    add.add_argument("title")
    add.add_argument("details")
    sub.add_parser("list", help="list queued tasks")
    check = sub.add_parser("checkpoint", help="save a task checkpoint")
    check.add_argument("task_id", type=int)
    check.add_argument("message")
    args = parser.parse_args()

    if args.command == "add":
        task_id = add_task(args.title, args.details)
        print(json.dumps({"id": task_id, "status": "queued"}))
    elif args.command == "list":
        print(json.dumps(list_tasks(), indent=2))
    elif args.command == "checkpoint":
        ok = set_checkpoint(args.task_id, args.message)
        print(json.dumps({"updated": ok, "task_id": args.task_id}))
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
