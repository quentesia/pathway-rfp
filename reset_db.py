"""Clear all tables except bls_cache. Preserves cached BLS API data."""

from app.db import init_db, SessionLocal, Base, ENGINE
from app.models import BLSCache

init_db()

KEEP_TABLES = {BLSCache.__tablename__}

session = SessionLocal()
try:
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in KEEP_TABLES:
            session.execute(table.delete())
            print(f"  Cleared {table.name}")
        else:
            count = session.execute(table.select()).rowcount
            print(f"  Kept {table.name} ({count} rows)")
    session.commit()
    print("\nDone. BLS cache preserved.")
finally:
    session.close()
