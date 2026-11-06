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
SCHEMA = 'unsupported_private_scope_user_editorial_selection_20261103_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _selection(*args, **kwargs):
    return deny('user_editorial_selection_20261103._selection', *args, **kwargs)

def _context(*args, **kwargs):
    return deny('user_editorial_selection_20261103._context', *args, **kwargs)

def _decision(*args, **kwargs):
    return deny('user_editorial_selection_20261103._decision', *args, **kwargs)

def _hint(*args, **kwargs):
    return deny('user_editorial_selection_20261103._hint', *args, **kwargs)

def _later(*args, **kwargs):
    return deny('user_editorial_selection_20261103._later', *args, **kwargs)

def _validate(*args, **kwargs):
    return deny('user_editorial_selection_20261103._validate', *args, **kwargs)

def load_adoptions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def historical_omission_covers(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

def register(*args, **kwargs):
    return deny('user_editorial_selection_20261103.register', *args, **kwargs)

if __name__ == "__main__":
    deny('user_editorial_selection_20261103.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
