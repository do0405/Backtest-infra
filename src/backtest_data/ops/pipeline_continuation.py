"""Re-use explicitly finished enrichment stages without changing their scope."""
from .continuation_manager import read_json

STAGE_STATUSES={'schema_tail':{'schema_ready'},'coverage':{'audit_complete'},
    'sec_metadata':{'complete'},'financial_completion':{'complete','complete_with_deferred_documents'},
    'price_release':{'sealed'},'price_query_verification':{'verified'}}


def completed_stage(domain,stage):
    try:
        runtime=read_json(stage['runtime']);result=read_json(stage['stdout'])
    except (KeyError,OSError,ValueError):return False
    if not runtime.get('finished_at'):return False
    if domain=='sec_metadata' and runtime.get('exit_code')==2 and result.get('status')=='blocked':
        return str(result.get('reason','')).startswith('sec_source_')
    return runtime.get('exit_code')==0 and result.get('status') in STAGE_STATUSES.get(domain,set())
