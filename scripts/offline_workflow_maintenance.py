"""Record collection health and schedule delays."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
from urllib.parse import quote

from offline_publication import GitHubError, github

WORKFLOW_FLAGS = {
    'offline-core-refresh.yml': 'CORE_COLLECTION_ALLOWED',
    'remote-data-update.yml': 'KRX_COLLECTION_ALLOWED',
    'remote-extended-refresh.yml': 'EXTRA_COLLECTION_ALLOWED',
}
WORKFLOWS = tuple(WORKFLOW_FLAGS)
SCHEDULE_MAX_AGE = timedelta(hours=4)
SCHEDULE_ALERTS = {'schedule_missing', 'schedule_stale', 'schedule_unsuccessful'}
PUBLICATION_STATES = {'committed', 'recovered', 'deferred', 'failed'}
POSTPUBLICATION_STATES = {'verified', 'deferred'}
CALLERS = {'.github/workflows/' + name for name in (*WORKFLOWS, 'collection-maintenance.yml')}
STATES = {'active', 'disabled_inactivity', 'disabled_manually', 'disabled_fork', 'deleted'}
OUTCOMES = {'success', 'failure', 'cancelled', 'skipped', 'not_run', 'deferred', 'committed_with_warnings'}
RUN_STATES = {'queued', 'in_progress', 'completed', 'waiting', 'requested', 'pending'}
CONCLUSIONS = {None, 'success', 'failure', 'neutral', 'cancelled', 'skipped', 'timed_out',
               'action_required', 'stale', 'startup_failure'}


def optional(api, endpoint):
    try:
        return api(endpoint)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise


def git_hash(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', value) is not None


def run_summary(value, repository):
    if (not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] <= 0 or
            value.get('repository', {}).get('full_name', '').lower() != repository.lower() or
            value.get('status') not in RUN_STATES or value.get('conclusion') not in CONCLUSIONS):
        raise ValueError('scheduled_run_identity_required')
    clock = datetime.fromisoformat(value['created_at'].replace('Z', '+00:00'))
    if clock.tzinfo is None:
        raise ValueError('aware_run_clock_required')
    return {key: value[key] for key in ('id', 'status', 'conclusion', 'created_at')}


def latest_schedule(api, endpoint, repository, *, completed=False):
    status = '&status=completed' if completed else ''
    runs = api(f'{endpoint}/runs?event=schedule{status}&per_page=1')['workflow_runs']
    if not runs:
        return None
    run = runs[0]
    if run.get('event') != 'schedule' or (completed and run.get('status') != 'completed'):
        raise ValueError('scheduled_run_event_required')
    return run_summary(run, repository)


def inspect_workflow(api, prefix, name, allowed, repository, now):
    endpoint = prefix + '/actions/workflows/' + name
    observation = {'operator_enabled': allowed, 'state': 'unknown',
                   'latest_observed_schedule': None,
                   'latest_completed_schedule': None, 'attention': None}
    try:
        workflow = api(endpoint)
        if workflow.get('path') != '.github/workflows/' + name or workflow.get('state') not in STATES:
            raise ValueError('known_workflow_identity_required')
        observation['state'] = workflow['state']
        scheduled = latest_schedule(api, endpoint, repository)
        completed = scheduled if scheduled and scheduled['status'] == 'completed' else (
            latest_schedule(api, endpoint, repository, completed=True) if scheduled else None)
        observation.update(latest_observed_schedule=scheduled, latest_completed_schedule=completed)
        if allowed and observation['state'] != 'disabled_manually' and observation['attention'] is None:
            if observation['state'] != 'active':
                observation['attention'] = 'workflow_unavailable'
            elif scheduled is None:
                observation['attention'] = 'schedule_missing'
            elif now - datetime.fromisoformat(scheduled['created_at'].replace('Z', '+00:00')) > SCHEDULE_MAX_AGE:
                observation['attention'] = 'schedule_stale'
            elif completed and completed['conclusion'] != 'success':
                observation['attention'] = 'schedule_unsuccessful'
    except (GitHubError, ValueError, KeyError, TypeError) as exc:
        observation['attention'] = 'workflow_inspection_failed'
        observation['error_type'] = type(exc).__name__
        if isinstance(exc, GitHubError):
            observation['http_status'] = exc.status
    return observation


def release_summary(api, endpoint):
    source = optional(api, endpoint)
    if source is None:
        return None
    if not isinstance(source.get('content'), str) or len(source['content']) > 48 * 1024:
        raise ValueError('bounded_source_index_required')
    value = json.loads(base64.b64decode(source['content']))
    if (not re.fullmatch(r'offline-core-forward-v1-\d{9}', value.get('tag', '')) or
            not re.fullmatch(r'[a-f0-9]{64}', value.get('commit_sha256', ''))):
        raise ValueError('committed_source_index_identity_required')
    return {'tag': value['tag'], 'commit_sha256': value['commit_sha256']}


def publish_health(api, prefix, branch, health, *, sleep=time.sleep):
    path = prefix + '/contents/data/offline/collection-health.json'
    content = json.dumps(health, sort_keys=True, indent=2).encode()
    if len(content) > 16 * 1024:
        raise ValueError('bounded_health_snapshot_required')
    for attempt in range(4):
        current = optional(api, path + '?ref=' + quote(branch, safe=''))
        if current and base64.b64decode(current.get('content', '')) == content:
            # A failed response may already have committed this exact snapshot.
            head = api(prefix + '/commits/' + quote(branch, safe=''))
            if not git_hash(head.get('sha')): raise ValueError('health_commit_readback_required')
            return head['sha']
        body = {'message': f"Record collection health for run {health['observed_by_run_id']}/{health['run_attempt']}",
                'branch': branch, 'content': base64.b64encode(content).decode()}
        if current is not None:
            if not git_hash(current.get('sha')):
                raise ValueError('current_health_sha_required')
            body['sha'] = current['sha']
        try:
            response = api(path, method='PUT', body=body)
        except GitHubError as exc:
            verified = optional(api, path + '?ref=' + quote(branch, safe=''))
            if verified and base64.b64decode(verified.get('content', '')) == content:
                head = api(prefix + '/commits/' + quote(branch, safe=''))
                if not git_hash(head.get('sha')): raise ValueError('health_commit_readback_required')
                return head['sha']
            if (exc.status not in {409, 422} and not exc.retryable) or attempt == 3:
                raise
            sleep(2 ** (attempt + 1))
            continue
        if not git_hash(response.get('commit', {}).get('sha')):
            raise ValueError('health_commit_readback_required')
        return response['commit']['sha']
    raise GitHubError(409)


def maintain(api, *, repository, run_id, run_attempt, collection_outcome, enabled, now,
             publication_state=None, source_index=None, pending_cleanup=None):
    if set(enabled) != set(WORKFLOWS) or any(type(value) is not bool for value in enabled.values()):
        raise ValueError('known_workflows_and_operator_flags_required')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or
            type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0 or
            collection_outcome not in OUTCOMES or now.tzinfo is None):
        raise ValueError('explicit_maintenance_context_required')
    if (publication_state not in PUBLICATION_STATES | {None}
            or source_index not in POSTPUBLICATION_STATES | {None}
            or pending_cleanup not in POSTPUBLICATION_STATES | {None}):
        raise ValueError('known_publication_states_required')
    prefix = 'repos/' + repository
    repo = api(prefix)
    branch = repo['default_branch']
    if repo.get('private') is not False or not re.fullmatch(r'[A-Za-z0-9._/-]+', branch):
        raise ValueError('public_repository_and_default_branch_required')
    caller = api(prefix + f'/actions/runs/{run_id}')
    if (caller.get('id') != run_id or caller.get('run_attempt') != run_attempt or
            caller.get('repository', {}).get('full_name', '').lower() != repository.lower() or
            caller.get('path') not in CALLERS or not git_hash(caller.get('head_sha')) or
            caller.get('event') not in {'schedule', 'workflow_dispatch', 'workflow_run'}):
        raise ValueError('calling_run_identity_required')
    health = {'format': 'collection_health_v1', 'checked_at': now.astimezone(timezone.utc).isoformat(),
              'observed_by_run_id': run_id, 'run_attempt': run_attempt, 'event': caller['event'],
              'head_sha': caller['head_sha'], 'collection_outcome': collection_outcome,
              'coverage_complete': False, 'latest_data_release': None,
              'workflows': {name: inspect_workflow(api, prefix, name, enabled[name], repository, now)
                            for name in WORKFLOWS}}
    if publication_state is not None:
        health['core_publication'] = {
            'state': publication_state, 'source_index': source_index, 'pending_cleanup': pending_cleanup,
        }
    try:
        health['latest_data_release'] = release_summary(api, prefix +
            '/contents/data/offline/core-forward-v1.json?ref=' + quote(branch, safe=''))
    except (GitHubError, ValueError, KeyError, TypeError) as exc:
        health['source_index_attention'] = type(exc).__name__
    attention = {name: value['attention'] for name, value in health['workflows'].items() if value['attention']}
    inspection_failed = ('source_index_attention' in health
                         or any(value not in SCHEDULE_ALERTS for value in attention.values()))
    health['requires_user'] = bool(attention or inspection_failed or collection_outcome == 'committed_with_warnings')
    committed = publish_health(api, prefix, branch, health)
    return {'status': 'collection_health_committed', 'commit': committed,
                'attention': attention, 'requires_user': health['requires_user'], 'inspection_failed': inspection_failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gh', default='gh')
    args = parser.parse_args()
    result = maintain(lambda endpoint, **options: github(args.gh, endpoint, **options),
        repository=os.environ['GITHUB_REPOSITORY'], run_id=int(os.environ['GITHUB_RUN_ID']),
        run_attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),
        collection_outcome=os.environ.get('COLLECTION_OUTCOME', 'not_run'),
        enabled={name: os.environ.get(flag) == 'true' for name, flag in WORKFLOW_FLAGS.items()},
        now=datetime.now(timezone.utc),
        publication_state=os.environ.get('CORE_PUBLICATION_STATE') or None,
        source_index=os.environ.get('CORE_SOURCE_INDEX') or None,
        pending_cleanup=os.environ.get('CORE_PENDING_CLEANUP') or None)
    print(json.dumps(result))
    for name, reason in result['attention'].items():
        print(f'::warning::{name}: {reason}')
    return 2 if result['inspection_failed'] else 0


def report_failure(exc):
    deferred = isinstance(exc, GitHubError) and exc.retryable
    print(json.dumps({'status': 'collection_maintenance_deferred' if deferred else 'collection_maintenance_failed',
                      'error_type': type(exc).__name__,
                      **(exc.safe_details() if isinstance(exc, GitHubError) else {})}))
    return 0 if deferred else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(report_failure(exc))
