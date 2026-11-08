"""Persist sentence blocks and adopt the managed remote file without overwriting it."""
import difflib
import json
from . import sentences
from .local_writer import make_writer


class SentenceStore:
    def __init__(self, db, ha, settings):
        self.db, self.ha = db, ha
        self.writer = make_writer(db, settings)

    def blocks(self):
        return self.db.rows('SELECT * FROM sentence_blocks ORDER BY id')

    def replace(self, blocks):
        # One SQLite transaction keeps imports and rollback snapshots consistent.
        with self.db.conn:
            self.db.conn.execute('DELETE FROM sentence_blocks')
            for block in blocks:
                self.db.conn.execute('INSERT INTO sentence_blocks(id,intent,sentences,slots,requires_context,comment,created_at,change_id) VALUES (?,?,?,?,?,?,?,?)',
                    tuple(json.dumps(block.get(k, {})) if k in ('sentences', 'slots', 'requires_context') else block.get(k)
                          for k in ('id', 'intent', 'sentences', 'slots', 'requires_context', 'comment', 'created_at', 'change_id')))

    async def adopt(self):
        if self.db.meta('sentence_adopted') or self.blocks():
            return
        text = await self.writer.read()
        blocks = sentences.parse(text)
        self.replace(blocks)
        self.db.meta('sentence_remote_hash', sentences.fingerprint(text))
        self.db.meta('sentence_adopted', 'true')

    def listing(self):
        blocks = self.blocks()
        return dict(blocks=blocks, rendered=sentences.render(blocks), writer=self.writer.configuration())

    async def import_file(self, confirm=False, fingerprint=None):
        text = await self.writer.read()
        blocks = sentences.parse(text)
        current = sentences.render(self.blocks())
        revision = sentences.fingerprint((text or '') + '\nDB:\n' + current)
        if confirm:
            if fingerprint != revision:
                raise ValueError('Import preview changed; preview again before confirming')
            self.replace(blocks)
            self.db.meta('sentence_remote_hash', sentences.fingerprint(text))
            self.db.meta('sentence_adopted', 'true')
        return dict(confirmed=confirm, fingerprint=revision, current_file=text, blocks=blocks,
            diff='\n'.join(difflib.unified_diff(current.splitlines(), sentences.render(blocks).splitlines(), fromfile='Request Tuner DB', tofile='HA import', lineterm='')))

    async def snapshot(self):
        await self.adopt()
        text = await self.writer.read()
        expected = self.db.meta('sentence_remote_hash')
        if expected and expected != sentences.fingerprint(text):
            raise ValueError('Managed file changed outside Request Tuner; import from HA before applying')
        return text

    async def write(self, text, blocks):
        await self.writer.write(text)
        await self.ha.reload_sentences()
        self.replace(blocks)
        self.db.meta('sentence_remote_hash', sentences.fingerprint(text))
        self.db.meta('sentence_adopted', 'true')
