"""Fixed-input deployment measurement only. No product APIs or model calls."""
import time
PROCESS_ENTRY_NS = time.perf_counter_ns()
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import sys
import uuid

ROOT = Path(__file__).resolve().parent

def measure():
    started = time.perf_counter()
    import nltk
    import_ms = (time.perf_counter() - started) * 1000
    # Never fall back to pre-existing user/system corpora or download at runtime.
    nltk.data.path[:] = [str(Path(os.environ.get('PROBE_DATA', ROOT / 'nltk_data')).resolve())]
    from nltk.corpus import wordnet
    from nltk.stem import WordNetLemmatizer
    from nltk.tag import PerceptronTagger
    started = time.perf_counter()
    wordnet.ensure_loaded()
    load_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    tagger = PerceptronTagger()
    tagger_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    tags = tagger.tag(['The', 'banks', 'approved', 'the', 'loans', '.'])
    lemma = WordNetLemmatizer().lemmatize('banks', 'n')
    senses = wordnet.synsets('bank')
    work_ms = (time.perf_counter() - started) * 1000
    assert lemma == 'bank' and len(senses) > 0
    return {
        'boot_id': str(uuid.uuid4()),
        'scope': 'process_only_not_platform_cold_start',
        'platform': platform.platform(), 'machine': platform.machine(),
        'python': platform.python_version(),
        'versions': {p: importlib.metadata.version(p) for p in ['nltk', 'fastapi', 'uvicorn', 'pydantic']},
        'nltk_import_ms': import_ms, 'wordnet_first_load_ms': load_ms,
        'tagger_first_load_ms': tagger_ms, 'first_nlp_operation_ms': work_ms,
        'wordnet_version': wordnet.get_version(), 'tags': tags,
        'lemma': lemma, 'bank_senses': len(senses),
        'max_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
    }

if __name__ == '__main__':
    if '--without-wordnet' in sys.argv:
        import nltk
        nltk.data.path[:] = [str(ROOT / '__intentionally_absent_corpus__')]
        from nltk.stem import WordNetLemmatizer
        try:
            result = WordNetLemmatizer().lemmatize('banks', 'n')
        except LookupError:
            print(json.dumps({'wordnet_removed': True, 'result': 'LookupError', 'lemmatizer_works': False}))
        else:
            raise AssertionError(f'Unexpected success: {result}')
    else:
        print(json.dumps(measure()))
else:
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    @asynccontextmanager
    async def lifespan(app):
        app.state.metrics = measure()
        app.state.metrics['module_entry_to_ready_ms'] = (time.perf_counter_ns() - PROCESS_ENTRY_NS) / 1e6
        print(json.dumps({'event': 'probe_ready', **app.state.metrics}), flush=True)
        yield
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    @app.get('/health')
    async def health():
        return {'ok': True}
    @app.get('/probe')
    async def probe():
        return app.state.metrics
