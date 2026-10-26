import copy
import os
import tempfile
import unittest
from datetime import datetime,timedelta
from unittest.mock import AsyncMock,patch
from store import Store,empty_state
from domain import *
from assistant import Operation,Proposal
from bot import Planner,task_list

D=datetime(2026,10,26,10,tzinfo=KYIV)

class Rules(unittest.TestCase):
    def setUp(self): self.s=empty_state()
    def task(self,**kwargs): return add_task(self.s,'Монтаж',D,**kwargs)
    def test_owned_documents(self):
        with tempfile.TemporaryDirectory() as p:
            db=Store('sqlite:///'+p+'/test.db')
            with db.transaction(1) as s: add_task(s,'private',D)
            with db.transaction(2) as s:
                with self.assertRaises(ValueError): change_task(s,1,'done',D)
            self.assertEqual(db.read(1)['tasks'][0]['status'],'pending')
    def test_atomic_batch(self):
        before=copy.deepcopy(self.s)
        key=draft(self.s,[dict(action='add_task',title='One'),dict(action='add_task',title='Bad',priority=99)],D)
        with self.assertRaises(ValueError): confirm(self.s,key,D)
        self.assertEqual(self.s['tasks'],before['tasks'])
    def test_draft_rejects_stale_target(self):
        t=self.task()
        key=draft(self.s,[dict(action='done',id=t['id'])],D)
        change_task(self.s,t['id'],'edit',D+timedelta(seconds=1),title='Changed')
        with self.assertRaises(ValueError): confirm(self.s,key,D+timedelta(seconds=2))
        self.assertEqual(t['status'],'pending')
    def test_habit_counts_today(self):
        apply_operations(self.s,[dict(action='add_habit',title='English',target=5)],D)
        habit_check(self.s,1,D)
        self.assertEqual(metrics(self.s,week(D),D+timedelta(minutes=1))['habits'],1)
    def test_confirmation_once(self):
        key=draft(self.s,[dict(action='add_task',title='One')],D)
        confirm(self.s,key,D)
        with self.assertRaises(ValueError): confirm(self.s,key,D)
        self.assertEqual(len(self.s['tasks']),1)
    def test_done_and_undo(self):
        t=self.task(); change_task(self.s,t['id'],'done',D)
        self.assertEqual(t['completed'],iso(D))
        change_task(self.s,t['id'],'undo',D)
        self.assertIsNone(t['completed'])
    def test_deadline_not_rewritten(self):
        t=add_task(self.s,'old',D-timedelta(days=2),due=iso(D+timedelta(hours=1)))
        change_task(self.s,t['id'],'move',D,due=iso(D+timedelta(days=2)),reason='Не вистачило часу')
        change_task(self.s,t['id'],'done',D+timedelta(days=1))
        m=metrics(self.s,week(D),D+timedelta(days=3))
        self.assertEqual(m['planned'],1); self.assertEqual(m['ontime'],0)
    def test_new_deadline_not_retroactive_plan(self):
        t=add_task(self.s,'undated',D-timedelta(days=2))
        change_task(self.s,t['id'],'move',D,due=iso(D+timedelta(days=1)))
        self.assertEqual(metrics(self.s,week(D),D+timedelta(days=3))['planned'],0)
    def test_legacy_import_repeat(self):
        import sqlite3
        from scripts.import_legacy import import_file
        with tempfile.TemporaryDirectory() as p:
            path=p+'/old.db'
            c=sqlite3.connect(path)
            c.executescript("CREATE TABLE users (user_id INTEGER); INSERT INTO users VALUES (1); CREATE TABLE tasks (id INTEGER,user_id INTEGER,task_text TEXT,status TEXT,created_at TEXT); INSERT INTO tasks VALUES (1,1,'old','done','2026-10-01 10:00:00');")
            c.commit(); c.close()
            db=Store('sqlite:///'+p+'/new.db')
            import_file(path,db); import_file(path,db)
            s=db.read(1)
            self.assertEqual(len(s['tasks']),1)
            self.assertIsNone(s['tasks'][0]['completed'])
    def test_cancel_not_done(self):
        t=self.task(); change_task(self.s,t['id'],'cancel',D)
        m=metrics(self.s,week(D),D+timedelta(days=1))
        self.assertEqual(m['done'],0); self.assertEqual(m['cancelled'],1)
    def test_habit_daily_dedup(self):
        apply_operations(self.s,[dict(action='add_habit',title='English',target=5)],D)
        habit_check(self.s,1,D); habit_check(self.s,1,D)
        self.assertEqual(len(self.s['habits'][0]['checks']),1)
    def test_focus_does_not_complete_task(self):
        t=self.task(); focus(self.s,t['id'],25,D)
        notices=due_notices(self.s,D+timedelta(minutes=26))
        self.assertTrue(any(n[1]=='focus' for n in notices)); self.assertEqual(t['status'],'pending')
    def test_kyiv_8_and_quiet(self):
        at8=D.replace(hour=8)
        self.assertTrue(any(x[1]=='morning' for x in due_notices(self.s,at8)))
        self.assertEqual(due_notices(self.s,at8.replace(hour=7)),[])
        self.assertEqual(due_notices(self.s,at8.replace(hour=23)),[])
    def test_dst(self):
        # Kyiv's 08:00 resolves to different UTC hours across autumn DST.
        a=datetime(2026,10,24,8,tzinfo=KYIV); b=datetime(2026,10,26,8,tzinfo=KYIV)
        self.assertNotEqual(a.utcoffset(),b.utcoffset())
        for dt in (a,b): self.assertTrue(any(x[1]=='morning' for x in due_notices(self.s,dt)))
    def test_delivery_dedup(self):
        key=due_notices(self.s,D)[0][0]
        self.s['sent'][key]={'status':'sent'}
        self.assertNotIn(key,[x[0] for x in due_notices(self.s,D)])
    def test_old_report_unchanged(self):
        t=self.task(); end=D+timedelta(days=2)
        old=metrics(self.s,week(D),end)
        change_task(self.s,t['id'],'done',D+timedelta(days=8))
        self.assertEqual(metrics(self.s,week(D),end),old)
    def test_invalid_model_actions(self):
        for data in [dict(action='move',id=1),dict(action='add_task'),dict(action='done'),dict(action='add_task',title='x',due='2026-10-27')]:
            with self.assertRaises(ValueError): Operation.model_validate(data)
    def test_list_pagination(self):
        for i in range(20): self.task()
        text,keys=task_list(self.s)
        self.assertEqual(len(keys.inline_keyboard),7)
        self.assertNotIn('#7 ',text)
    def test_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as p:
            db=Store('sqlite:///'+p+'/test.db')
            db.read(1)
            with self.assertRaises(ValueError):
                with db.transaction(1) as s:
                    add_task(s,'rollback',D)
                    raise ValueError()
            self.assertEqual(db.read(1)['tasks'],[])

