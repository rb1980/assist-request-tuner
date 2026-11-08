import asyncio
import logging
import re
from pathlib import Path
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import names
from .precheck import check_template

PATH = names.BLUEPRINT_PATH   # how automations refer to it
BUNDLED = (Path(__file__).parent / 'blueprints' / 'voice_reply.yaml').read_bytes().decode()
logger = logging.getLogger(names.LOGGER + '.blueprint')


class ReplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entity: str = Field(pattern=r'^[a-z_]+\.[a-z0-9_]+$')
    reply: str = Field(min_length=1, max_length=1000)


class Inputs(ReplyInput):
    sentences: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1, max_length=20)
    unavailable_reply: str | None = Field(default=None, max_length=300)

    @model_validator(mode='after')
    def validate_inputs(self):
        if any(not s.strip() or any(c in s for c in '{}<>') for s in self.sentences):
            raise ValueError('Blueprint sentences must not contain wildcards or rule references')
        result = check_template(self.sentences, [])
        if result['error']:
            raise ValueError('Invalid sentence syntax: ' + result['error'])
        if self.unavailable_reply is not None and any(s in self.unavailable_reply for s in ('{{', '{%')):
            raise ValueError('unavailable_reply must be plain text')
        return self


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    automation_id: str = Field(pattern=names.AUTOMATION_ID)
    alias: str = Field(min_length=1, max_length=100)
    blueprint: Literal['voice_reply']
    inputs: Inputs


def instance(config):
    value = (config or {}).get('use_blueprint', {})
    return value.get('input') if value.get('path') == PATH else None


def version(text):
    match = re.search(r'blueprint version (\d+)', text or '')
    return int(match[1]) if match else None


class Blueprints:
    def __init__(self, db, ha, writer):
        self.db, self.ha, self.writer = db, ha, writer
        self.lock = asyncio.Lock()

    async def listing(self):
        remote = await self.writer.read('blueprint')
        return dict(id='voice_reply', path=PATH, version=version(BUNDLED), remote_version=version(remote),
                    status='absent' if remote is None else 'in sync' if remote == BUNDLED else 'differs',
                    previous=self.db.meta('blueprint_previous'))

    async def ensure_blueprint(self):
        async with self.lock:
            remote = await self.writer.read('blueprint')
            if remote != BUNDLED:
                if remote is not None:
                    logger.warning('Replacing differing managed voice_reply blueprint; previous text retained in meta')
                    self.db.meta('blueprint_previous', remote)
                await self.writer.write(BUNDLED, 'blueprint')
                self.db.meta('blueprint_reload_pending', '1')
            if self.db.meta('blueprint_reload_pending') == '1':
                await self.ha.rest('POST', service='automation.reload')
                self.db.meta('blueprint_reload_pending', '0')
        return await self.listing()

    async def preview_reply(self, entity, reply):
        value = ReplyInput(entity=entity, reply=reply)
        await self.ha.refresh()
        if not any(e['entity_id'] == value.entity and e.get('exposed') is True for e in self.ha.entities):
            raise ValueError('Select an existing exposed entity')
        return await self.ha.render_template(value.reply, value.entity)
