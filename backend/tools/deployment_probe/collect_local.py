"""Independent process samples; explicitly NOT veFaaS cold-start measurements."""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

p = argparse.ArgumentParser()
p.add_argument('--data', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
p.add_argument('--samples', type=int, default=10)
a = p.parse_args()
assert a.samples > 0
root = Path(__file__).resolve().parent
env = {**os.environ, 'PROBE_DATA': str(a.data.resolve()), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
rows = []
for i in range(a.samples):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        start = time.perf_counter()
        proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'probe:app', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=root, env=env, stdout=log, stderr=log)
        try:
            deadline = start + 60
            while True:
                if proc.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read().decode())
                try:
                    with opener.open(f'http://127.0.0.1:{port}/probe', timeout=1) as r:
                        data = json.load(r)
                    break
                except (urllib.error.URLError, TimeoutError):
                    if time.perf_counter() > deadline: raise TimeoutError('probe did not start')
                    time.sleep(.01)
            data['local_process_to_first_response_ms'] = (time.perf_counter() - start) * 1000
            data['sample'] = i + 1
            rows.append(data)
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
without = json.loads(subprocess.check_output([sys.executable, '-B', str(root / 'probe.py'), '--without-wordnet'], env=env))
summary = {}
for key in ['nltk_import_ms', 'wordnet_first_load_ms', 'tagger_first_load_ms', 'first_nlp_operation_ms', 'local_process_to_first_response_ms', 'max_rss_bytes']:
    values = sorted(row[key] for row in rows)
    summary[key] = {'min': min(values), 'median': statistics.median(values), 'p95_nearest_rank': values[math.ceil(.95 * len(values)) - 1], 'max': max(values)}
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps({'scope': 'local_process_restart_OS_cache_not_cleared', 'platform_cold_start_ms': None, 'sample_count': len(rows), 'summary': summary, 'without_wordnet': without, 'samples': rows}, ensure_ascii=False, indent=2))
print(json.dumps(summary, indent=2))
