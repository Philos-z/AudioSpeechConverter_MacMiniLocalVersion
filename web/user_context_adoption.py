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
SCHEMA = 'unsupported_private_scope_user_context_adoption_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def text_sha(*args, **kwargs):
    return deny('user_context_adoption.text_sha', *args, **kwargs)

def _ref(*args, **kwargs):
    return deny('user_context_adoption._ref', *args, **kwargs)

def load_adoptions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def _load_legacy_adoptions(*args, **kwargs):
    return deny('user_context_adoption._load_legacy_adoptions', *args, **kwargs)

if __name__ == "__main__":
    deny('user_context_adoption.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