class Runtime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store('sqlite:///'+self.tmp.name+'/test.db')
        with patch.dict(os.environ,{'GEMINI_API_KEY':''}): self.p=Planner(self.store,AsyncMock())
    async def asyncTearDown(self):
        await self.p.ai.close(); self.store.engine.dispose(); self.tmp.cleanup()
    async def test_commands_without_ai(self):
        await self.p.command(1,'/add','Task')
        await self.p.command(1,'/habit','English | 5 | 15 хв')
        await self.p.command(1,'/goal','Channel')
        await self.p.command(1,'/idea','Story')
        s=self.store.read(1)
        self.assertEqual(len(s['tasks']),1)
        for collection in ('goals','habits','ideas'): self.assertEqual(len(s[collection]),1)
    async def test_plain_conversation_not_task(self):
        await self.p.propose(1,'Привіт')
        self.assertEqual(self.store.read(1)['tasks'],[])
    async def test_duplicate_delivery(self):
        await self.p.deliver(1,'test','morning',None)
        await self.p.deliver(1,'test','morning',None)
        self.assertEqual(self.p.bot.send_message.await_count,1)
    async def test_failure_retry_lease(self):
        self.p.bot.send_message.side_effect=RuntimeError('network')
        await self.p.deliver(1,'test','morning',None)
        self.assertEqual(self.store.read(1)['sent']['test']['status'],'sending')
    async def test_repeat_message_mutation(self):
        from bot import MUTATION_SCOPE
        for _ in range(2):
            token=MUTATION_SCOPE.set(('message:123',0))
            try: await self.p.command(1,'/add','Once')
            finally: MUTATION_SCOPE.reset(token)
        self.assertEqual(len(self.store.read(1)['tasks']),1)
    async def test_clear_confirmation(self):
        await self.p.command(1,'/add','x'); await self.p.command(1,'/clear','')
        self.assertEqual(self.store.read(1)['tasks'][0]['status'],'pending')

if __name__=='__main__': unittest.main()
