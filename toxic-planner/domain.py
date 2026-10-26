"""Pure planner rules. All dates are timezone-aware; history is append-only."""
import copy
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KYIV = ZoneInfo('Europe/Kyiv')
UTC = timezone.utc


def now():
    return datetime.now(UTC)


def iso(dt):
    return dt.astimezone(UTC).isoformat()


def parse(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Дата повинна містити часовий пояс')
    return dt.astimezone(UTC)


def week(dt):
    d = dt.astimezone(KYIV)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def stamp(s, kind, item, dt):
    s['events'].append(dict(at=iso(dt), kind=kind, item=copy.deepcopy(item)))


def new_id(s):
    result = s['next_id']
    s['next_id'] += 1
    return result


def get(s, collection, ident):
    item = next((x for x in s[collection] if x['id'] == int(ident)), None)
    if item is None:
        raise ValueError('Запис не знайдено у твоєму списку')
    return item


def add_task(s, title, dt, due=None, priority=2, minutes=None, area='Особисте', goal_id=None, important=False):
    title = title.strip()
    if not title or len(title) > 500:
        raise ValueError('Назва: від 1 до 500 символів')
    if priority not in (1, 2, 3):
        raise ValueError('Пріоритет: 1, 2 або 3')
    if minutes is not None and not 1 <= minutes <= 1440:
        raise ValueError('Тривалість: 1–1440 хв')
    if goal_id is not None:
        get(s, 'goals', goal_id)
    due = iso(parse(due)) if due else None
    t = dict(id=new_id(s), title=title, created=iso(dt), updated=iso(dt), due=due,
             original_due=due, priority=priority, minutes=minutes, area=area[:80], goal_id=goal_id,
             status='pending', completed=None, moves=0, important=important,
             result_week=week(dt).date().isoformat() if important else None)
    s['tasks'].append(t)
    stamp(s, 'task', t, dt)
    return t


def change_task(s, ident, action, dt, **fields):
    t = get(s, 'tasks', ident)
    if action == 'done':
        if t['status'] != 'pending':
            return t
        t.update(status='done', completed=iso(dt))
    elif action == 'undo':
        if t['status'] != 'done':
            return t
        t.update(status='pending', completed=None)
    elif action == 'cancel':
        t.update(status='cancelled', completed=None)
    elif action == 'move':
        due = iso(parse(fields['due']))
        if t['status'] != 'pending':
            raise ValueError('Переносити можна лише активну задачу')
        if t['due'] != due:
            t['moves'] += 1
        if t['original_due'] is None: t['original_due']=due
        t.update(due=due, reason=fields.get('reason', 'Не вказано'))
    elif action == 'edit':
        for k in ('title', 'priority', 'minutes', 'area', 'goal_id'):
            if k in fields and fields[k] is not None:
                if k == 'goal_id':
                    get(s, 'goals', fields[k])
                t[k] = fields[k]
    elif action == 'important':
        key = week(dt).date().isoformat()
        if sum(x.get('result_week') == key and x['important'] for x in s['tasks']) >= 3 and not t['important']:
            raise ValueError('На тиждень обери максимум три головні результати')
        t.update(important=True, result_week=key)
    else:
        raise ValueError('Невідома дія')
    t['updated'] = iso(dt)
    stamp(s, 'task', t, dt)
    return t


def draft(s, operations, dt):
    key = uuid.uuid4().hex[:12]
    s['drafts'] = {k:v for k,v in s['drafts'].items() if parse(v['expires']) > dt}
    versions={str(op['id']):get(s,'tasks',op['id'])['updated'] for op in operations if op.get('id') is not None}
    s['drafts'][key] = dict(operations=operations, versions=versions, expires=iso(dt + timedelta(hours=1)))
    return key


def apply_operations(s, operations, dt):
    result = []
    for op in operations:
        action = op['action']
        if action == 'add_task':
            t = add_task(s, op['title'], dt, **{k:op[k] for k in ('due','priority','minutes','area','goal_id') if op.get(k) is not None})
            result.append(f"Додано #{t['id']}: {t['title']}")
        elif action in ('done','cancel','move','edit'):
            fields = {k:op[k] for k in ('due','title','priority','minutes','area','goal_id','reason') if op.get(k) is not None}
            t = change_task(s, op['id'], action, dt, **fields)
            result.append(f"Оновлено #{t['id']}: {t['title']}")
        elif action in ('add_goal','add_idea','add_habit'):
            collection = {'add_goal':'goals','add_idea':'ideas','add_habit':'habits'}[action]
            item = dict(id=new_id(s), title=op['title'], created=iso(dt), active=True)
            if action == 'add_goal':
                item['due'] = iso(parse(op['due'])) if op.get('due') else None
            if action == 'add_habit':
                item.update(target=op.get('target') or 5, minimum=op.get('minimum') or '1 повторення', checks=[])
            s[collection].append(item)
            stamp(s, collection, item, dt)
            result.append(f"Збережено #{item['id']}: {item['title']}")
        else:
            raise ValueError('Непідтримувана дія')
    return '\n'.join(result)


def confirm(s, key, dt):
    pending = s['drafts'].get(key)
    if not pending or parse(pending['expires']) <= dt:
        raise ValueError('Підтвердження вже використане або застаріло')
    for ident,updated in pending.get('versions',{}).items():
        if get(s,'tasks',int(ident))['updated'] != updated:
            raise ValueError('Задача змінилась після пропозиції. Повтори запит.')
    # Validate the entire batch before modifying the original document.
    candidate = copy.deepcopy(s)
    result = apply_operations(candidate, pending['operations'], dt)
    del candidate['drafts'][key]
    s.clear()
    s.update(candidate)
    return result


def habit_check(s, ident, dt):
    h = get(s, 'habits', ident)
    day = dt.astimezone(KYIV).date().isoformat()
    if day not in h['checks']:
        h['checks'].append(day)
        stamp(s, 'habits', h, dt)
    return h


def focus(s, task_id, minutes, dt):
    if minutes not in (15, 25, 50):
        raise ValueError('Доступні 15, 25 або 50 хв')
    t = get(s, 'tasks', task_id)
    if t['status'] != 'pending':
        raise ValueError('Задача вже не активна')
    if any(f['status'] == 'running' for f in s['focuses']):
        raise ValueError('Вже є активна сесія. Заверши або зупини її через /focus')
    f = dict(id=new_id(s), task_id=task_id, started=iso(dt), ends=iso(dt+timedelta(minutes=minutes)), status='running')
    s['focuses'].append(f)
    return f


def task_versions(s, end):
    latest = {}
    for e in s['events']:
        if e['kind'] == 'task' and parse(e['at']) <= end:
            latest[e['item']['id']] = e['item']
    return list(latest.values())


def metrics(s, start, end):
    tasks = task_versions(s, end)
    baseline=task_versions(s,start-timedelta(microseconds=1))
    planned_ids={t['id'] for t in baseline if t['status']=='pending' and t['original_due'] and start <= parse(t['original_due']) < start+timedelta(weeks=1)}
    planned = [t for t in tasks if t['id'] in planned_ids]
    added = [t for t in tasks if start <= parse(t['created']) < end]
    done = [t for t in tasks if t['status']=='done' and t['completed'] and start <= parse(t['completed']) < end]
    ontime = [t for t in planned if t['status']=='done' and t['completed'] and parse(t['completed']) <= parse(t['original_due'])]
    key = start.date().isoformat()
    main = [t for t in tasks if t.get('result_week') == key]
    # Habit target as of start, or creation for a newly introduced habit.
    habits = {}
    targets = {}
    for e in s['events']:
        if e['kind'] == 'habits' and parse(e['at']) < end:
            h = e['item']; habits[h['id']] = h
            if h['id'] not in targets or parse(e['at']) <= start:
                targets[h['id']] = h['target']
    last_day=end.astimezone(KYIV)
    day_end=(last_day.date()+timedelta(days=1)).isoformat() if last_day.time().isoformat()!='00:00:00' else last_day.date().isoformat()
    checks = sum(min(targets[i], sum(start.date().isoformat() <= d < day_end for d in h['checks'])) for i,h in habits.items())
    reasons = [e['item'].get('reason','Не вказано') for e in s['events'] if e['kind']=='task' and start <= parse(e['at']) < end and e['item'].get('reason')]
    areas = {}
    for t in done:
        areas[t['area']] = areas.get(t['area'],0)+1
    return dict(planned=len(planned), ontime=len(ontime), added=len(added), done=len(done),
                cancelled=sum(t['status']=='cancelled' for t in tasks if start <= parse(t['updated']) < end),
                main=len(main), main_done=sum(t['status']=='done' for t in main),
                habits=checks, habit_target=sum(targets.values()),
                overdue=sum(t['status']=='pending' and t['due'] is not None and parse(t['due']) < end for t in tasks),
                moved=sum(t.get('moves',0)>0 and start <= parse(t['updated']) < end for t in tasks),
                focuses=sum(start <= parse(f.get('finished_at',f['ends'])) < end and f['status']=='finished' for f in s['focuses']),
                areas=areas, energy=s['energy'].get(key), reasons=reasons[-5:])


def report(s, dt):
    start = week(dt)
    lines = ['Тижневий звіт: поточний тиждень станом на зараз.']
    for offset in range(4):
        a = start-timedelta(weeks=offset)
        b = dt if offset==0 else a+timedelta(weeks=1)
        m = metrics(s,a,b)
        if not any(parse(e['at']) < b for e in s['events']):
            lines.append(f'{a:%d.%m}: немає даних для порівняння')
            continue
        rate = f"{round(100*m['ontime']/m['planned'])}%" if m['planned'] else 'немає початкового плану'
        lines.append(f"\n{a:%d.%m}: головні {m['main_done']}/{m['main']}; вчасно {m['ontime']}/{m['planned']} ({rate}); звички {m['habits']}/{m['habit_target']}; прострочені {m['overdue']}.\nДодано {m['added']}; завершено {m['done']}; скасовано {m['cancelled']}; задач із перенесенням {m['moved']}; фокус-сесій {m['focuses']}. Енергія: {m['energy'] or 'не вказано'}/5.")
        if m['areas']:
            lines.append('Напрями: '+', '.join(f'{k}: {v}' for k,v in m['areas'].items()))
    prev_start=start-timedelta(weeks=1)
    if any(parse(e['at'])<start for e in s['events']):
        current=metrics(s,start,dt); previous=metrics(s,prev_start,start)
        lines.append('\nПорівняння з минулим тижнем (поточний ще триває):')
        for name,key in [('Головні результати','main_done'),('Повтори звичок','habits'),('Прострочені','overdue')]:
            delta=current[key]-previous[key]
            lines.append(f'{name}: {delta:+d}.')
    stuck = [t for t in s['tasks'] if t['status']=='pending' and t['moves']>=3]
    for t in stuck[:3]:
        lines.append(f"Буксує #{t['id']}: {t['title']}. Перенесень: {t['moves']}. Причина: {t.get('reason','не вказано')}. /split {t['id']} — розбити на кроки.")
    lines.append('Початковий план — задачі, що існували на початку тижня, з початковим дедлайном цього тижня. Перенесення не змінює цей дедлайн. Неповний тиждень не означає спад. /energy 1–5 — оцінити сили.')
    return '\n'.join(lines)


def quiet(settings, dt):
    h = dt.astimezone(KYIV).hour
    a,b = settings['quiet_start'],settings['quiet_end']
    return (a <= h < b) if a < b else (h >= a or h < b) if a != b else False


def due_notices(s, dt):
    """Deterministic keys prevent routine duplicates; callers persist delivery leases."""
    if not s['settings']['enabled'] or quiet(s['settings'],dt):
        return []
    local=dt.astimezone(KYIV); day=local.date().isoformat(); out=[]
    if local.hour >= 8 and local.hour < 21:
        out.append((f'morning:{day}', 'morning', None))
    if local.hour >=21:
        out.append((f'evening:{day}', 'evening', None))
    if local.weekday()==6 and local.hour>=20:
        out.append((f'weekly:{day}', 'weekly', None))
    if local.weekday()==0 and local.hour>=8:
        out.append((f'plan:{day}', 'plan', None))
    if local.day==1 and local.hour>=9:
        out.append((f'monthly:{day[:7]}', 'monthly', None))
    for t in s['tasks']:
        if t['status']=='pending' and t['due'] and parse(t['due'])<=dt:
            out.append((f"task:{t['id']}:{t['due']}", 'task', t['id']))
    for f in s['focuses']:
        if f['status']=='running' and parse(f['ends'])<=dt:
            out.append((f"focus:{f['id']}:{f['ends']}", 'focus', f['id']))
    return [(k,kind,ident) for k,kind,ident in out if s['sent'].get(k,{}).get('status')!='sent']
