from datetime import datetime
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from .analysis import requests, stats


def render(db):
    registry = CollectorRegistry()
    count = Gauge('ar_tuner_requests_total', 'Retained captured requests', ['handled_by', 'source', 'topic'], registry=registry)
    groups = {}
    for row in requests(db, include_excluded=True, limit=-1):
        key = (row['handled_by'], row['source'], row['effective_topic'])
        groups[key] = groups.get(key, 0) + 1
    for key, value in groups.items():
        count.labels(*key).set(value)
    Gauge('ar_tuner_home_coverage_ratio', 'Voice home coverage over 30 days', registry=registry).set(stats(db)['home_coverage'])
    Gauge('ar_tuner_collector_up', 'Last poll succeeded', registry=registry).set(db.meta('collector_status') == 'up')
    poll = db.meta('last_poll')
    Gauge('ar_tuner_last_poll_timestamp_seconds', 'Last successful poll time', registry=registry).set(datetime.fromisoformat(poll).timestamp() if poll else 0)
    Gauge('ar_tuner_changes_applied_total', 'Successful applies in the audit log', registry=registry).set(db.rows("SELECT count(*) AS n FROM changes_log WHERE action='applied'")[0]['n'])
    return generate_latest(registry)
