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
SCHEMA = 'unsupported_private_scope_user_context_adoption_20261028_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _require(*args, **kwargs):
    return deny('user_context_adoption_20261028._require', *args, **kwargs)

def _text_sha(*args, **kwargs):
    return deny('user_context_adoption_20261028._text_sha', *args, **kwargs)

def _ref(*args, **kwargs):
    return deny('user_context_adoption_20261028._ref', *args, **kwargs)

def _read_ref(*args, **kwargs):
    return deny('user_context_adoption_20261028._read_ref', *args, **kwargs)

def _context(*args, **kwargs):
    return deny('user_context_adoption_20261028._context', *args, **kwargs)

def _identity(*args, **kwargs):
    return deny('user_context_adoption_20261028._identity', *args, **kwargs)

def _machine_evidence(*args, **kwargs):
    return deny('user_context_adoption_20261028._machine_evidence', *args, **kwargs)

def _report_row(*args, **kwargs):
    return deny('user_context_adoption_20261028._report_row', *args, **kwargs)

def _validate(*args, **kwargs):
    return deny('user_context_adoption_20261028._validate', *args, **kwargs)

def _hint(*args, **kwargs):
    return deny('user_context_adoption_20261028._hint', *args, **kwargs)

def _load_primary_adoptions(*args, **kwargs):
    return deny('user_context_adoption_20261028._load_primary_adoptions', *args, **kwargs)

def load_adoptions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def register(*args, **kwargs):
    return deny('user_context_adoption_20261028.register', *args, **kwargs)

if __name__ == "__main__":
    deny('user_context_adoption_20261028.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
