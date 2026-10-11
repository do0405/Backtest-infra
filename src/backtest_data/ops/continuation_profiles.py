"""Reuse the current worker commands, roots and disjoint source groups."""
from dataclasses import dataclass
from pathlib import Path

ROLES=('clean_completion','prices','fundamentals','alpaca_prices','krx_quotes','public_pit',
    'daily_publication','supplier_prices','vwap_inputs','supplemental_sources','metric_evaluation')
SOURCES={'prices':('kis','kis_overseas'),'fundamentals':('opendart','sec'),
    'alpaca_prices':('alpaca',),'krx_quotes':('krx',)}
DOMAINS={'prices':('prices',),'alpaca_prices':('prices',),'krx_quotes':('market_quotes',),
    'fundamentals':('fundamentals','issuer_discovery','filing_sql','filing_json')}


@dataclass(frozen=True)
class Profile:
    role:str
    root:Path
    wrapper:Path
    flags:tuple


def profile(workspace,role,entry=None):
    workspace=Path(workspace);art=workspace/'artifacts';entry=entry or {}
    root='collection-watch';wrapper='operator-resume-20261010/background-collector-worker.py';flags=('--role',role)
    if role in ('clean_completion','metric_evaluation'):
        wrapper='clean-completion/worker-runtime.py';flags=('--domain',role)
    elif role=='public_pit':
        root='public-pit';wrapper='public-pit/worker-runtime.py'
        flags=('--audit-path',entry.get('audit_path',str(art/'public-pit/audit-20261009.json')),
            '--progress-path',entry.get('progress_path',str(art/'public-pit/progress-20261009.json')))
    elif role in ('daily_publication','supplier_prices'):
        root='daily-publication' if role=='daily_publication' else 'supplier-prices'
        wrapper='daily-publication/worker-runtime.py';flags=('--kind',role)
    elif role=='vwap_inputs':root='vwap-inputs';wrapper='vwap-inputs/worker-runtime.py';flags=()
    elif role=='supplemental_sources':root='supplemental';wrapper='supplemental/worker-runtime.py';flags=()
    elif role not in SOURCES:raise ValueError('unknown_continuation_role')
    return Profile(role,art/root,art/wrapper,tuple(flags))


def section(role):return 'results' if role in ('prices','fundamentals') else 'auxiliary'
