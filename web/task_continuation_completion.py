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
SCHEMA = 'unsupported_private_scope_task_continuation_completion_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _frozen(*args, **kwargs):
    return deny('task_continuation_completion._frozen', *args, **kwargs)

def validate_budget_records(*args, **kwargs):
    return deny('task_continuation_completion.validate_budget_records', *args, **kwargs)

def _code(*args, **kwargs):
    return deny('task_continuation_completion._code', *args, **kwargs)

def _extract(*args, **kwargs):
    return deny('task_continuation_completion._extract', *args, **kwargs)

def _prepare(*args, **kwargs):
    return deny('task_continuation_completion._prepare', *args, **kwargs)

def run(*args, **kwargs):
    return deny('task_continuation_completion.run', *args, **kwargs)

def validate_receipt(*args, **kwargs):
    return deny('task_continuation_completion.validate_receipt', *args, **kwargs)

if __name__ == "__main__":
    deny('task_continuation_completion.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
