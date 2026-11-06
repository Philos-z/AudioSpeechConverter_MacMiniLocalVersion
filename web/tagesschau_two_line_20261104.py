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
SCHEMA = 'unsupported_private_scope_tagesschau_two_line_20261104_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def p(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.p', *args, **kwargs)

def require(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.require', *args, **kwargs)

def ref(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.ref', *args, **kwargs)

def read_ref(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.read_ref', *args, **kwargs)

def tsha(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.tsha', *args, **kwargs)

def identity(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.identity', *args, **kwargs)

def source_proof(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.source_proof', *args, **kwargs)

def prepared(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.prepared', *args, **kwargs)

def payload(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.payload', *args, **kwargs)

def response(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.response', *args, **kwargs)

def run(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.run', *args, **kwargs)

def verified(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.verified', *args, **kwargs)

def later(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.later', *args, **kwargs)

def expected_outputs(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.expected_outputs', *args, **kwargs)

def transaction(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.transaction', *args, **kwargs)

def commit(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.commit', *args, **kwargs)

def compatibility_changes(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.compatibility_changes', *args, **kwargs)

def load_targets(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def source_review_context(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def main(*args, **kwargs):
    return deny('tagesschau_two_line_20261104.main', *args, **kwargs)

if __name__ == "__main__":
    deny('tagesschau_two_line_20261104.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
