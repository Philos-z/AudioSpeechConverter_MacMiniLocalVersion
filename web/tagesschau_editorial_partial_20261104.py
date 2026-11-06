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
SCHEMA = 'unsupported_private_scope_tagesschau_editorial_partial_20261104_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def path(*args, **kwargs):
    return deny('tagesschau_editorial_partial_20261104.path', *args, **kwargs)

def checked(*args, **kwargs):
    return deny('tagesschau_editorial_partial_20261104.checked', *args, **kwargs)

def decision(*args, **kwargs):
    return deny('tagesschau_editorial_partial_20261104.decision', *args, **kwargs)

def hint(*args, **kwargs):
    return deny('tagesschau_editorial_partial_20261104.hint', *args, **kwargs)

def load_adoptions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def adoption_invariant_errors(*args, **kwargs):
    return deny('tagesschau_editorial_partial_20261104.adoption_invariant_errors', *args, **kwargs)

def commit(*args, **kwargs):
    return deny('tagesschau_editorial_partial_20261104.commit', *args, **kwargs)

if __name__ == "__main__":
    deny('tagesschau_editorial_partial_20261104.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
