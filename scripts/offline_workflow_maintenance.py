"""Record real collection health and recover inactivity-disabled schedules only."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
import re
import time
from urllib.parse import quote

from offline_publication import GitHubError, github

WORKFLOWS = ('offline-core-refresh.yml', 'remote-data-update.yml')
CALLERS = {'.github/workflows/' + name for name in (*WORKFLOWS, 'collection-maintenance.yml')}
STATES = {'active', 'disabled_inactivity', 'disabled_manually', 'disabled_fork', 'deleted'}
OUTCOMES = {'success', 'failure', 'cancelled', 'skipped', 'not_run'}
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


def inspect_workflow(api, prefix, name, allowed, repository):
    endpoint = prefix + '/actions/workflows/' + name
    observation = {'operator_enabled': allowed, 'state': 'unknown',
                   'recovered': False, 'latest_observed_schedule': None, 'attention': None}
    try:
        workflow = api(endpoint)
        if workflow.get('path') != '.github/workflows/' + name or workflow.get('state') not in STATES:
            raise ValueError('known_workflow_identity_required')
        observation['state'] = workflow['state']
        if allowed and workflow['state'] == 'disabled_inactivity':
            api(endpoint + '/enable', method='PUT')
            verified = api(endpoint)
            if verified.get('path') != workflow['path'] or verified.get('state') != 'active':
                observation['attention'] = 'enable_not_verified'
            else:
                observation.update(state='active', recovered=True)
        runs = api(endpoint + '/runs?event=schedule&per_page=1')['workflow_runs']
        scheduled = next((run for run in runs if run.get('event') == 'schedule'), None)
        if scheduled:
            observation['latest_observed_schedule'] = run_summary(scheduled, repository)
        if allowed and observation['state'] in {'disabled_fork', 'deleted'}:
            observation['attention'] = 'workflow_unavailable'
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


def maintain(api, *, repository, run_id, run_attempt, collection_outcome, enabled, now):
    if set(enabled) != set(WORKFLOWS) or any(type(value) is not bool for value in enabled.values()):
        raise ValueError('known_workflows_and_operator_flags_required')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or
            type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0 or
            collection_outcome not in OUTCOMES or now.tzinfo is None):
        raise ValueError('explicit_maintenance_context_required')
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
              'workflows': {name: inspect_workflow(api, prefix, name, enabled[name], repository)
                            for name in WORKFLOWS}}
    try:
        health['latest_data_release'] = release_summary(api, prefix +
            '/contents/data/offline/core-forward-v1.json?ref=' + quote(branch, safe=''))
    except (GitHubError, ValueError, KeyError, TypeError) as exc:
        health['source_index_attention'] = type(exc).__name__
    health['requires_user'] = (any(value['attention'] for value in health['workflows'].values()) or
                               'source_index_attention' in health)
    committed = publish_health(api, prefix, branch, health)
    return {'status': 'collection_health_committed', 'commit': committed,
            'recovered_workflows': [name for name in WORKFLOWS if health['workflows'][name]['recovered']],
            'requires_user': health['requires_user']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gh', default='gh')
    args = parser.parse_args()
    result = maintain(lambda endpoint, **options: github(args.gh, endpoint, **options),
        repository=os.environ['GITHUB_REPOSITORY'], run_id=int(os.environ['GITHUB_RUN_ID']),
        run_attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),
        collection_outcome=os.environ.get('COLLECTION_OUTCOME', 'not_run'),
        enabled={WORKFLOWS[0]: os.environ.get('CORE_COLLECTION_ALLOWED') == 'true',
                 WORKFLOWS[1]: os.environ.get('KRX_COLLECTION_ALLOWED') == 'true'},
        now=datetime.now(timezone.utc))
    print(json.dumps(result))
    return 2 if result['requires_user'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'collection_maintenance_failed', 'error_type': type(exc).__name__,
                          **(exc.safe_details() if isinstance(exc, GitHubError) else {})}))
        raise SystemExit(2)
