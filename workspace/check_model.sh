#!/usr/bin/env bash
# Run in another terminal in the same container after the server is ready.
set -euo pipefail
"${PYTHON_BIN:-python3}" - "${SERVER_URL:-http://127.0.0.1:30000}" <<'PY'
import json
import os
import sys
import tempfile
import urllib.request
url = sys.argv[1].rstrip('/')
fd, log_path = tempfile.mkstemp(prefix='sglang-request-', suffix='.jsonl')
log = os.fdopen(fd, 'w')
print('Response log:', log_path)

def post(path, body):
    request = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    log.write(json.dumps({'endpoint': path, 'request': body, 'response': result}, ensure_ascii=False) + '\n')
    log.flush()
    return result

try:
    with urllib.request.urlopen(url + '/health', timeout=10) as response:
        print('Health:', response.status)
    body = {'text': 'What is 1 + 1? Answer briefly.',
            'sampling_params': {'temperature': 0, 'max_new_tokens': 32},
            'routed_dp_rank': 0, 'stream': False}
    result = post('/generate', body)
    if not isinstance(result, dict):
        raise RuntimeError('Unexpected generate response type')
    meta = result.get('meta_info') or {}
    print('Raw completion:', 'tokens=', meta.get('completion_tokens'),
          'finish=', json.dumps(meta.get('finish_reason'), ensure_ascii=False),
          'text=', repr(result.get('text')))
    finish = meta.get('finish_reason') or {}
    if isinstance(finish, dict) and finish.get('type') == 'abort':
        raise RuntimeError('Server aborted generation: ' + str(finish))
    if result.get('text'):
        print('M1:OK')
    else:
        # /generate receives a raw continuation prompt. Chat applies the model's
        # own tokenizer template, which may avoid immediate EOS for chat weights.
        print('Raw text empty; checking the model chat template...')
        result = post('/v1/chat/completions', {
            'model': 'default',
            'messages': [{'role': 'user', 'content': 'What is 1 + 1? Answer briefly.'}],
            'temperature': 0, 'max_tokens': 128, 'stream': False})
        choice = (result.get('choices') or [{}])[0]
        message = choice.get('message') or {}
        print('Chat:', 'tokens=', (result.get('usage') or {}).get('completion_tokens'),
              'finish=', choice.get('finish_reason'),
              'content=', repr(message.get('content')),
              'reasoning=', repr(message.get('reasoning_content')))
        if message.get('content'):
            print('M1:CHAT_OK_RAW_EMPTY')
        elif message.get('reasoning_content'):
            print('M1:REASONING_ONLY')
            raise SystemExit(1)
        else:
            raise RuntimeError('Raw and chat responses both have no generated text; see response log')
except Exception as exc:
    print('M1:FAIL', type(exc).__name__, str(exc)[:300])
    raise SystemExit(1)
finally:
    log.close()
PY
