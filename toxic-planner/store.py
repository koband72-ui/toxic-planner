"""Transactional user documents; PostgreSQL on Render, SQLite for local tests only."""
import copy
import json
import os
import threading
from contextlib import contextmanager
from sqlalchemy import create_engine, text


def empty_state():
    return dict(tasks=[], goals=[], habits=[], ideas=[], focuses=[], events=[], messages=[],
                drafts={}, sent={}, settings=dict(tone='direct', quiet_start=22, quiet_end=8,
                daily_limit=8, enabled=True), energy={}, reviews={}, next_id=1)


class Store:
    def __init__(self, url=None):
        url = url or os.getenv('DATABASE_URL', 'sqlite:///planner-v2.db')
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql+psycopg://', 1)
        elif url.startswith('postgresql://'):
            url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
        if os.getenv('RENDER') and not url.startswith('postgresql+psycopg://'):
            raise RuntimeError('Render requires persistent PostgreSQL DATABASE_URL')
        self.pg = url.startswith('postgresql')
        self.lock = threading.RLock()
        self.engine = create_engine(url, pool_pre_ping=True)
        with self.engine.begin() as c:
            c.execute(text('CREATE TABLE IF NOT EXISTS planner_users (user_id BIGINT PRIMARY KEY, document TEXT NOT NULL)'))

    @contextmanager
    def transaction(self, uid):
        with self.lock, self.engine.begin() as c:
            c.execute(text('INSERT INTO planner_users (user_id, document) VALUES (:u, :d) ON CONFLICT (user_id) DO NOTHING'),
                      dict(u=uid, d=json.dumps(empty_state())))
            row = c.execute(text('SELECT document FROM planner_users WHERE user_id=:u' + (' FOR UPDATE' if self.pg else '')), dict(u=uid)).scalar_one()
            state = json.loads(row)
            yield state
            c.execute(text('UPDATE planner_users SET document=:d WHERE user_id=:u'),
                      dict(u=uid, d=json.dumps(state, ensure_ascii=False)))

    def read(self, uid):
        with self.transaction(uid) as s:
            return copy.deepcopy(s)

    def users(self):
        with self.engine.connect() as c:
            return list(c.execute(text('SELECT user_id FROM planner_users')).scalars())
