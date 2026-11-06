"""Private bounded approval registries are not distributed in public builds.
Ordinary cache and saved-review validation remain in translation_reuse and
translation_recovery. Presence of an unsupported bounded receipt is an error.
"""
from portable_private_hooks import check_context
METHOD = 'saved_independent_proxy_receipt_and_numeric_checks'
def load_targets(root, folder, rows):
    check_context(root, folder)
    return {}
