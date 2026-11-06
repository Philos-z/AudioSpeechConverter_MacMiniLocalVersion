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
SCHEMA = 'unsupported_private_scope_tagesschau_saved_reviews_20261105_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def require(*args, **kwargs):
    return deny('tagesschau_saved_reviews_20261105.require', *args, **kwargs)

def ref(*args, **kwargs):
    return deny('tagesschau_saved_reviews_20261105.ref', *args, **kwargs)

def _normal_context(*args, **kwargs):
    return deny('tagesschau_saved_reviews_20261105._normal_context', *args, **kwargs)

def _normal_reviews(*args, **kwargs):
    return deny('tagesschau_saved_reviews_20261105._normal_reviews', *args, **kwargs)

def _baseline(*args, **kwargs):
    return deny('tagesschau_saved_reviews_20261105._baseline', *args, **kwargs)

def build_bundle(*args, **kwargs):
    return deny('tagesschau_saved_reviews_20261105.build_bundle', *args, **kwargs)

def load_targets(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def validated_local_requirements(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def resolved_local_guard_ids(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

def freeze(*args, **kwargs):
    return deny('tagesschau_saved_reviews_20261105.freeze', *args, **kwargs)

if __name__ == "__main__":
    deny('tagesschau_saved_reviews_20261105.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
