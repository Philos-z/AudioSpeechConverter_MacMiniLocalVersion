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
SCHEMA = 'unsupported_private_scope_source_adjudication_20261028_v1'
ContinuationError = ContinuationBudgetExhausted = PrivateScopeUnavailable

def _require(*args, **kwargs):
    return deny('source_adjudication_20261028._require', *args, **kwargs)

def _identity(*args, **kwargs):
    return deny('source_adjudication_20261028._identity', *args, **kwargs)

def _ledger(*args, **kwargs):
    return deny('source_adjudication_20261028._ledger', *args, **kwargs)

def _publish(*args, **kwargs):
    return deny('source_adjudication_20261028._publish', *args, **kwargs)

def _snapshot(*args, **kwargs):
    return deny('source_adjudication_20261028._snapshot', *args, **kwargs)

def _model_digest(*args, **kwargs):
    return deny('source_adjudication_20261028._model_digest', *args, **kwargs)

def _validate_raw_origin(*args, **kwargs):
    return deny('source_adjudication_20261028._validate_raw_origin', *args, **kwargs)

def _native_source(*args, **kwargs):
    return deny('source_adjudication_20261028._native_source', *args, **kwargs)

def _items(*args, **kwargs):
    return deny('source_adjudication_20261028._items', *args, **kwargs)

def _whole_diagnostics(*args, **kwargs):
    return deny('source_adjudication_20261028._whole_diagnostics', *args, **kwargs)

def _freeze_reports(*args, **kwargs):
    return deny('source_adjudication_20261028._freeze_reports', *args, **kwargs)

def _prompts(*args, **kwargs):
    return deny('source_adjudication_20261028._prompts', *args, **kwargs)

def _code_identity(*args, **kwargs):
    return deny('source_adjudication_20261028._code_identity', *args, **kwargs)

def _source_findings(*args, **kwargs):
    return deny('source_adjudication_20261028._source_findings', *args, **kwargs)

def preflight(*args, **kwargs):
    return deny('source_adjudication_20261028.preflight', *args, **kwargs)

def run(*args, **kwargs):
    return deny('source_adjudication_20261028.run', *args, **kwargs)

def validate_receipt(*args, **kwargs):
    return deny('source_adjudication_20261028.validate_receipt', *args, **kwargs)

def _entry(*args, **kwargs):
    return deny('source_adjudication_20261028._entry', *args, **kwargs)

def _save_once(*args, **kwargs):
    return deny('source_adjudication_20261028._save_once', *args, **kwargs)

def _committed_hints(*args, **kwargs):
    return deny('source_adjudication_20261028._committed_hints', *args, **kwargs)

def _finish_transaction(*args, **kwargs):
    return deny('source_adjudication_20261028._finish_transaction', *args, **kwargs)

def commit(*args, **kwargs):
    return deny('source_adjudication_20261028.commit', *args, **kwargs)

def _later_rejection(*args, **kwargs):
    return deny('source_adjudication_20261028._later_rejection', *args, **kwargs)

def _resolved(*args, **kwargs):
    return deny('source_adjudication_20261028._resolved', *args, **kwargs)

def resolved_source_ids(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

def autonomous_diagnostic_resolved(*args, **kwargs):
    check_context(*args, **kwargs)
    return False

def source_review_context(*args, **kwargs):
    check_context(*args, **kwargs)
    return {}

def pending_required_ids(*args, **kwargs):
    check_context(*args, **kwargs)
    return []

if __name__ == "__main__":
    deny('source_adjudication_20261028.cli')
