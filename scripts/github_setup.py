"""Register existing provider credentials through protected stdin; never print them."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def gh_path():
    value = shutil.which('gh')
    if value:
        return value
    local = ROOT / 'artifacts/github-setup/cli/bin/gh.exe'
    if local.is_file():
        return str(local)
    raise ValueError('GitHub CLI is required; authenticate with gh auth login')


def configured_secrets(credentials):
    fields = {'KIS_APP_KEY': 'kis_app_key', 'KIS_APP_SECRET': 'kis_app_secret',
              'OPENDART_API_KEY': 'opendart_api_key', 'APCA_API_KEY_ID': 'alpaca_key',
              'APCA_API_SECRET_KEY': 'alpaca_secret', 'KRX_API_KEY': 'krx_api_key',
              'ALPHA_VANTAGE_API_KEY': 'alpha_vantage_api_key', 'BACKTEST_SEC_USER_AGENT': 'sec_user_agent'}
    result = {}
    for name, field in fields.items():
        value = getattr(credentials, field)
        if value:
            result[name] = value.reveal() if hasattr(value, 'reveal') else value
    return result


def set_secret(gh, repository, name, value, *, run=subprocess.run):
    if not re.fullmatch('[A-Z][A-Z0-9_]{0,99}', name):
        raise ValueError('invalid Secret name')
    # GitHub CLI encrypts to the repository's public key. Plaintext is supplied
    # only through stdin, never arguments, files, JSON output or diagnostic logs.
    response = run([gh, 'secret', 'set', name, '--repo', repository], input=value,
                   text=True, capture_output=True, timeout=60)
    return {'name': name, 'registered': response.returncode == 0,
            'error': None if response.returncode == 0 else 'GitHub Secret registration failed'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--register-secrets', action='store_true')
    parser.add_argument('--configure-variables', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
        raise ValueError('invalid GitHub repository')
    gh = gh_path()
    profile = subprocess.run([gh, 'api', 'user', '--jq', '{login:.login}'], capture_output=True,
                             text=True, timeout=30, check=True)
    repo = subprocess.run([gh, 'repo', 'view', args.repository, '--json',
                           'nameWithOwner,isPrivate,viewerPermission'], capture_output=True,
                          text=True, timeout=30, check=True)
    info = json.loads(repo.stdout)
    if info['nameWithOwner'].lower() != args.repository.lower() or info['viewerPermission'] not in ('ADMIN', 'MAINTAIN', 'WRITE'):
        raise ValueError('repository write permission required')
    from backtest_data.config import Settings
    from backtest_data.credentials import load_credentials
    Settings.from_env()  # Read local dotenv through the existing configuration path.
    values = configured_secrets(load_credentials())
    output = {'repository': args.repository, 'login': json.loads(profile.stdout)['login'],
              'public': not info['isPrivate'], 'configured_secret_names': sorted(values),
              'registered': [], 'remote_collection_activated': False}
    if args.register_secrets:
        for name, value in values.items():
            output['registered'].append(set_secret(gh, args.repository, name, value))
        if not all(x['registered'] for x in output['registered']):
            print(json.dumps(output))
            return 2
    if args.configure_variables:
        variables = {'REMOTE_PIPELINE_ENABLED': 'false', 'REMOTE_FREE_BUDGET_APPROVED': 'true',
                     'REMOTE_IDENTITY_HANDOFF_READY': 'false', 'REMOTE_DATA_PUBLIC_ALLOWED': 'true',
                     'REMOTE_PRIVATE_STORAGE_CAP_BYTES': str(128 * 1024 * 1024)}
        output['configured_variables'] = []
        for name, value in variables.items():
            p = subprocess.run([gh, 'variable', 'set', name, '--repo', args.repository, '--body', value],
                               capture_output=True, text=True, timeout=30)
            if p.returncode:
                raise RuntimeError('GitHub variable registration failed')
            output['configured_variables'].append(name)
    print(json.dumps(output))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Captured API responses and subprocess errors can contain credentials.
        print(json.dumps({'status': 'setup_failed', 'error_type': type(exc).__name__}))
        raise SystemExit(2)
