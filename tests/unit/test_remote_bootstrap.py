from types import SimpleNamespace
import pytest


def full_target():
    members=[{'security_id':f'original-{index}','market':'KR' if index<4507 else 'US','symbol':str(index),
              'identifier':f'source-{index}'} for index in range(17758)]
    return SimpleNamespace(id='actual-requested-manifest',content_hash='a'*64,members=members,
        scope={'output_start':'2016-01-01','output_end':'2026-10-07','directory_evidence':[{'raw_id':'directory-raw'}]})


def test_bootstrap_keeps_every_original_member_and_actual_manifest_identity():
    from backtest_data.remote.bootstrap import bootstrap_binding
    target=full_target();scope,ids=bootstrap_binding(target)
    assert scope['manifest_id']=='actual-requested-manifest'
    assert scope['target_count']==17758
    assert scope['market_target_counts']=={'KR':4507,'US':13251}
    assert set(ids)=={row['security_id'] for row in target.members}


def test_bootstrap_refuses_partial_duplicate_or_changed_backfill_target():
    from backtest_data.remote.bootstrap import bootstrap_binding
    target=full_target();target.members=target.members[:-1]
    with pytest.raises(ValueError,match='whole'):bootstrap_binding(target)
    target=full_target();target.members[-1]['security_id']=target.members[-2]['security_id']
    with pytest.raises(ValueError,match='duplicate'):bootstrap_binding(target)
    target=full_target();target.scope['output_start']='2020-01-01'
    with pytest.raises(ValueError,match='original'):bootstrap_binding(target)


def test_manifest_and_current_mapping_declare_non_fk_raw_and_member_closure():
    from backtest_data.remote.registry import logical_parents
    rows=logical_parents({'table':'target_universe_manifest','values':{'members':[{'security_id':'security','issuer_id':'issuer'}],
        'scope':{'directory_evidence':[{'raw_id':'original-directory'}]}}})
    assert set(rows)=={('security','security'),('issuer','issuer'),('raw_object','original-directory')}
    assert logical_parents({'table':'catalog_observation','values':{'source':'kis_overseas_master',
        'fields':{'master_raw_id':'master-capture'}}})==[('raw_object','master-capture')]


def test_catalog_only_handoff_cannot_enable_remote_acquisition():
    from backtest_data.remote.planner import validate_ownership
    configuration={'enabled':True,'owner_id':'private-sec','provider_slots':[{'source':'sec','account_id':'account-label',
        'owner':'remote','local_enabled':False,'market':'us','domain':'fundamentals'}]}
    with pytest.raises(ValueError,match='identity'):validate_ownership(configuration)
    configuration['identity_handoff_ready']=True
    assert validate_ownership(configuration)==configuration['provider_slots']


def test_supplier_unavailable_gap_preserves_bound_security_and_optional_raw_lineage():
    from backtest_data.remote.registry import logical_parents
    issue={'table':'validation_issue','values':{'record_domain':'price','record_id':None,
            'evidence':{'security_id':'original-security','provider_code':'404'}}}
    assert logical_parents(issue)==[('security','original-security')]
    issue['values']['record_id']='observation';issue['values']['evidence']['raw_id']='raw-response'
    assert set(logical_parents(issue))=={('price_observation','observation'),('security','original-security'),('raw_object','raw-response')}
