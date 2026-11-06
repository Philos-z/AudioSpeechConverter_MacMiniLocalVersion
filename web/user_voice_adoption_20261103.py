"""Public fail-closed adapter; private task registrations are not shipped."""
from portable_private_hooks import check_context, deny, PrivateScopeUnavailable
JID = JOB = JOB_ID = 'unregistered_private_scope'
SPECS = {}
TASK_SCOPES = {}
SCOPE = IDS = PRIOR_TOKENS = ()
CONFIG = 'translation/unsupported_private_scope.json'
BASE = 'recovery/unsupported_private_scope'
LEDGER = BASE+'/ledger.json'
DIRECTORY = 'tts/unsupported_private_scope'
MANIFEST = DIRECTORY+'/manifest.json'
RUNNER = 'scripts/unsupported_private_scope.py'
FAILED = 'unregistered_private_group'
SCHEMA = 'unsupported_private_scope_user_voice_adoption_20261103_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _authority(*args, **kwargs):
    return deny('user_voice_adoption_20261103._authority', *args, **kwargs)

def _write_once(*args, **kwargs):
    return deny('user_voice_adoption_20261103._write_once', *args, **kwargs)

def register(*args, **kwargs):
    return deny('user_voice_adoption_20261103.register', *args, **kwargs)

def resolve(root, folder, plan_path, base_path):
    """Keep the exact ordinary manifest path; private selections stay refused."""
    from pathlib import Path
    check_context(root, folder, plan_path, base_path)
    return Path(base_path)

def validate_manifest(*args, **kwargs):
    return deny('user_voice_adoption_20261103.validate_manifest', *args, **kwargs)

def pronunciation_matches(group, content, audio_sha256, *, caller_job=None, plan_path=None, manifest_path=None):
    check_context(plan_path, manifest_path)
    expected = group.get("tts_pronunciation_text") or group["text_zh"]
    if content.get("text_zh") == group["text_zh"] and content.get("effective_tts_text") == expected:
        return True
    return deny("pronunciation_exception", plan_path, manifest_path)

def accept_quality(*args, **kwargs):
    check_context(*args, **kwargs)
    return None

if __name__ == "__main__":
    deny('user_voice_adoption_20261103.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
