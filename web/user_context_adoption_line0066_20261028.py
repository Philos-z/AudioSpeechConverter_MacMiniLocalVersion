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
SCHEMA = 'unsupported_private_scope_user_context_adoption_line0066_20261028_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _context(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028._context', *args, **kwargs)

def _report_row(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028._report_row', *args, **kwargs)

def _pcm(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028._pcm', *args, **kwargs)

def _evidence(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028._evidence', *args, **kwargs)

def _hint(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028._hint', *args, **kwargs)

def _validate(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028._validate', *args, **kwargs)

def load_adoptions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def prepare(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028.prepare', *args, **kwargs)

def register(*args, **kwargs):
    return deny('user_context_adoption_line0066_20261028.register', *args, **kwargs)

if __name__ == "__main__":
    deny('user_context_adoption_line0066_20261028.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
