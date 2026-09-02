"""SQLite store for proposals and their review decisions. This is what makes re-runs safe."""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import config

# A rejection is a standing human decision: never ask again. An *applied* proposal that is
# generated again means the CRM drifted back (someone re-broke the field), so it re-opens.
PERMANENT = ("rejected",)
REOPENABLE = ("applied", "failed")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path=None):
        self.path = str(path or config.STATE_DB)
        config.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS proposals (
            fingerprint TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            title TEXT, summary TEXT, confidence TEXT,
            actions TEXT NOT NULL, evidence TEXT NOT NULL, key TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            first_seen TEXT, last_seen TEXT, run_id INTEGER,
            decided_at TEXT, decided_by TEXT, applied_at TEXT, result TEXT,
            reopened INTEGER NOT NULL DEFAULT 0, prev_result TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started TEXT, finished TEXT, summary TEXT
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, fingerprint TEXT, action TEXT, detail TEXT
        );
        """)
        self.db.commit()
        for col, ddl in (("reopened", "INTEGER NOT NULL DEFAULT 0"), ("prev_result", "TEXT")):
            if col not in [r[1] for r in self.db.execute("PRAGMA table_info(proposals)")]:
                self.db.execute(f"ALTER TABLE proposals ADD COLUMN {col} {ddl}")
        self.db.commit()

    # ---- runs
    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs (started) VALUES (?)", (now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, summary: Dict) -> None:
        self.db.execute("UPDATE runs SET finished=?, summary=? WHERE run_id=?", (now(), json.dumps(summary), run_id))
        self.db.commit()

    def runs(self, limit=10) -> List[Dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,))]

    # ---- proposals
    def upsert_proposals(self, proposals, run_id: int) -> Dict:
        """Insert new proposals as pending; refresh evidence on still-pending ones; never re-ask
        about rejected ones; re-open applied/failed ones that come back (drift); mark pending
        ones that did not reappear as stale."""
        seen = set()
        counts = {"new": 0, "still_pending": 0, "already_decided": 0, "stale": 0, "revived": 0, "reopened": 0}
        ts = now()
        for p in proposals:
            seen.add(p.fingerprint)
            row = self.db.execute("SELECT status FROM proposals WHERE fingerprint=?", (p.fingerprint,)).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO proposals (fingerprint,kind,subject,title,summary,confidence,actions,evidence,key,"
                    "status,first_seen,last_seen,run_id) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                    (p.fingerprint, p.kind, p.subject, p.title, p.summary, p.confidence,
                     json.dumps(p.actions), json.dumps(p.evidence), json.dumps(p.key), ts, ts, run_id))
                counts["new"] += 1
            elif row["status"] in PERMANENT:
                counts["already_decided"] += 1
                self.db.execute("UPDATE proposals SET last_seen=? WHERE fingerprint=?", (ts, p.fingerprint))
            elif row["status"] in REOPENABLE or row["status"] == "approved":
                counts["reopened"] += 1
                prev = self.db.execute("SELECT status, applied_at, result FROM proposals WHERE fingerprint=?",
                                       (p.fingerprint,)).fetchone()
                p.evidence = {**p.evidence, "history": f"Previously {prev['status']} at {prev['applied_at'] or '?'}; "
                                                       f"the same difference is back in the CRM, so it is proposed again."}
                self.db.execute(
                    "UPDATE proposals SET title=?,summary=?,confidence=?,actions=?,evidence=?,status='pending',"
                    "last_seen=?,run_id=?,reopened=reopened+1,prev_result=result,result=NULL,decided_at=NULL,"
                    "decided_by=NULL,applied_at=NULL WHERE fingerprint=?",
                    (p.title, p.summary, p.confidence, json.dumps(p.actions), json.dumps(p.evidence), ts, run_id,
                     p.fingerprint))
                self.db.execute("INSERT INTO audit (ts,fingerprint,action,detail) VALUES (?,?,?,?)",
                                (ts, p.fingerprint, "reopened", prev["result"]))
            else:  # pending or stale -> refresh and (re)activate
                if row["status"] == "stale":
                    counts["revived"] += 1
                else:
                    counts["still_pending"] += 1
                self.db.execute(
                    "UPDATE proposals SET title=?,summary=?,confidence=?,actions=?,evidence=?,status='pending',"
                    "last_seen=?,run_id=? WHERE fingerprint=?",
                    (p.title, p.summary, p.confidence, json.dumps(p.actions), json.dumps(p.evidence), ts, run_id,
                     p.fingerprint))
        for r in self.db.execute("SELECT fingerprint FROM proposals WHERE status='pending'"):
            if r["fingerprint"] not in seen:
                self.db.execute("UPDATE proposals SET status='stale' WHERE fingerprint=?", (r["fingerprint"],))
                counts["stale"] += 1
        self.db.commit()
        return counts

    def list(self, status: Optional[str] = None, kind: Optional[str] = None) -> List[Dict]:
        q, args = "SELECT * FROM proposals", []
        conds = []
        if status:
            conds.append("status=?"); args.append(status)
        if kind:
            conds.append("kind=?"); args.append(kind)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, kind, title"
        return [self._hydrate(r) for r in self.db.execute(q, args)]

    def get(self, fingerprint: str) -> Optional[Dict]:
        r = self.db.execute("SELECT * FROM proposals WHERE fingerprint=?", (fingerprint,)).fetchone()
        return self._hydrate(r) if r else None

    def _hydrate(self, r) -> Dict:
        d = dict(r)
        for k in ("actions", "evidence", "key", "result"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        return d

    def set_status(self, fingerprint: str, status: str, by: str = "reviewer", result: Optional[Dict] = None) -> None:
        cols = {"status": status}
        if status in ("approved", "rejected"):
            cols["decided_at"] = now(); cols["decided_by"] = by
        if status in ("applied", "failed"):
            cols["applied_at"] = now()
        if result is not None:
            cols["result"] = json.dumps(result)
        sets = ", ".join(f"{k}=?" for k in cols)
        self.db.execute(f"UPDATE proposals SET {sets} WHERE fingerprint=?", (*cols.values(), fingerprint))
        self.db.execute("INSERT INTO audit (ts,fingerprint,action,detail) VALUES (?,?,?,?)",
                        (now(), fingerprint, status, json.dumps(result) if result else None))
        self.db.commit()

    def counts(self) -> Dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}

    def audit(self, limit=200) -> List[Dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]
