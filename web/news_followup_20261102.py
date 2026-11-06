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
SCHEMA = 'unsupported_private_scope_news_followup_20261102_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def require(*args, **kwargs):
    return deny('news_followup_20261102.require', *args, **kwargs)

def identity(*args, **kwargs):
    return deny('news_followup_20261102.identity', *args, **kwargs)

def validate_ledger(*args, **kwargs):
    return deny('news_followup_20261102.validate_ledger', *args, **kwargs)

if __name__ == "__main__":
    deny('news_followup_20261102.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
