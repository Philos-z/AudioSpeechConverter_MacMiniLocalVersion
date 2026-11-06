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
SCHEMA = 'unsupported_private_scope_tagesschau_quality_tolerance_20261105_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def require(*args, **kwargs):
    return deny('tagesschau_quality_tolerance_20261105.require', *args, **kwargs)

def ref(*args, **kwargs):
    return deny('tagesschau_quality_tolerance_20261105.ref', *args, **kwargs)

def readref(*args, **kwargs):
    return deny('tagesschau_quality_tolerance_20261105.readref', *args, **kwargs)

def policy(*args, **kwargs):
    return deny('tagesschau_quality_tolerance_20261105.policy', *args, **kwargs)

def covers(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

def replay_request(*args, **kwargs):
    check_context(*args, **kwargs)
    return None

def apply(snapshot, findings, decisions, events, folder):
    check_context(folder)
    return findings, {}

if __name__ == "__main__":
    deny('tagesschau_quality_tolerance_20261105.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
