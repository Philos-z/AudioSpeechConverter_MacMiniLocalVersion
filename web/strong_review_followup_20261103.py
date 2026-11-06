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
SCHEMA = 'unsupported_private_scope_strong_review_followup_20261103_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _bundle(*args, **kwargs):
    return deny('strong_review_followup_20261103._bundle', *args, **kwargs)

def _context_matches(*args, **kwargs):
    return deny('strong_review_followup_20261103._context_matches', *args, **kwargs)

def _later_whole(*args, **kwargs):
    return deny('strong_review_followup_20261103._later_whole', *args, **kwargs)

def _later_normal_ids(*args, **kwargs):
    return deny('strong_review_followup_20261103._later_normal_ids', *args, **kwargs)

def _setting(*args, **kwargs):
    return deny('strong_review_followup_20261103._setting', *args, **kwargs)

def load_ordinary_targets(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def _revision_hint(*args, **kwargs):
    return deny('strong_review_followup_20261103._revision_hint', *args, **kwargs)

def load_revisions(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def register(*args, **kwargs):
    return deny('strong_review_followup_20261103.register', *args, **kwargs)

if __name__ == "__main__":
    deny('strong_review_followup_20261103.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
