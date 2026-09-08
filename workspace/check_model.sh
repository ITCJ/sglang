#!/usr/bin/env bash
# Run in another terminal in the same container after the server is ready.
set -euo pipefail
"${PYTHON_BIN:-python3}" - "${SERVER_URL:-http://127.0.0.1:30000}" <<'PY'
import json
import sys
import urllib.request
url = sys.argv[1].rstrip('/')
try:
    with urllib.request.urlopen(url + '/health', timeout=10) as response:
        print('Health:', response.status)
    body = {'text': 'What is 1 + 1? Answer briefly.',
            'sampling_params': {'temperature': 0, 'max_new_tokens': 32},
            'routed_dp_rank': 0, 'stream': False}
    request = urllib.request.Request(url + '/generate', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not result.get('text'):
        raise RuntimeError('Response has no generated text: ' + str(result)[:300])
    print('Generated:', result['text'])
    print('M1:OK')
except Exception as exc:
    print('M1:FAIL', type(exc).__name__, str(exc)[:300])
    raise SystemExit(1)
PY
