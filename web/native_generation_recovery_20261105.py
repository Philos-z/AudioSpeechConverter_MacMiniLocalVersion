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
SCHEMA = 'unsupported_private_scope_native_generation_recovery_20261105_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def argv_for(*args, **kwargs):
    return deny('native_generation_recovery_20261105.argv_for', *args, **kwargs)

def enabled(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

def context(*args, **kwargs):
    return deny('native_generation_recovery_20261105.context', *args, **kwargs)

def once(*args, **kwargs):
    return deny('native_generation_recovery_20261105.once', *args, **kwargs)

def request_path(*args, **kwargs):
    return deny('native_generation_recovery_20261105.request_path', *args, **kwargs)

def prepare(*args, **kwargs):
    return deny('native_generation_recovery_20261105.prepare', *args, **kwargs)

def check_request(*args, **kwargs):
    return deny('native_generation_recovery_20261105.check_request', *args, **kwargs)

def complete(*args, **kwargs):
    return deny('native_generation_recovery_20261105.complete', *args, **kwargs)

def launch(*args, **kwargs):
    return deny('native_generation_recovery_20261105.launch', *args, **kwargs)

def build(*args, **kwargs):
    return deny('native_generation_recovery_20261105.build', *args, **kwargs)

def finish(*args, **kwargs):
    return deny('native_generation_recovery_20261105.finish', *args, **kwargs)

def validate_manifest(*args, **kwargs):
    return deny('native_generation_recovery_20261105.validate_manifest', *args, **kwargs)

if __name__ == "__main__":
    deny('native_generation_recovery_20261105.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
