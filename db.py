import sqlite3
import json
from datetime import datetime, timezone

DB_PATH = "provenance.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS submissions (
            content_id  TEXT PRIMARY KEY,
            creator_id  TEXT NOT NULL,
            text        TEXT NOT NULL,
            attribution TEXT NOT NULL,
            confidence  REAL NOT NULL,
            llm_score   REAL,
            stylo_score REAL,
            status      TEXT NOT NULL DEFAULT 'classified',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id TEXT    NOT NULL,
            event      TEXT    NOT NULL,
            data       TEXT    NOT NULL,
            timestamp  TEXT    NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def insert_submission(content_id, creator_id, text, attribution, confidence,
                      llm_score, stylo_score):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        """INSERT INTO submissions
           (content_id, creator_id, text, attribution, confidence,
            llm_score, stylo_score, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'classified', ?)""",
        (content_id, creator_id, text, attribution, confidence,
         llm_score, stylo_score, now),
    )
    log_data = {
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": confidence,
        "llm_score": llm_score,
        "stylo_score": stylo_score,
        "status": "classified",
    }
    conn.execute(
        "INSERT INTO audit_log (content_id, event, data, timestamp) VALUES (?, ?, ?, ?)",
        (content_id, "classification", json.dumps(log_data), now),
    )
    conn.commit()
    conn.close()


def get_submission(content_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_under_review(content_id, creator_reasoning):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE submissions SET status = 'under_review' WHERE content_id = ?",
        (content_id,),
    )
    log_data = {
        "content_id": content_id,
        "creator_reasoning": creator_reasoning,
        "status": "under_review",
    }
    conn.execute(
        "INSERT INTO audit_log (content_id, event, data, timestamp) VALUES (?, ?, ?, ?)",
        (content_id, "appeal_filed", json.dumps(log_data), now),
    )
    conn.commit()
    conn.close()


def get_log(limit=20):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    entries = []
    for row in rows:
        entry = dict(row)
        entry["data"] = json.loads(entry["data"])
        entries.append(entry)
    return entries


def get_analytics():
    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) FROM submissions"
    ).fetchone()[0]

    attr_rows = conn.execute(
        "SELECT attribution, COUNT(*) as cnt FROM submissions GROUP BY attribution"
    ).fetchall()

    appeal_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE event = 'appeal_filed'"
    ).fetchone()[0]

    conf_rows = conn.execute(
        """SELECT attribution,
                  ROUND(AVG(confidence), 3)  AS avg_confidence,
                  ROUND(AVG(llm_score),  3)  AS avg_llm_score,
                  ROUND(AVG(stylo_score),3)  AS avg_stylo_score
           FROM submissions
           GROUP BY attribution"""
    ).fetchall()

    conn.close()

    distribution = {}
    for row in attr_rows:
        distribution[row["attribution"]] = {
            "count": row["cnt"],
            "percentage": round(row["cnt"] / total * 100, 1) if total else 0,
        }

    calibration = {}
    for row in conf_rows:
        calibration[row["attribution"]] = {
            "avg_confidence":  row["avg_confidence"],
            "avg_llm_score":   row["avg_llm_score"],
            "avg_stylo_score": row["avg_stylo_score"],
        }

    return {
        "total_submissions": total,
        "attribution_distribution": distribution,
        "appeal_rate": {
            "total_appeals":   appeal_count,
            "rate_percentage": round(appeal_count / total * 100, 1) if total else 0,
        },
        "avg_confidence_by_attribution": calibration,
    }
