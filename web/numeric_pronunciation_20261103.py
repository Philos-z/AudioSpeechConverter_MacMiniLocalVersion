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
SCHEMA = 'unsupported_private_scope_numeric_pronunciation_20261103_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def modules(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.modules', *args, **kwargs)

def selected_groups(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.selected_groups', *args, **kwargs)

def context(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.context', *args, **kwargs)

def write_once(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.write_once', *args, **kwargs)

def prepare(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.prepare', *args, **kwargs)

def validate_output(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.validate_output', *args, **kwargs)

def screen(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.screen', *args, **kwargs)

def execute(*args, **kwargs):
    return deny('numeric_pronunciation_20261103.execute', *args, **kwargs)

if __name__ == "__main__":
    deny('numeric_pronunciation_20261103.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
