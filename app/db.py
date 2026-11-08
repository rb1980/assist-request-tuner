import json
import sqlite3
import zlib
from pathlib import Path
from .parse import now

JSON_FIELDS = {'sentences', 'slots', 'requires_context', 'sentence_before', 'sentence_after', 'capture_preview', 'working_phrasings', 'tools', 'matcher', 'payload', 'tests', 'before', 'after', 'test_result', 'replay_result', 'detail', 'precheck', 'steps', 'result'}


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS requests (
          id INTEGER PRIMARY KEY, pipeline_run_id TEXT UNIQUE, pipeline_id TEXT, pipeline_name TEXT,
          started_at TEXT, ended_at TEXT, status TEXT, source TEXT, transcript TEXT, normalized TEXT, pattern TEXT,
          device_id TEXT, device_name TEXT, satellite_id TEXT, conversation_id TEXT, engine TEXT, prefer_local INTEGER,
          processed_locally INTEGER, handled_by TEXT, topic TEXT, topic_override TEXT, excluded INTEGER DEFAULT 0,
          response_type TEXT, reply TEXT, duration_s REAL, llm_s REAL, tool_count INTEGER, tools TEXT, matcher TEXT,
          raw_events BLOB, created_at TEXT);
        CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at);
        CREATE INDEX IF NOT EXISTS requests_handled ON requests(handled_by);
        CREATE INDEX IF NOT EXISTS requests_pattern ON requests(pattern);
        CREATE TABLE IF NOT EXISTS patterns (pattern TEXT PRIMARY KEY, ignored INTEGER DEFAULT 0, topic_override TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY, created_at TEXT, updated_at TEXT, type TEXT,
          title TEXT, payload TEXT, tests TEXT, allow_device_actions INTEGER, status TEXT, "before" TEXT, "after" TEXT,
          test_result TEXT, replay_result TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS changes_log (id INTEGER PRIMARY KEY, at TEXT, change_id INTEGER, action TEXT, actor TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS ignore_windows (id INTEGER PRIMARY KEY, start TEXT, end TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
        ''')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS diagnoses (
          id INTEGER PRIMARY KEY, created_at TEXT, target_type TEXT, target TEXT, status TEXT, model TEXT,
          steps TEXT, result TEXT, error TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
          cost_estimate REAL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS diagnoses_target ON diagnoses(target_type, target);
        CREATE TABLE IF NOT EXISTS diagnosis_usage (
          day TEXT PRIMARY KEY, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, cost_estimate REAL DEFAULT 0);
        ''')
        columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(changes)')}
        for name, definition in [('diagnosis_id', 'INTEGER'), ('source', "TEXT DEFAULT 'manual'"), ('precheck', 'TEXT'), ('capture_preview', 'TEXT'), ('sentence_before', 'TEXT'), ('sentence_after', 'TEXT'), ('reply_preview', 'TEXT')]:
            if name not in columns:
                self.conn.execute(f'ALTER TABLE changes ADD COLUMN {name} {definition}')
        self.conn.execute('CREATE TABLE IF NOT EXISTS sentence_blocks (id INTEGER PRIMARY KEY, intent TEXT NOT NULL, sentences TEXT NOT NULL, slots TEXT NOT NULL, requires_context TEXT NOT NULL, comment TEXT, created_at TEXT, change_id INTEGER)')
        request_columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(requests)')}
        if 'working_phrasings' not in request_columns:
            self.conn.execute('ALTER TABLE requests ADD COLUMN working_phrasings TEXT')
        self.conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        self.conn.commit()

    def execute(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def rows(self, sql, args=()):
        result = []
        for row in self.conn.execute(sql, args):
            row = dict(row)
            for key in {'processed_locally', 'prefer_local', 'excluded', 'ignored', 'allow_device_actions'} & row.keys():
                if row[key] is not None:
                    row[key] = bool(row[key])
            for key in JSON_FIELDS & row.keys():
                row[key] = json.loads(row[key]) if row[key] is not None else None
            if row.get('raw_events'):
                row['raw_events'] = json.loads(zlib.decompress(row['raw_events']))
            result.append(row)
        return result

    def save(self, table, values, key='id'):
        values = dict(values)
        for k in JSON_FIELDS & values.keys():
            values[k] = json.dumps(values[k])
        if values.get('raw_events') is not None:
            values['raw_events'] = zlib.compress(json.dumps(values['raw_events']).encode())
        columns = ','.join('"' + k + '"' for k in values)
        updates = ','.join(f'"{k}"=excluded."{k}"' for k in values if k != key)
        cur = self.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in values)}) ON CONFLICT({key}) DO UPDATE SET {updates}', tuple(values.values()))
        return values.get(key) or cur.lastrowid

    def meta(self, key, value=None):
        if value is not None:
            self.save('meta', {'key': key, 'value': str(value)}, 'key')
        rows = self.rows('SELECT value FROM meta WHERE key=?', (key,))
        return rows[0]['value'] if rows else None

    def store_run(self, row, store_raw=True):
        row = {k: v for k, v in row.items() if k not in {'error', 'topic_override'}}
        row['raw_events'] = row.get('raw_events') if store_raw else None
        old = self.rows('SELECT id,created_at,excluded FROM requests WHERE pipeline_run_id=?', (row['pipeline_run_id'],))
        row['created_at'] = old[0]['created_at'] if old else now()
        if old:
            row['excluded'] = bool(old[0]['excluded'] or row.get('excluded'))
        return self.save('requests', row, 'pipeline_run_id')

    def log(self, change_id, action, actor, detail):
        self.save('changes_log', dict(at=now(), change_id=change_id, action=action, actor=actor, detail=detail))
