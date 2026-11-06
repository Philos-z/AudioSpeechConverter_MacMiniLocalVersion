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
SCHEMA = 'unsupported_private_scope_task_explicit_continuation_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _scope(*args, **kwargs):
    return deny('task_explicit_continuation._scope', *args, **kwargs)

def _supported(*args, **kwargs):
    return deny('task_explicit_continuation._supported', *args, **kwargs)

def _current_rows(*args, **kwargs):
    return deny('task_explicit_continuation._current_rows', *args, **kwargs)

def _require(*args, **kwargs):
    return deny('task_explicit_continuation._require', *args, **kwargs)

def _identity(*args, **kwargs):
    return deny('task_explicit_continuation._identity', *args, **kwargs)

def _ledger(*args, **kwargs):
    return deny('task_explicit_continuation._ledger', *args, **kwargs)

def _all_entries(*args, **kwargs):
    return deny('task_explicit_continuation._all_entries', *args, **kwargs)

def _publish(*args, **kwargs):
    return deny('task_explicit_continuation._publish', *args, **kwargs)

def _snapshot(*args, **kwargs):
    return deny('task_explicit_continuation._snapshot', *args, **kwargs)

def _model_digest(*args, **kwargs):
    return deny('task_explicit_continuation._model_digest', *args, **kwargs)

def _validate_raw_origin(*args, **kwargs):
    return deny('task_explicit_continuation._validate_raw_origin', *args, **kwargs)

def _items(*args, **kwargs):
    return deny('task_explicit_continuation._items', *args, **kwargs)

def _whole_diagnostics(*args, **kwargs):
    return deny('task_explicit_continuation._whole_diagnostics', *args, **kwargs)

def _freeze_reports(*args, **kwargs):
    return deny('task_explicit_continuation._freeze_reports', *args, **kwargs)

def _prompts(*args, **kwargs):
    return deny('task_explicit_continuation._prompts', *args, **kwargs)

def _task_prompts(*args, **kwargs):
    return deny('task_explicit_continuation._task_prompts', *args, **kwargs)

def _code_identity(*args, **kwargs):
    return deny('task_explicit_continuation._code_identity', *args, **kwargs)

def _task_code_identity(*args, **kwargs):
    return deny('task_explicit_continuation._task_code_identity', *args, **kwargs)

def preflight(*args, **kwargs):
    return deny('task_explicit_continuation.preflight', *args, **kwargs)

def run(*args, **kwargs):
    return deny('task_explicit_continuation.run', *args, **kwargs)

def validate_receipt(*args, **kwargs):
    return deny('task_explicit_continuation.validate_receipt', *args, **kwargs)

def _entry(*args, **kwargs):
    return deny('task_explicit_continuation._entry', *args, **kwargs)

def _save_once(*args, **kwargs):
    return deny('task_explicit_continuation._save_once', *args, **kwargs)

def _committed_hints(*args, **kwargs):
    return deny('task_explicit_continuation._committed_hints', *args, **kwargs)

def _finish_transaction(*args, **kwargs):
    return deny('task_explicit_continuation._finish_transaction', *args, **kwargs)

def commit(*args, **kwargs):
    return deny('task_explicit_continuation.commit', *args, **kwargs)

def _later_rejection(*args, **kwargs):
    return deny('task_explicit_continuation._later_rejection', *args, **kwargs)

def _resolved(*args, **kwargs):
    return deny('task_explicit_continuation._resolved', *args, **kwargs)

def resolved_source_ids(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

def committed_targets(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def source_review_context(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

if __name__ == "__main__":
    deny('task_explicit_continuation.cli')
