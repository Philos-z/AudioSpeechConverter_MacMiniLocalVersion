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
SCHEMA = 'unsupported_private_scope_tagesschau_six_commit_20261105_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def verified(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.verified', *args, **kwargs)

def approval_epoch(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.approval_epoch', *args, **kwargs)

def active(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

def historical_bundle(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.historical_bundle', *args, **kwargs)

def hint(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.hint', *args, **kwargs)

def expected(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.expected', *args, **kwargs)

def records(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.records', *args, **kwargs)

def later(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.later', *args, **kwargs)

def transaction(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.transaction', *args, **kwargs)

def commit(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.commit', *args, **kwargs)

def state(*args, **kwargs):
    return deny('tagesschau_six_commit_20261105.state', *args, **kwargs)

def load_targets(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def load_adoptions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def historical_adoptions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def source_review_context(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

def pending_after_commit(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

if __name__ == "__main__":
    deny('tagesschau_six_commit_20261105.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
