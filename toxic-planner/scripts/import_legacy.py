"""Run on a secured host with a COPY of old planner.db and DATABASE_URL set."""
import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from store import Store
from domain import add_task,stamp,now,parse


def import_file(path,store):
    marker=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    conn=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        users={r[0] for r in conn.execute('SELECT user_id FROM users')}
        tasks=[dict(r) for r in conn.execute('SELECT * FROM tasks')]
        users.update(t['user_id'] for t in tasks)
        for uid in users:
            with store.transaction(uid) as s:
                if marker in s.get('legacy_imports',[]): continue
                for row in tasks:
                    if row['user_id']!=uid: continue
                    created=row.get('created_at')
                    try: dt=parse(created.replace(' ','T')+'+00:00')
                    except (ValueError,AttributeError): dt=now()
                    t=add_task(s,row['task_text'],dt)
                    if row['status']=='done':
                        # Legacy has no completed_at; never invent a completion date.
                        t.update(status='done',completed=None)
                        stamp(s,'task',t,now())
                s.setdefault('legacy_imports',[]).append(marker)
    finally: conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('sqlite_copy')
    import_file(parser.parse_args().sqlite_copy,Store())
    print('Import complete. Legacy completion dates remain unknown.')
