import asyncio
import contextlib
import contextvars
import copy
import hmac
import io
import logging
import os
from datetime import timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from store import Store
from assistant import Assistant
from domain import (now, iso, parse, week, KYIV, add_task, change_task, get, draft, confirm,
                    habit_check, focus, report, due_notices, stamp, new_id)

MUTATION_SCOPE=contextvars.ContextVar('mutation_scope',default=None)

HELP='''Твій планувальник
/tasks — активні задачі та керування
/add Назва | 2026-11-01T16:00:00+02:00 — додати (дата необов'язкова)
/now — що робити зараз
/focus — активна фокус-сесія
/goals — цілі; /goal Назва | дата — додати
/habits — звички; /habit Назва | 5 | мінімальний обсяг
/ideas — парковка; /idea Текст — записати
/weekly — чотири тижні результатів
/important ID — головний результат тижня (до трьох)
/energy 1–5 — сили цього тижня
/review Текст — підсумок дня
/stale — давні задачі
/settings — характер, тихі години, ліміт повідомлень
/tone calm або direct або hard
/quiet 22 8 — тихі години за Києвом
/limit 8 — ліміт додаткових нагадувань на день
/pause та /resume — вимкнути/увімкнути нагадування
/clear — скасувати активні задачі з підтвердженням
/split ID — запропонувати кроки
Можна писати людською мовою або голосом: перед змінами покажу підтвердження.
Ранковий план 08:00, підсумок 21:00, недільний звіт 20:00. Час — Київ.'''


def keyboard(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text=label,callback_data=data) for label,data in row] for row in rows])


def when(value):
    return parse(value).astimezone(KYIV).strftime('%d.%m %H:%M') if value else 'без дати'


