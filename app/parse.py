import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def seconds(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() if start and end else None


def parse_run(payload):
    events = payload['events']
    by_type = {e['type']: e for e in events}
    def data(kind):
        return (by_type.get(kind, {}).get('data') or {})
    def stamp(kind):
        return by_type.get(kind, {}).get('timestamp')
    start, end = data('intent-start'), data('intent-end')
    response = end.get('intent_output', {}).get('response', {})
    tools, chunks = {}, []
    for event in events:
        delta = (event.get('data') or {}).get('chat_log_delta', {})
        for call in delta.get('tool_calls', []):
            tools[call['id']] = dict(id=call['id'], name=call['tool_name'], args=call.get('tool_args', {}), result=None)
        if delta.get('tool_call_id') in tools:
            result = delta.get('tool_result')
            encoded = json.dumps(result, ensure_ascii=False)
            tools[delta['tool_call_id']]['result'] = result if len(encoded) <= 2000 else encoded[:2000]
        if isinstance(delta.get('content'), str):
            chunks.append(delta['content'])
    return dict(started_at=stamp('run-start') or now(), ended_at=stamp('run-end'),
                status='complete' if 'run-end' in by_type else 'in_progress',
                source='voice' if 'stt-end' in by_type else 'text',
                transcript=data('stt-end').get('stt_output', {}).get('text', start.get('intent_input', '')),
                device_id=start.get('device_id'), satellite_id=start.get('satellite_id'),
                conversation_id=start.get('conversation_id', data('run-start').get('conversation_id')),
                engine=start.get('engine'), prefer_local=start.get('prefer_local_intents'),
                processed_locally=end.get('processed_locally'), response_type=response.get('response_type'),
                reply=response.get('speech', {}).get('plain', {}).get('speech', ''.join(chunks)),
                duration_s=seconds(stamp('run-start'), stamp('run-end')),
                llm_s=seconds(stamp('intent-start'), stamp('intent-end')) if end.get('processed_locally') is False else None,
                tools=list(tools.values()), tool_count=len(tools), error='error' in by_type, raw_events=events)
