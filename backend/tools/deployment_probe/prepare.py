"""Build an isolated Linux CPython 3.12 dependency/corpus probe, not the product."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument('--work', type=Path, required=True)
p.add_argument('--data-only', action='store_true')
p.add_argument('--package-only', action='store_true')
a = p.parse_args()
work = a.work.resolve()
work.mkdir(parents=True, exist_ok=True)

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'SentenceScope-deployment-probe'})
    with urllib.request.urlopen(req, timeout=90) as response: return response.read()

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def size(path): return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())

if not a.package_only:
    source = work / 'source.json'
    locked = json.loads((ROOT / 'sources.lock.json').read_text())
    commit = locked['commit']
    resources = []
    for rel in ['corpora/wordnet.zip', 'taggers/averaged_perceptron_tagger_eng.zip']:
        url = f'https://raw.githubusercontent.com/nltk/nltk_data/{commit}/packages/{rel}'
        dest = work / 'downloads' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists(): dest.write_bytes(fetch(url))
        expected = next(r for r in locked['resources'] if r['url'] == url)
        if digest(dest) != expected['sha256']:
            raise SystemExit(f'Corpus checksum mismatch: {dest}')
        with zipfile.ZipFile(dest) as z:
            parent = work / 'nltk_data' / Path(rel).parent
            for info in z.infolist():
                assert '..' not in Path(info.filename).parts and not Path(info.filename).is_absolute()
            z.extractall(parent)
        resources.append({'url': url, 'bytes': dest.stat().st_size, 'sha256': digest(dest)})
    source.write_text(json.dumps({'commit': commit, 'resources': resources}, indent=2))
    print(source.read_text(), flush=True)
if a.data_only: raise SystemExit(0)

stage = work / 'linux-probe'
if stage.exists(): raise SystemExit(f'Refusing to overwrite existing staging directory: {stage}')
stage.mkdir()
subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', '--no-compile', '--only-binary=:all:', '--platform', 'manylinux2014_x86_64', '--platform', 'manylinux_2_17_x86_64', '--platform', 'manylinux_2_28_x86_64', '--implementation', 'cp', '--python-version', '3.12', '--abi', 'cp312', '--target', str(stage / 'vendor'), '-r', str(ROOT / 'requirements.lock'), '--report', str(work / 'pip-linux-report.json')], check=True)
for name in ['probe.py', 'run.sh', 'requirements.lock']:
    shutil.copy2(ROOT / name, stage / name)
shutil.copytree(work / 'nltk_data', stage / 'nltk_data')
# Explicit whitelist: no user sources, .env, venv, tests or pip caches.
archive = work / 'linux-probe.zip'
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for file in sorted(stage.rglob('*')):
        if file.is_file():
            info = zipfile.ZipInfo(file.relative_to(stage).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if file.name == 'run.sh' else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, file.read_bytes(), compresslevel=9)
with zipfile.ZipFile(archive) as z: assert z.testzip() is None
summary = {
    'scope': 'cross_platform_pip_install_on_mac_NOT_Linux_execution',
    'target': 'CPython 3.12 / Debian 11-compatible manylinux x86_64 wheels',
    'dependencies_bytes': size(stage / 'vendor'),
    'wordnet_extracted_bytes': size(stage / 'nltk_data/corpora/wordnet'),
    'tagger_extracted_bytes': size(stage / 'nltk_data/taggers/averaged_perceptron_tagger_eng'),
    'probe_payload_bytes': size(stage),
    'probe_zip_bytes': archive.stat().st_size,
    'probe_zip_sha256': digest(archive),
    'cloud_cold_start_ms': None,
    'excludes': ['lexicon.db', 'business APIs', 'model SDK', 'Python interpreter supplied by platform', 'test dependencies'],
    'files': [{'path': f.relative_to(stage).as_posix(), 'bytes': f.stat().st_size, 'sha256': digest(f)} for f in sorted(stage.rglob('*')) if f.is_file()],
}
(work / 'size-results.json').write_text(json.dumps(summary, indent=2))
print(json.dumps({k: v for k, v in summary.items() if k != 'files'}, indent=2))
