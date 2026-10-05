"""Provider-scoped model limits; rendering never performs network I/O."""
import json
import os
import tempfile
import urllib.request
from pathlib import Path
from terminal_ui import positive_int

def _read(path):
    try:
        if Path(path).stat().st_size > 8*1024*1024: return {}
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError): return {}

SNAPSHOT=_read(Path(__file__).parent/'data'/'model-limits.json')
CACHE_PATH=Path(os.environ.get('XDG_CACHE_HOME') or str(Path.home()/'.cache'))/'uachat'/'model-limits.json'
CACHED=_read(CACHE_PATH)
CODEX_CACHE=_read(Path(os.environ.get('CODEX_HOME') or str(Path.home()/'.codex'))/'models_cache.json')

def resolve(provider,model,metadata=None):
    meta=metadata if isinstance(metadata,dict) else {}
    limits=meta.get('limit') if isinstance(meta.get('limit'),dict) else {}
    context=next((n for n in (positive_int(meta.get(k)) for k in ('context_window','context_length','max_context_tokens')) if n),None) or positive_int(limits.get('context'))
    if context:
        return {'context':context,'output':positive_int(meta.get('max_output_tokens')) or positive_int(limits.get('output')),'source':'provider catalogue'}
    if provider=='ollama': return None  # architecture maximum is not configured num_ctx
    if provider=='openai-codex':
        root=os.environ.get('CODEX_HOME') or str(Path.home()/'.codex')
        # Read the lightweight local cache, not account credentials.
        cache=CODEX_CACHE
        for item in cache.get('models',[]):
            if isinstance(item,dict) and item.get('slug')==model and positive_int(item.get('context_window')):
                return {'context':positive_int(item['context_window']),'output':positive_int(item.get('max_output_tokens')),'source':'Codex catalogue'}
    item=CACHED.get('providers',{}).get(provider,{}).get(model) or SNAPSHOT.get('providers',{}).get(provider,{}).get(model)
    return dict(item) if isinstance(item,dict) else None

def describe(provider,model,metadata=None):
    item=resolve(provider,model,metadata)
    if not item: return 'context unavailable'
    return f"ctx {item['context']/1000:g}k · {item['source']}"


def refresh():
    """Explicit catalogue refresh. No credentials sent and no render-time I/O."""
    global CACHED, CODEX_CACHE
    request=urllib.request.Request('https://models.dev/api.json',headers={'User-Agent':'uachat/0.7'})
    with urllib.request.urlopen(request,timeout=15) as response:
        raw=response.read(32*1024*1024+1)
    if len(raw)>32*1024*1024: raise ValueError('model catalogue too large')
    data=json.loads(raw)
    if not isinstance(data,dict): raise ValueError('invalid model catalogue')
    providers={}
    for target,origin in [('openai','openai'),('opencode-go','opencode-go'),('openrouter','openrouter'),('fireworks','fireworks-ai'),('google-antigravity','google')]:
        source=data.get(origin,{})
        models=source.get('models',{}) if isinstance(source,dict) else {}
        entries={}
        for model,entry in models.items():
            limits=entry.get('limit',{}) if isinstance(entry,dict) else {}
            window=positive_int(limits.get('context'))
            if window: entries[model]={'context':window,'output':positive_int(limits.get('output')),'source':'models.dev (refreshed)'}
        providers[target]=entries
    if not any(providers.values()): raise ValueError('empty model catalogue')
    value={'providers':providers}
    CACHE_PATH.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.limits-',dir=CACHE_PATH.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,separators=(',',':'));stream.flush();os.fsync(stream.fileno())
        os.replace(name,CACHE_PATH)
    finally:
        if os.path.exists(name): os.unlink(name)
    CACHED=value
    CODEX_CACHE=_read(Path(os.environ.get('CODEX_HOME') or str(Path.home()/'.codex'))/'models_cache.json')
    return sum(len(entries) for entries in providers.values())
