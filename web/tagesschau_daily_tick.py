"""GET-only daily queue tick. Reconcile writes derived summaries, never jobs."""
import argparse
import json
from pathlib import Path

import tagesschau_daily_state as state
from task_takeover import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=['observe', 'plan', 'reconcile'], default='plan')
    parser.add_argument('--observation', type=Path,
        help='Saved observation for offline plan/observe only; reconcile requires fresh local GET observations')
    parser.add_argument('--evidence-dir', type=Path,
        help='Optional new directory inside root/reports; contains compact observations and plan')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    applied = {'status': 'planned', 'changed': False}
    try:
        state.require(not (args.observation and args.mode == 'reconcile'),
            'offline_reconcile_not_supported')
        current, binding = state.snapshot(root)
        if args.observation:
            observation, _ = state.read_file(root, args.observation)
            state.require(observation.get('binding') == binding, 'offline_observation_binding_mismatch')
            state._replay_observation(root, current, observation)
        else:
            observation = state.observe(root, current, binding)
        result = state.plan(current, observation)
        directory = None
        if args.evidence_dir:
            directory = state.confined(root, args.evidence_dir)
            state.require(directory.is_relative_to(root / 'reports') and not directory.exists(),
                'evidence_directory_must_be_new_and_inside_reports')
            directory.mkdir(parents=True)
            # Persist plan/evidence before any queue commit; only the result follows it.
            write_json(directory / 'observation.json', observation)
            write_json(directory / 'plan.json', result)
        if args.mode == 'reconcile':
            applied = state.apply_projection(root, result)
        elif args.mode == 'observe':
            applied['status'] = 'observed'
        if directory:
            write_json(directory / 'result.json', applied)
        projection = result['allowed_patch']['projection']
        print(json.dumps({'schema': state.PLAN_SCHEMA, 'mode': args.mode, **applied,
            'generation_observed': binding['generation'], 'next_action': result['allowed_patch']['next_action'],
            'active_decisions': projection['active_decisions'], 'conflicts': projection['conflicts'],
            'external_mutations_performed': False, 'executor_available': False}, ensure_ascii=False))
        return 2 if applied['status'] in {'conflict', 'busy'} else 0
    except (state.StateError, OSError, ValueError, TypeError, KeyError):
        if applied.get('changed'):
            print(json.dumps({**applied, 'receipt_error': 'post_commit_receipt_write_failed',
                'external_mutations_performed': False, 'executor_available': False}))
            return 3
        print(json.dumps({'status': 'rejected', 'reason': 'invalid_changed_or_unbound_tick_input',
            'changed': False, 'external_mutations_performed': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
