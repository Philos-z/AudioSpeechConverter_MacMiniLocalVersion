"""Fail closed on private source-specific credentials omitted from public builds.

Ordinary production does not register historical exceptions. Existing portable
picture/audio/timing registries retain their separate strict validators.
"""
from pathlib import Path
import json

class PrivateScopeUnavailable(ValueError):
    pass

PATTERNS = (
    'translation/*adoption*.json', 'translation/user_editorial*.json',
    'translation/assistant_editorial_revisions.json', 'translation/tagesschau*.json',
    'translation/frozen_ordinary_reviews.json', 'translation/frozen_saved_reviews*.json',
    'recovery/explicit_continuation', 'recovery/task_explicit_continuation',
    'recovery/completion_text_review*', 'recovery/source_adjudication*',
    'recovery/bounded_adjudication', 'recovery/tagesschau*',
    'recovery/user_context_adoption*', 'recovery/user_editorial*',
    'tts/explicit_voice_seed43', 'tts/native_generation_failure',
    'tts/numeric_pronunciation', 'tts/user_voice_adoption',
    'tts/user_voice_risk*',
)
ROOT_PATTERNS = (
    'reports/start_*/numeric_pronunciation/*reply*.json',
    'reports/tagesschau_daily/*/three_voice_decision_*/*reply*.json',
    'reports/tagesschau_daily/*/native_nonfinite_recovery_*/policy.json',
)

def _belongs_to_job(value, job):
    if isinstance(value, dict):
        return any(value.get(key)==job for key in ('job','job_id')) or any(
            _belongs_to_job(item,job) for item in value.values())
    return isinstance(value,list) and any(_belongs_to_job(item,job) for item in value)

def check_context(*args, **kwargs):
    """Read-only detection; a present unsupported receipt is never ignored."""
    folders=set()
    for value in (*args, *kwargs.values()):
        if not isinstance(value,(str,Path)):continue
        path=Path(value)
        for candidate in (path,*path.parents):
            if candidate.parent.name=='jobs':folders.add(candidate);break
    for folder in folders:
        for pattern in PATTERNS:
            if any(folder.glob(pattern)):
                raise PrivateScopeUnavailable('Private source-specific credentials require a separately audited local integration')
        root=folder.parent.parent
        for pattern in ROOT_PATTERNS:
            for path in root.glob(pattern):
                if path.is_symlink():
                    raise PrivateScopeUnavailable('Private root credentials must not be symlinks')
                if not path.is_file():continue
                try:value=json.loads(path.read_text())
                except (OSError,ValueError):continue
                if _belongs_to_job(value,folder.name):
                    raise PrivateScopeUnavailable('Private source-specific root credentials require a separately audited local integration')
        settings=folder.parent.parent/'config/source_scopes.local.json'
        if settings.is_symlink():raise PrivateScopeUnavailable('Local scope settings must not be a symlink')
        if settings.is_file():
            try:value=json.loads(settings.read_text())
            except (OSError,ValueError) as error:raise PrivateScopeUnavailable('Invalid local scope settings') from error
            allowed={'schema','picture_holds','native_audio_selection','review_compilation'}
            if not isinstance(value,dict) or value.get('schema')!='local_source_scopes_v1' or set(value)-allowed:
                raise PrivateScopeUnavailable('Unregistered local source-specific scope')

def deny(operation,*args,**kwargs):
    check_context(*args,**kwargs)
    raise PrivateScopeUnavailable('Private source-specific operation is unavailable in the public source build: '+operation)
