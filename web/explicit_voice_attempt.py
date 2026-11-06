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
SCHEMA = 'unsupported_private_scope_explicit_voice_attempt_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _ref(*args, **kwargs):
    return deny('explicit_voice_attempt._ref', *args, **kwargs)

def seed43_bytes(*args, **kwargs):
    return deny('explicit_voice_attempt.seed43_bytes', *args, **kwargs)

def runner_ref(*args, **kwargs):
    return deny('explicit_voice_attempt.runner_ref', *args, **kwargs)

def context(*args, **kwargs):
    return deny('explicit_voice_attempt.context', *args, **kwargs)

def _decisions(*args, **kwargs):
    return deny('explicit_voice_attempt._decisions', *args, **kwargs)

def current_context(*args, **kwargs):
    return deny('explicit_voice_attempt.current_context', *args, **kwargs)

def record_decision(*args, **kwargs):
    return deny('explicit_voice_attempt.record_decision', *args, **kwargs)

def validate(*args, **kwargs):
    return deny('explicit_voice_attempt.validate', *args, **kwargs)

if __name__ == "__main__":
    deny('explicit_voice_attempt.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
