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
SCHEMA = 'unsupported_private_scope_explicit_continuation_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _hash(*args, **kwargs):
    return deny('explicit_continuation._hash', *args, **kwargs)

def _text_hash(*args, **kwargs):
    return deny('explicit_continuation._text_hash', *args, **kwargs)

def _ref(*args, **kwargs):
    return deny('explicit_continuation._ref', *args, **kwargs)

def _read_ref(*args, **kwargs):
    return deny('explicit_continuation._read_ref', *args, **kwargs)

def _pointer(*args, **kwargs):
    return deny('explicit_continuation._pointer', *args, **kwargs)

def _identity(*args, **kwargs):
    return deny('explicit_continuation._identity', *args, **kwargs)

def _ledger(*args, **kwargs):
    return deny('explicit_continuation._ledger', *args, **kwargs)

def _publish(*args, **kwargs):
    return deny('explicit_continuation._publish', *args, **kwargs)

def _snapshot(*args, **kwargs):
    return deny('explicit_continuation._snapshot', *args, **kwargs)

def _check_snapshot(*args, **kwargs):
    return deny('explicit_continuation._check_snapshot', *args, **kwargs)

def _proofs(*args, **kwargs):
    return deny('explicit_continuation._proofs', *args, **kwargs)

def _items(*args, **kwargs):
    return deny('explicit_continuation._items', *args, **kwargs)

def _response(*args, **kwargs):
    return deny('explicit_continuation._response', *args, **kwargs)

def _evaluate(*args, **kwargs):
    return deny('explicit_continuation._evaluate', *args, **kwargs)

def _attach_findings(*args, **kwargs):
    return deny('explicit_continuation._attach_findings', *args, **kwargs)

def _system_prompts(*args, **kwargs):
    return deny('explicit_continuation._system_prompts', *args, **kwargs)

def preflight(*args, **kwargs):
    return deny('explicit_continuation.preflight', *args, **kwargs)

def run(*args, **kwargs):
    return deny('explicit_continuation.run', *args, **kwargs)

def validate_receipt(*args, **kwargs):
    return deny('explicit_continuation.validate_receipt', *args, **kwargs)

def _validate_commit(*args, **kwargs):
    return deny('explicit_continuation._validate_commit', *args, **kwargs)

def mark_committed(*args, **kwargs):
    return deny('explicit_continuation.mark_committed', *args, **kwargs)

def _later_rejection(*args, **kwargs):
    return deny('explicit_continuation._later_rejection', *args, **kwargs)

def resolved_source_ids(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

if __name__ == "__main__":
    deny('explicit_continuation.cli')

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False
