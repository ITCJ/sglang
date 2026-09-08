#!/usr/bin/env python3
"""Chat functionality checks. These are not KV-hit or performance measurements."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request
import urllib.error


def stream_result(response, events):
    content, reasoning, finish = '', '', None
    done, data = False, []
    for raw in response:
        line = raw.decode('utf-8').rstrip('\r\n')
        if line.startswith('data:'):
            data.append(line[5:].lstrip())
        elif not line and data:
            value = '\n'.join(data)
            data = []
            if value == '[DONE]':
                done = True
                break
            event = json.loads(value)
            events.append(event)
            if event.get('error'):
                raise RuntimeError(str(event['error']))
            for choice in event.get('choices', []):
                delta = choice.get('delta') or {}
                content += delta.get('content') or ''
                reasoning += delta.get('reasoning_content') or ''
                finish = choice.get('finish_reason') or finish
    if not done:
        raise RuntimeError('Stream ended without [DONE]')
    return content, reasoning, finish


def validate(content, finish):
    if not content.strip():
        raise RuntimeError('No final answer text; reasoning-only is not a pass')
    if finish != 'stop':
        raise RuntimeError(f'Incomplete answer: finish_reason={finish!r}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('SERVER_URL', 'http://127.0.0.1:30000'))
    parser.add_argument('--model', default=os.environ.get('MODEL_ID'))
    parser.add_argument('--suite', action='store_true', help='include streaming and multi-turn requests')
    parser.add_argument('--max-tokens', type=int, default=256)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    if args.max_tokens < 1 or args.timeout <= 0:
        parser.error('max-tokens and timeout must be positive')
    directory = Path(tempfile.mkdtemp(prefix='sglang-chat-'))
    print('Logs:', directory, flush=True)
    headers = {'Content-Type': 'application/json'}
    if os.environ.get('API_KEY'):
        headers['Authorization'] = 'Bearer ' + os.environ['API_KEY']
    base = args.url.rstrip('/')

    def open_request(path, body=None):
        request = urllib.request.Request(base + path, headers=headers,
            data=None if body is None else json.dumps(body).encode())
        return urllib.request.urlopen(request, timeout=args.timeout)

    results = []
    try:
        with open_request('/health') as response:
            print('Health:', response.status, flush=True)
        model = args.model
        if not model:
            with open_request('/v1/models') as response:
                models = json.load(response)
            (directory / 'models.json').write_text(json.dumps(models, indent=2))
            model = models['data'][0]['id']
        question = [{'role': 'user', 'content': 'What is 1 + 1? Answer briefly.'}]
        cases = [('chat', question, False)]
        if args.suite:
            cases += [('stream', question, True), ('multi-turn', [
                {'role': 'user', 'content': 'Remember this word: apple.'},
                {'role': 'assistant', 'content': 'I will remember apple.'},
                {'role': 'user', 'content': 'Which word did I ask you to remember? Answer briefly.'}], False)]
        for name, messages, stream in cases:
            print('Testing:', name, flush=True)
            body = {'model': model, 'messages': messages, 'temperature': 0,
                    'max_tokens': args.max_tokens, 'stream': stream}
            record = {'case': name, 'request': body, 'events': [], 'ok': False}
            start = time.perf_counter()
            try:
                with open_request('/v1/chat/completions', body) as response:
                    record['http_status'] = response.status
                    if stream:
                        content, reasoning, finish = stream_result(response, record['events'])
                    else:
                        raw = response.read().decode('utf-8')
                        record['raw_response'] = raw
                        result = json.loads(raw)
                        if result.get('error'):
                            raise RuntimeError(str(result['error']))
                        choice = result['choices'][0]
                        message = choice['message']
                        content = message.get('content') or ''
                        reasoning = message.get('reasoning_content') or ''
                        finish = choice.get('finish_reason')
                        record['usage'] = result.get('usage')
                record.update(content=content, reasoning=reasoning, finish=finish)
                validate(content, finish)
                record['ok'] = True
                print(f'{name}: PASS finish={finish}; answer={content[:160]!r}', flush=True)
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    record['error_response'] = exc.read().decode('utf-8', errors='replace')
                record['error'] = f'{type(exc).__name__}: {exc}'
                print(f'{name}: FAIL {record["error"][:240]}', flush=True)
            finally:
                record['elapsed_ms'] = (time.perf_counter() - start) * 1000
                (directory / f'{name}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
                results.append({'case': name, 'ok': record['ok']})
        success = all(result['ok'] for result in results)
        (directory / 'summary.json').write_text(json.dumps(results, indent=2))
        print('M1:OK' if success else 'M1:FAIL')
        return int(not success)
    except Exception as exc:
        (directory / 'setup-error.txt').write_text(f'{type(exc).__name__}: {exc}')
        print('M1:SETUP_FAIL', type(exc).__name__, str(exc)[:240])
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
