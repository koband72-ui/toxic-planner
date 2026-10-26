"""Gemini proposes structured actions; never gets direct database/tool access."""
import asyncio
import json
import logging
import os
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from google import genai
from google.genai import types
from domain import KYIV, parse


class Operation(BaseModel):
    action: Literal['add_task','edit','move','done','cancel','add_goal','add_idea','add_habit']
    id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due: str | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    minutes: int | None = Field(default=None, ge=1, le=1440)
    area: str | None = Field(default=None, max_length=80)
    goal_id: int | None = None
    reason: str | None = Field(default=None, max_length=500)
    target: int | None = Field(default=None, ge=1, le=7)
    minimum: str | None = Field(default=None, max_length=150)

    @model_validator(mode='after')
    def validate_action(self):
        if self.action.startswith('add_') and not (self.title and self.title.strip()):
            raise ValueError('Потрібна назва')
        if self.action in ('edit','move','done','cancel') and self.id is None:
            raise ValueError('Потрібен ID')
        if self.action=='move' and not self.due:
            raise ValueError('Потрібна нова дата')
        if self.due:
            parse(self.due)
        return self


class Proposal(BaseModel):
    reply: str = Field(max_length=1800)
    operations: list[Operation] = Field(default_factory=list, max_length=15)


class Assistant:
    def __init__(self):
        key=os.getenv('GEMINI_API_KEY')
        self.client=genai.Client(api_key=key).aio if key else None

    async def propose(self, message, state, dt, audio=None):
        if not self.client:
            return Proposal(reply='Gemini не налаштований. Команди /add, /tasks, /goals, /habits, /ideas працюють без нього.')
        context={k:state[k] for k in ('tasks','goals','habits','ideas')}
        context={k:v[-50:] for k,v in context.items()}
        
        current_time = dt.astimezone(KYIV).isoformat()
        prompt=f'''Ти — мій кореш і особистий планнер. Звертайся до мене «вась» або «братське серце». Базари жорстко і з матюками по ділу.
Зараз {current_time}, Europe/Kyiv.
Поверни ВІДПОВІДЬ ВИКЛЮЧНО У ВИГЛЯДІ ВАЛІДНОГО JSON (без жодних markdown-обгорток типу ```json), за такою структурою:
{{
  "reply": "Твоя відповідь або підтвердження у твоєму стилі з матами",
  "operations": [
    {{
      "action": "add_task",
      "title": "назва задачі",
      "due": "2026-10-28T19:00:00+02:00",
      "priority": 2
    }}
  ]
}}
Якщо це просто розмова або нема чіткої зміни — operations має бути порожнім ([]).
Власні записи користувача: {json.dumps(context,ensure_ascii=False)}
Повідомлення: {message[:4000]}'''

        contents=[prompt]
        if audio:
            contents.append(types.Part.from_bytes(data=audio, mime_type='audio/ogg'))
        try:
            async with asyncio.timeout(40):
                res=await self.client.models.generate_content(
                    model=os.getenv('GEMINI_MODEL','gemini-2.5-flash'), 
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
                    )
                )
            
            text_resp = res.text.strip()
            if text_resp.startswith('```'):
                text_resp = text_resp.split('```')[1]
                if text_resp.startswith('json'):
                    text_resp = text_resp[4:]
                text_resp = text_resp.strip()
            
            data = json.loads(text_resp)
            proposal = Proposal.model_validate(data)
            for op in proposal.operations:
                if op.id is not None and not any(t['id']==op.id for t in state['tasks']):
                    raise ValueError('Unknown task')
            return proposal
        except Exception as exc:
            logging.warning('Gemini request failed (%s)',type(exc).__name__)
            return Proposal(reply='Бля, затупив із цим запитом, вась. Напиши через /add назва | дата', operations=[])

    async def close(self):
        if self.client:
            await self.client.aclose()