def task_list(s,page=0):
    tasks=sorted([t for t in s['tasks'] if t['status']=='pending'],key=lambda t:(t['due'] or '9999',t['priority'],t['id']))
    page=max(0,min(page,max(0,(len(tasks)-1)//6)))
    lines=['Твої задачі']; rows=[]
    for t in tasks[page*6:page*6+6]:
        lines.append(f"#{t['id']} {t['title']}\n{when(t['due'])} · пріоритет {t['priority']}"+(f" · ~{t['minutes']} хв" if t['minutes'] else ''))
        rows.append([(f"Відкрити #{t['id']}",f"task:{t['id']}")])
    if not tasks:
        lines.append('Активних задач немає. /add — додати головну справу.')
    nav=[]
    if page: nav.append(('←',f'list:{page-1}'))
    if (page+1)*6<len(tasks): nav.append(('→',f'list:{page+1}'))
    if nav: rows.append(nav)
    return '\n\n'.join(lines),keyboard(rows)


def task_card(t):
    rows=[]
    if t['status']=='pending':
        rows=[[('✅ Зроблено',f"done:{t['id']}"),('⏰ Перенести',f"move:{t['id']}")],
              [('✏️ Змінити',f"edit:{t['id']}"),('🎯 Головна',f"important:{t['id']}")],
              [('Фокус 25 хв',f"focus:{t['id']}:25"),('50 хв',f"focus:{t['id']}:50")],
              [('Розбити на кроки',f"split:{t['id']}"),('Скасувати',f"cancel:{t['id']}")]]
    elif t['status']=='done':
        rows=[[('Скасувати завершення',f"undo:{t['id']}")]]
    rows.append([('До списку','list:0')])
    return f"#{t['id']} {t['title']}\nТермін: {when(t['due'])}\nСтан: {t['status']} · перенесень: {t['moves']}",keyboard(rows)


def morning(s,dt):
    day=dt.astimezone(KYIV).date()
    tasks=[t for t in s['tasks'] if t['status']=='pending']
    today=[t for t in tasks if t['due'] and parse(t['due']).astimezone(KYIV).date()==day]
    overdue=[t for t in tasks if t['due'] and parse(t['due']).astimezone(KYIV).date()<day]
    lines=[f'План на {day:%d.%m}', 'Сьогодні:']
    lines += [f"#{t['id']} {t['title']} · {when(t['due'])}" for t in today[:12]] or ['Датованих задач немає. Обери головну справу через /tasks.']
    if overdue:
        lines += ['\nПрострочені:']+[f"#{t['id']} {t['title']}" for t in overdue[:8]]
    return '\n'.join(lines)


class Planner:
    def __init__(self,store,bot):
        self.store=store; self.bot=bot; self.ai=Assistant(); self.dp=Dispatcher()
        self.tick_lock=asyncio.Lock()
        self.dp.message.register(self.message,F.chat.type=='private')
        self.dp.callback_query.register(self.callback)

    async def read(self,uid):
        return await asyncio.to_thread(self.store.read,uid)

    async def mutate(self,uid,fn):
        scope=MUTATION_SCOPE.get()
        key=None
        if scope:
            prefix,index=scope
            key=f'{prefix}:{index}'
            MUTATION_SCOPE.set((prefix,index+1))
        def run():
            with self.store.transaction(uid) as s:
                ledger=s.setdefault('mutations',{})
                if key and key in ledger:
                    return copy.deepcopy(ledger[key])
                result=fn(s)
                if key:
                    # fn can replace the document when confirming a batch.
                    ledger=s.setdefault('mutations',{})
                    ledger[key]=copy.deepcopy(result)
                    while len(ledger)>2000: ledger.pop(next(iter(ledger)))
                return copy.deepcopy(result)
        return await asyncio.to_thread(run)

    async def send(self,uid,text,markup=None):
        for i in range(0,len(text),3500):
            await self.bot.send_message(uid,text[i:i+3500],reply_markup=markup if i+3500>=len(text) else None)

    async def propose(self,uid,text,audio=None):
        state=await self.read(uid)
        p=await self.ai.propose(text,state,now(),audio)
        def remember(s):
            s['messages']=(s['messages']+[dict(role='user',text=text[:3000]),dict(role='assistant',text=p.reply)])[-20:]
            key=draft(s,[o.model_dump(exclude_none=True) for o in p.operations],now()) if p.operations else None
            return dict(key=key,proposal=p.model_dump())
        saved=await self.mutate(uid,remember)
        from assistant import Proposal
        p=Proposal.model_validate(saved['proposal'])
        key=saved['key']
        summary=[]
        for op in p.operations:
            summary.append(f"{op.action}: "+(f'#{op.id} ' if op.id else '')+(op.title or '')+(f' · {when(op.due)}' if op.due else '')+(f' · {op.target} разів/тиждень' if op.target else ''))
        await self.send(uid,p.reply+ ('\n\nПропоновані зміни:\n'+'\n'.join(summary) if key else ''),
                        keyboard([[('Підтвердити',f'confirm:{key}'),('Відхилити',f'reject:{key}')]]) if key else None)

    async def message(self,m):
        uid=m.from_user.id
        token=MUTATION_SCOPE.set((f'message:{m.message_id}',0))
        try:
            if m.voice:
                if m.voice.duration>300 or (m.voice.file_size or 0)>15*1024*1024:
                    await self.send(uid,'Надішли голосове до 5 хвилин і 15 МБ.'); return
                stream=io.BytesIO()
                await self.bot.download(m.voice,destination=stream)
                await self.propose(uid,'Розпізнай голосове, покажи почуте й запропонуй задачі для підтвердження.',stream.getvalue()); return
            if not m.text: return
            text=m.text.strip(); cmd,_,arg=text.partition(' '); cmd=cmd.split('@')[0]
            if text.startswith('/'):
                await self.command(uid,cmd,arg.strip()); return
            s=await self.read(uid)
            pending=s.get('input')
            if pending and parse(pending['expires'])>now():
                await self.handle_input(uid,pending,text); return
            await self.propose(uid,text)
        except (ValueError,KeyError,IndexError) as exc:
            await self.send(uid,'Не змінено: '+str(exc)[:300])
        except Exception as exc:
            logging.error('Message failed: %s',type(exc).__name__)
            await self.send(uid,'Не вдалося виконати дію. Спробуй ще раз. /tasks покаже збережений стан.')

        finally:
            MUTATION_SCOPE.reset(token)

    async def command(self,uid,cmd,arg):
        s=await self.read(uid); dt=now()
        if cmd in ('/start','/help'): await self.send(uid,HELP)
        elif cmd=='/tasks': await self.send(uid,*task_list(s))
        elif cmd=='/add':
            title,_,due=arg.partition('|')
            t=await self.mutate(uid,lambda s:add_task(s,title,dt,due.strip() or None))
            await self.send(uid,*task_card(t))
        elif cmd in ('/goal','/idea','/habit'):
            parts=[x.strip() for x in arg.split('|')]
            if not parts[0]: raise ValueError('Вкажи назву після команди')
            op=dict(action={'/goal':'add_goal','/idea':'add_idea','/habit':'add_habit'}[cmd],title=parts[0])
            if cmd=='/goal' and len(parts)>1: op['due']=parts[1]
            if cmd=='/habit':
                op.update(target=int(parts[1]) if len(parts)>1 else 5,minimum=parts[2] if len(parts)>2 else '1 повторення')
            from assistant import Operation
            op=Operation.model_validate(op).model_dump(exclude_none=True)
            from domain import apply_operations
            result=await self.mutate(uid,lambda s:apply_operations(s,[op],dt))
            await self.send(uid,result)
        elif cmd in ('/goals','/ideas','/habits'):
            collection=cmd[1:]; rows=[]; lines=[]
            for item in s[collection]:
                if not item.get('active',True): continue
                i=item['id']; line=f"#{i} {item['title']}"
                if collection=='habits':
                    count=sum(d>=week(dt).date().isoformat() for d in item['checks'])
                    line+=f" · {count}/{item['target']} цього тижня · мінімум {item['minimum']}"
                    rows.append([(f'Виконано #{i}',f'habit:{i}')])
                elif collection=='ideas': rows.append([(f'У задачу #{i}',f'idea:{i}')])
                else:
                    ts=[t for t in s['tasks'] if t.get('goal_id')==i]
                    line+=f" · {sum(t['status']=='done' for t in ts)}/{len(ts)} кроків · {when(item.get('due'))}"
                    rows.append([(f'Запропонувати кроки #{i}',f'goal:{i}')])
                lines.append(line)
            # Each page is an independent message, kept below Telegram limits.
            for start in range(0,max(1,len(lines)),8):
                await self.send(uid,'\n'.join(lines[start:start+8]) or 'Список порожній.',keyboard(rows[start:start+8]))
        elif cmd=='/weekly': await self.send(uid,report(s,dt))
        elif cmd=='/energy':
            value=int(arg)
            if not 1<=value<=5: raise ValueError('Оцінка 1–5')
            await self.mutate(uid,lambda s:s['energy'].update({week(dt).date().isoformat():value}))
            await self.send(uid,'Оцінку сил збережено окремо від результатів.')
        elif cmd=='/review':
            if not arg: await self.send(uid,'Що завершено? Що завадило? Що головне завтра? Напиши /review і відповідь.'); return
            await self.mutate(uid,lambda s:s['reviews'].update({dt.astimezone(KYIV).date().isoformat():arg[:4000]}))
            await self.send(uid,'Підсумок збережено. Перевірю, чи потрібні зміни у задачах.')
            await self.propose(uid,'Підсумок дня: '+arg)
        elif cmd=='/important':
            t=await self.mutate(uid,lambda s:change_task(s,int(arg),'important',dt))
            await self.send(uid,'Головний результат тижня: '+t['title'])
        elif cmd=='/split':
            t=get(s,'tasks',int(arg)); await self.propose(uid,f"Розбий мою задачу #{t['id']} на конкретні маленькі кроки. Запропонуй заміну для підтвердження.")
        elif cmd=='/now': await self.recommend(uid,s,int(arg) if arg else 0)
        elif cmd=='/focus':
            active=[f for f in s['focuses'] if f['status'] in ('running','awaiting')]
            for f in active: await self.send(uid,f"Фокус #{f['id']} · до {when(f['ends'])}",keyboard([[('Завершив',f"finish:{f['id']}"),('Ще 15 хв',f"extend:{f['id']}"),('Застряг',f"stuck:{f['id']}")],[('Зупинити',f"stopfocus:{f['id']}")]]))
            if not active: await self.send(uid,'Відкрий задачу через /tasks і натисни «Фокус 25 хв».')
        elif cmd=='/stale': await self.stale(uid,s,dt)
        elif cmd=='/clear':
            key=await self.mutate(uid,lambda s:draft(s,[dict(action='cancel',id=t['id']) for t in s['tasks'] if t['status']=='pending'],dt))
            await self.send(uid,'Скасувати всі поточні активні задачі? Історія залишиться.',keyboard([[('Так, скасувати',f'confirm:{key}'),('Ні',f'reject:{key}')]]))
        elif cmd=='/settings': await self.send(uid,f"Стиль: {s['settings']['tone']}\nТихі години: {s['settings']['quiet_start']}–{s['settings']['quiet_end']}\nЛіміт додаткових нагадувань: {s['settings']['daily_limit']}\nУвімкнено: {s['settings']['enabled']}\n/tone calm|direct|hard · /quiet 22 8 · /limit 8 · /pause · /resume")
        elif cmd=='/tone':
            if arg not in ('calm','direct','hard'): raise ValueError('Стиль: calm, direct, hard')
            await self.mutate(uid,lambda s:s['settings'].update(tone=arg)); await self.send(uid,'Стиль змінено.')
        elif cmd=='/quiet':
            a,b=map(int,arg.split())
            if not 0<=a<=23 or not 0<=b<=23: raise ValueError('Години: 0–23')
            await self.mutate(uid,lambda s:s['settings'].update(quiet_start=a,quiet_end=b)); await self.send(uid,'Тихі години збережено. Вони також можуть відкласти ранковий або вечірній звіт.')
        elif cmd=='/limit':
            limit=int(arg)
            if not 1<=limit<=30: raise ValueError('Ліміт: 1–30')
            await self.mutate(uid,lambda s:s['settings'].update(daily_limit=limit)); await self.send(uid,'Ліміт збережено.')
        elif cmd in ('/pause','/resume'):
            await self.mutate(uid,lambda s:s['settings'].update(enabled=cmd=='/resume')); await self.send(uid,'Нагадування '+('увімкнено' if cmd=='/resume' else 'вимкнено'))
        else: await self.send(uid,'Невідома команда. /help — список.')

    async def recommend(self,uid,s,skip=0,minutes=None):
        tasks=[t for t in s['tasks'] if t['status']=='pending']
        tasks.sort(key=lambda t:(t['due'] or '9999',t['priority'],not t['important']))
        if not tasks: await self.send(uid,'Активних задач немає. /add — додати.'); return
        t=tasks[skip%len(tasks)]
        if minutes==5:
            await self.propose(uid,f"Маю лише 5 хвилин і мало сил. Запропонуй один конкретний маленький крок для #{t['id']}, не змінюй задачу без прохання."); return
        await self.send(uid,f"Зараз: #{t['id']} {t['title']}\nТермін: {when(t['due'])}",keyboard([[('Почати',f"focus:{t['id']}:25"),('Інша',f'next:{skip+1}'),('Маю 5 хв',f'five:{skip}')]]))

    async def stale(self,uid,s,dt):
        tasks=[t for t in s['tasks'] if t['status']=='pending' and parse(t['updated'])<dt-timedelta(days=30)]
        for t in tasks[:20]:
            await self.send(uid,f"Давно без руху: #{t['id']} {t['title']}",keyboard([[('Залишити: новий термін',f"move:{t['id']}"),('Переробити',f"split:{t['id']}"),('Скасувати',f"cancel:{t['id']}")]]))
        if not tasks: await self.send(uid,'Немає задач без руху понад 30 днів.')
        elif len(tasks)>20: await self.send(uid,'Показано перші 20. Після перегляду повтори /stale.')

    async def handle_input(self,uid,pending,text):
        kind=pending['kind']; ident=pending['id']
        if text.lower() in ('скасувати','cancel'):
            await self.mutate(uid,lambda s:s.pop('input',None)); await self.send(uid,'Ввід скасовано.'); return
        if kind=='edit':
            if len(text)>500: raise ValueError('Назва до 500 символів')
            op=dict(action='edit',id=ident,title=text)
        elif kind=='move':
            try:
                parse(text)
                op=dict(action='move',id=ident,due=text,reason=pending.get('reason','Не вказано'))
            except ValueError:
                await self.propose(uid,f"Перенеси задачу #{ident}. Новий термін: {text}. Причина: {pending.get('reason','Не вказано')}")
                await self.mutate(uid,lambda s:s.pop('input',None)); return
        else:
            await self.propose(uid,f"Застряг на задачі #{ident}. Перешкода: {text}. Допоможи з конкретним першим кроком.")
            await self.mutate(uid,lambda s:s.pop('input',None)); return
        def save(s):
            s.pop('input',None)
            return draft(s,[op],now())
        key=await self.mutate(uid,save)
        await self.send(uid,'Підтвердити: '+(when(op['due']) if kind=='move' else text),keyboard([[('Так',f'confirm:{key}'),('Ні',f'reject:{key}')]]))

    async def callback(self,c):
        if not c.message or c.message.chat.type!='private' or c.message.chat.id!=c.from_user.id:
            await c.answer('Лише у власному приватному чаті.',show_alert=True); return
        await c.answer()
        uid=c.from_user.id; dt=now()
        try:
            parts=c.data.split(':'); action=parts[0]; value=parts[1] if len(parts)>1 else ''
            s=await self.read(uid)
            if action=='confirm':
                result=await self.mutate(uid,lambda s:confirm(s,value,dt))
                await self.send(uid,result or 'Список вже порожній.')
                await self.send(uid,*task_list(await self.read(uid)))
            elif action=='reject':
                await self.mutate(uid,lambda s:s['drafts'].pop(value,None)); await self.send(uid,'Зміни відхилено.')
            elif action=='list':
                text,markup=task_list(s,int(value))
                try: await c.message.edit_text(text,reply_markup=markup)
                except TelegramBadRequest: await self.send(uid,text,markup)
            elif action=='task': await self.send(uid,*task_card(get(s,'tasks',int(value))))
            elif action in ('done','undo','important'):
                t=await self.mutate(uid,lambda s:change_task(s,int(value),action,dt))
                await self.send(uid,*task_card(t))
                await self.send(uid,*task_list(await self.read(uid)))
            elif action=='cancel':
                get(s,'tasks',int(value))
                key=await self.mutate(uid,lambda s:draft(s,[dict(action='cancel',id=int(value))],dt))
                await self.send(uid,'Скасувати задачу зі збереженням історії?',keyboard([[('Так',f'confirm:{key}'),('Ні',f'reject:{key}')]]))
            elif action=='move':
                get(s,'tasks',int(value))
                await self.send(uid,'Що завадило?',keyboard([[(r,f'reason:{value}:{i}')] for i,r in enumerate(['Не вистачило часу','Не знаю, як зробити','Чекаю на когось','Втратило сенс','Інше'])]))
            elif action=='reason':
                reasons=['Не вистачило часу','Не знаю, як зробити','Чекаю на когось','Втратило сенс','Інше']; reason=reasons[int(parts[2])]
                t=get(s,'tasks',int(value))
                await self.mutate(uid,lambda s:s.update(input=dict(kind='move',id=int(value),reason=reason,expires=iso(dt+timedelta(hours=1)))))
                tips=['Обери реальний новий термін.','Можна розбити на кроки через /split '+value+'.','Обери час, коли перевіриш відповідь.','Можна скасувати через картку задачі.','']
                await self.send(uid,tips[int(parts[2])]+' Напиши нову дату й час (наприклад, завтра о 16:00), або «скасувати».'+(' Ця задача вже переносилася щонайменше тричі.' if t['moves']>=3 else ''))
            elif action in ('edit','stuck'):
                ident=int(value)
                if action=='stuck':
                    f=get(s,'focuses',ident); ident=f['task_id']
                    await self.mutate(uid,lambda s:get(s,'focuses',int(value)).update(status='stuck'))
                else: get(s,'tasks',ident)
                await self.mutate(uid,lambda s:s.update(input=dict(kind=action,id=ident,expires=iso(dt+timedelta(hours=1)))))
                await self.send(uid,'Напиши '+('нову назву' if action=='edit' else 'що конкретно заважає')+' або «скасувати».')
            elif action=='split': await self.command(uid,'/split',value)
            elif action=='focus':
                f=await self.mutate(uid,lambda s:focus(s,int(value),int(parts[2]),dt))
                await self.send(uid,f"Фокус до {when(f['ends'])}. /focus — керування. Таймер не завершує задачу автоматично.")
            elif action in ('finish','extend','stopfocus'):
                def update(s):
                    f=get(s,'focuses',int(value))
                    if f['status'] not in ('running','awaiting','stuck'): raise ValueError('Сесію вже завершено')
                    if action=='extend':
                        if any(x['id']!=f['id'] and x['status']=='running' for x in s['focuses']): raise ValueError('Інша сесія вже працює')
                        f.update(ends=iso(dt+timedelta(minutes=15)),status='running')
                    else:
                        f['status']='finished' if action=='finish' else 'stopped'
                        f['finished_at']=iso(dt)
                    return f
                f=await self.mutate(uid,update)
                await self.send(uid,'Сесію оновлено. '+('Якщо вся задача готова, познач її окремо.' if action=='finish' else ''),keyboard([[('Задача готова',f"done:{f['task_id']}")]]) if action=='finish' else None)
            elif action in ('next','five'): await self.recommend(uid,s,int(value),5 if action=='five' else None)
            elif action=='habit':
                h=await self.mutate(uid,lambda s:habit_check(s,int(value),dt)); await self.send(uid,'Відмічено за сьогодні: '+h['title'])
            elif action=='idea':
                idea=get(s,'ideas',int(value))
                key=await self.mutate(uid,lambda s:draft(s,[dict(action='add_task',title=idea['title'])],dt))
                await self.send(uid,'Створити задачу з ідеї? Оригінал ідеї залишиться.',keyboard([[('Створити',f'confirm:{key}'),('Ні',f'reject:{key}')]]))
            elif action=='goal':
                g=get(s,'goals',int(value)); await self.propose(uid,f"Запропонуй конкретні задачі для цілі #{g['id']} «{g['title']}» з goal_id={g['id']}. Дати не вигадуй.")
        except (ValueError,KeyError,IndexError) as exc: await self.send(uid,'Не змінено: '+str(exc)[:300])
        except Exception as exc:
            logging.error('Callback failed: %s',type(exc).__name__)
            await self.send(uid,'Помилка. Перевір збережений стан через /tasks.')

    async def tick(self):
        if self.tick_lock.locked(): return
        async with self.tick_lock:
            for uid in await asyncio.to_thread(self.store.users):
                try:
                    state=await self.read(uid)
                    for key,kind,ident in due_notices(state,now()):
                        await self.deliver(uid,key,kind,ident)
                except Exception as exc: logging.error('Scheduler user failed: %s',type(exc).__name__)

    async def deliver(self,uid,key,kind,ident):
        dt=now(); day=dt.astimezone(KYIV).date().isoformat()
        def claim(s):
            old=s['sent'].get(key,{})
            if old.get('status')=='sent' or (old.get('lease') and parse(old['lease'])>dt): return False
            routine=kind in ('morning','evening','weekly','plan','monthly')
            used=sum(v.get('day')==day and v.get('status')=='sent' and not v.get('routine') for v in s['sent'].values())
            if not routine and used>=s['settings']['daily_limit']: return False
            s['sent'][key]=dict(status='sending',lease=iso(dt+timedelta(minutes=5)),day=day,routine=routine)
            return True
        if not await self.mutate(uid,claim): return
        try:
            s=await self.read(uid); markup=None
            if kind=='morning': text=morning(s,dt); markup=keyboard([[('Задачі','list:0'),('Що зараз?','next:0')]])
            elif kind=='evening':
                count=sum(t['status']=='done' and t['completed'] and parse(t['completed']).astimezone(KYIV).date()==dt.astimezone(KYIV).date() for t in s['tasks'])
                text=f'Підсумок дня: завершено {count} задач.\nЩо завадило? Що головне завтра? Напиши /review і відповідь.'
            elif kind=='weekly': text=report(s,dt)+'\n\nПереглянь парковку ідей: /ideas'
            elif kind=='plan': text='Обери 1–3 головні результати тижня: /goals покаже цілі, /tasks — задачі, /important ID позначить результат.'
            elif kind=='monthly':
                count=sum(t['status']=='pending' and parse(t['updated'])<dt-timedelta(days=30) for t in s['tasks'])
                text=f'Щомісячний перегляд: {count} задач без руху понад 30 днів. /stale — залишити, переробити або скасувати.'
            elif kind=='task':
                t=get(s,'tasks',ident)
                if t['status']!='pending': return
                text='Термін задачі настав.\n'+task_card(t)[0]; markup=task_card(t)[1]
            else:
                f=get(s,'focuses',ident)
                text='Фокус-сесія закінчилася. Який результат?'
                markup=keyboard([[('Завершив',f"finish:{ident}"),('Ще 15 хв',f"extend:{ident}"),('Застряг',f"stuck:{ident}")]])
            await self.send(uid,text,markup)
            def complete(s):
                s['sent'][key].update(status='sent',lease=None)
                if kind=='focus' and get(s,'focuses',ident)['status']=='running': get(s,'focuses',ident)['status']='awaiting'
            await self.mutate(uid,complete)
        except TelegramForbiddenError:
            await self.mutate(uid,lambda s:s['settings'].update(enabled=False))
        except TelegramRetryAfter as exc:
            await self.mutate(uid,lambda s:s['sent'][key].update(lease=iso(dt+timedelta(seconds=exc.retry_after))))
        except Exception as exc:
            # Lease expires; retry next tick. A crash after Telegram accepts can duplicate a message.
            logging.warning('Notification delivery failed: %s',type(exc).__name__)


async def main():
    logging.basicConfig(level=logging.INFO)
    # HTTP debug logs must never expose Telegram token URLs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    bot=Bot(os.environ['BOT_TOKEN']); planner=Planner(Store(),bot)
    public=os.getenv('WEBHOOK_BASE_URL',os.getenv('RENDER_EXTERNAL_URL','')).rstrip('/')
    webhook_secret=os.getenv('WEBHOOK_SECRET',''); cron_secret=os.getenv('CRON_SECRET','')
    if os.getenv('RENDER') and (not public or len(webhook_secret)<32 or len(cron_secret)<32):
        raise RuntimeError('Render needs WEBHOOK_BASE_URL, WEBHOOK_SECRET and CRON_SECRET (32+ chars)')
    app=web.Application(client_max_size=1024*1024)
    async def health(request):
        await asyncio.to_thread(planner.store.users)
        return web.json_response({'status':'ok'})
    async def tick(request):
        if not cron_secret or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+cron_secret):
            raise web.HTTPUnauthorized()
        await planner.tick()
        return web.json_response({'ok':True})
    async def webhook(request):
        if not webhook_secret or not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),webhook_secret):
            raise web.HTTPUnauthorized()
        update=types.Update.model_validate(await request.json(),context={'bot':bot})
        # Wait for processing before acknowledgement; no fire-and-forget work on sleeping Free hosts.
        await planner.dp.feed_update(bot,update)
        return web.json_response({'ok':True})
    app.router.add_get('/',health); app.router.add_get('/healthz',health)
    app.router.add_post('/cron/tick',tick); app.router.add_post('/telegram/webhook',webhook)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,'0.0.0.0',int(os.getenv('PORT','10000'))).start()
    async def loop():
        while True:
            try: await planner.tick()
            except Exception as exc: logging.error('Tick failed: %s',type(exc).__name__)
            await asyncio.sleep(30)
    worker=asyncio.create_task(loop())
    try:
        if public:
            await bot.set_webhook(public+'/telegram/webhook',secret_token=webhook_secret,allowed_updates=['message','callback_query'],drop_pending_updates=False)
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            await planner.dp.start_polling(bot)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError): await worker
        await runner.cleanup(); await planner.ai.close(); await bot.session.close()
        planner.store.engine.dispose()


if __name__=='__main__': asyncio.run(main())
