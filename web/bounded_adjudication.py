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
SCHEMA = 'unsupported_private_scope_bounded_adjudication_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def engine(*args, **kwargs):
    return deny('bounded_adjudication.engine', *args, **kwargs)

def preflight(*args, **kwargs):
    return deny('bounded_adjudication.preflight', *args, **kwargs)

def run(*args, **kwargs):
    return deny('bounded_adjudication.run', *args, **kwargs)

def validate_receipt(*args, **kwargs):
    return deny('bounded_adjudication.validate_receipt', *args, **kwargs)

def commit(*args, **kwargs):
    return deny('bounded_adjudication.commit', *args, **kwargs)

def resolved_source_ids(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

def pending_required_ids(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

def source_review_context(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def explicit_whole_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

if __name__ == "__main__":
    deny('bounded_adjudication.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
