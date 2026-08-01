import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[2] / "data" / "GsData.db"
if not db.exists():
    print("no db")
    raise SystemExit(0)
c = sqlite3.connect(str(db))
r = c.execute("SELECT count(*) FROM aischeduledtask WHERE status='pending'").fetchone()
print("pending", r[0] if r else 0)
c.execute("UPDATE aischeduledtask SET status='cancelled' WHERE status='pending'")
c.commit()
print("cancelled")
c.close()
