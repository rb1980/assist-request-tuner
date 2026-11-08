import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from . import names
from .working import ensure_working
from .parse import parse_run, now
from .patterns import normalize, pattern
from .classify import classify
from .ha import safe_message, safe_url

logger = logging.getLogger(names.LOGGER + ".collector")


class Collector:
    def __init__(self, db, ha, settings):
        self.db, self.ha, self.settings = db, ha, settings
        self.pipelines = []
        self.pipeline_at = 0
        self.watched = None
        self.retention_at = 0
        self.initial_import_done = False
        self.initial_import_ids = set()

    async def poll(self):
        if not self.settings.ha_token:
            self.db.meta('collector_status', 'not configured')
            return
        # Re-read the pipeline list often: HA keeps only the last ten runs of a pipeline, so one created after start-up
        # (often the LLM pipeline, set up after installing Request Tuner) must be noticed before it has run ten times.
        if time.monotonic() - self.pipeline_at > 30 or not self.pipelines:
            self.pipelines = (await self.ha.call('assist_pipeline/pipeline/list'))['pipelines']
            self.pipeline_at = time.monotonic()
            watched = [f"{p['name']} ({p['id']})" for p in self.pipelines
                       if p['id'] not in self.settings.ignore_pipelines and p['name'] not in self.settings.ignore_pipelines]
            if watched != self.watched:
                logger.info('Pipelines watched: %s', safe_message(self.settings, ', '.join(watched) or '(none)'))
                self.watched = watched
        for pipeline in self.pipelines:
            if pipeline['id'] in self.settings.ignore_pipelines or pipeline['name'] in self.settings.ignore_pipelines:
                continue
            runs = (await self.ha.call('assist_pipeline/pipeline_debug/list', pipeline_id=pipeline['id']))['pipeline_runs']
            # Keep fetching active runs even after they leave HA's ten-entry listing.
            active = self.db.rows("SELECT pipeline_run_id, started_at AS timestamp FROM requests WHERE pipeline_id=? AND status='in_progress'", (pipeline['id'],))
            runs = {r['pipeline_run_id']: r for r in active + runs}.values()
            for run in runs:
                old = self.db.rows('SELECT status,started_at FROM requests WHERE pipeline_run_id=?', (run['pipeline_run_id'],))
                if old and old[0]['status'] == 'complete':
                    continue
                if old and (datetime.now(timezone.utc) - datetime.fromisoformat(old[0]['started_at'])).total_seconds() > 600:
                    self.db.execute("UPDATE requests SET status='complete', handled_by=CASE WHEN processed_locally IS NULL THEN 'error' ELSE handled_by END WHERE pipeline_run_id=?", (run['pipeline_run_id'],))
                    continue
                payload = await self.ha.call('assist_pipeline/pipeline_debug/get', pipeline_id=pipeline['id'], pipeline_run_id=run['pipeline_run_id'])
                row = parse_run(payload)
                row.update(pipeline_run_id=run['pipeline_run_id'], pipeline_id=pipeline['id'], pipeline_name=pipeline['name'])
                row['normalized'] = normalize(row['transcript'], self.settings.prefixes)
                row['pattern'] = pattern(row['transcript'], self.ha.entities, self.ha.areas, self.settings.prefixes)
                device = next((d for d in self.ha.devices if d['id'] == row['device_id']), {})
                row['device_name'] = device.get('name_by_user') or device.get('name') or row['device_id']
                window = self.db.rows('SELECT id FROM ignore_windows WHERE start<=? AND end>=?', (row['started_at'], row['started_at']))
                capturing_window = self.db.rows('SELECT id FROM ignore_windows WHERE start<=? AND end>=?', (now(), now()))
                row['excluded'] = bool(window or capturing_window or row['device_id'] in self.settings.exclude_devices)
                row.update(classify(row, None, self.settings, self.ha.entities, self.ha.areas))
                self.db.store_run(row, self.settings.store_raw)
                if not old and not self.initial_import_done:
                    self.initial_import_ids.add(run['pipeline_run_id'])
                self.db.execute('UPDATE requests SET matcher=NULL WHERE pipeline_run_id=?', (row['pipeline_run_id'],))
        # Capture before refreshing the larger registries so the ten-run HA buffer
        # is drained promptly even on the initial import.
        await self.ha.refresh()
        # SQL NULL means not attempted; JSON null is a successful no-match response.
        pending = self.db.rows("SELECT * FROM requests WHERE matcher IS NULL AND transcript!=''")
        for start in range(0, len(pending), 50):
            batch = pending[start:start + 50]
            matches = await self.ha.match([r['transcript'] for r in batch])
            for row, match in zip(batch, matches):
                row['matcher'] = match
                row['pattern'] = pattern(row['transcript'], self.ha.entities, self.ha.areas, self.settings.prefixes)
                device = next((d for d in self.ha.devices if d['id'] == row['device_id']), {})
                row['device_name'] = device.get('name_by_user') or device.get('name') or row['device_id']
                row['error'] = row['handled_by'] == 'error'
                row.update(classify(row, match, self.settings, self.ha.entities, self.ha.areas))
                self.db.store_run(row, self.settings.store_raw)
        for row in self.db.rows("SELECT * FROM requests WHERE working_phrasings IS NULL AND handled_by='llm' AND status='complete'"):
            await ensure_working(self.db, self.ha, row)
        if not self.retention_at or time.monotonic() - self.retention_at > 86400:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.retention)).isoformat()
            self.db.execute('DELETE FROM requests WHERE started_at<?', (cutoff,))
            self.retention_at = time.monotonic()
        self.db.meta('last_poll', now())
        self.db.meta('collector_status', 'up')
        if not self.initial_import_done:
            logger.info('Initial import complete: %d runs', len(self.initial_import_ids))
            self.initial_import_done = True
            self.initial_import_ids.clear()

    async def run(self):
        logger.info('Collector starting: HA URL=%s', safe_url(self.settings))
        while True:
            try:
                await self.poll()
            except Exception as exc:
                logger.warning('Collector poll failed: %s: %s', type(exc).__name__, safe_message(self.settings, exc))
                self.db.meta('collector_status', 'HA unavailable; retrying')
            await asyncio.sleep(self.settings.poll_interval)
