"""Queries resolve only sealed SQL membership and frozen catalog snapshots."""

from datetime import date,datetime,timezone
from collections import defaultdict

from ..fundamentals.models import Filing
from .build import database_session,digest,json_value,row_payload
from .models import DatasetRelease
from .query_context import QueryContext,bind_context,definition_registry,registered_definition,valid_metric_state
from .validation import frozen_rows
from .public_clock import availability_payload,daily_clock_enabled,public_clock_enabled


class QueryRows(dict):
    """One ephemeral index over frozen membership, reused by every row lookup."""
    def __init__(self,rows,manifest=None):
        super().__init__(rows)
        self.manifest=manifest or {}
        self.decisions=defaultdict(list)
        self.mappings=defaultdict(list)
        self.aliases=defaultdict(list)
        self.metrics=defaultdict(list)
        self.sources_by_issuer=defaultdict(list)
        self.canonicals_by_issuer=defaultdict(list)
        self.current_public_inputs={}
        for decision in rows.get('selection_decision',{}).values():
            self.decisions[(decision.record_domain,decision.record_id)].append(decision)
        for values in self.decisions.values():values.sort(key=lambda item:(item.recorded_at,item.id))
        for source in rows.get('source_financial_fact',{}).values():self.sources_by_issuer[source.issuer_id].append(source)
        for mapping in rows.get('fundamental_fact',{}).values():
            self.mappings[mapping.source_fact_id].append(mapping)
            self.canonicals_by_issuer[mapping.issuer_id].append(mapping)
        for metric in rows.get('derived_fact',{}).values():self.metrics[(metric.entity_id,metric.metric_id)].append(metric)
        for value in (manifest or {}).get('catalog',{}).get('observations',[]):
            if value['source']=='financial_provenance':self.aliases[value.get('fields',{}).get('document_id')].append(value)


def visible(row,context,rows=None):
    available=getattr(row,'available_at',None)
    if rows is not None:
        from .market_clock import enabled,availability
        if enabled(rows):
            market=availability(row,rows)
            if market is not None:
                if market['status']=='unavailable':return False
                available=market['effective_available_at']
        from .public_clock import public_clock_enabled,row_availability
        if public_clock_enabled(rows):
            resolved=row_availability(row,rows,context)
            policies=rows.manifest.get('policies',{})
            financial_proof_required=policies.get('financial_certificate_required') or policies.get('financial_daily_metadata_required')
            if financial_proof_required and resolved is None:
                from ..fundamentals.models import FinancialSourceDocument,SourceFinancialFact,FundamentalFact,DerivedFact
                if isinstance(row,(FinancialSourceDocument,SourceFinancialFact,FundamentalFact,DerivedFact)):return False
            if resolved is not None:
                if resolved['status']=='unavailable':return False
                if financial_proof_required and resolved['status']!='qualified':return False
                if rows.manifest.get('policies',{}).get('financial_certificate_required'):
                    from ..fundamentals.models import FundamentalFact,SourceFinancialFact
                    from ..fundamentals.completion import original_metadata_qualified
                    basis=getattr(row,'basis',{})
                    if isinstance(row,SourceFinancialFact) and (row.evidence or {}).get('sec_fiscal_metadata'):
                        document=rows.get('financial_source_document',{}).get(row.document_id)
                        if document is None:return False
                        basis={'sec_fiscal_metadata':row.evidence['sec_fiscal_metadata'],'source_document_hash':document.content_hash}
                    if isinstance(row,(FundamentalFact,SourceFinancialFact)):
                        proofs=rows.get('financial_publication_evidence',{})
                        selected_proofs=[proofs[identity] for identity in resolved.get('evidence_ids',()) if identity in proofs]
                        if not original_metadata_qualified(basis,selected_proofs,resolved):return False
                available=resolved['effective_available_at']
    if isinstance(row,Filing) and not row.evidence.get('original_publication_verified'):
        available=max(available,row.captured_at) if available else None
    if available is None or available>context.as_of:return False
    if context.history_mode=='observed_system':
        for name in ('captured_at','first_seen_at','extracted_at','materialized_at'):
            if hasattr(row,name) and (getattr(row,name) is None or getattr(row,name)>context.as_of):return False
    return True


def lineage_visible(row,domain,rows,context):
    from .build import dependencies
    pending=[(domain,row.id)]
    seen=set()
    while pending:
        current_domain,identity=pending.pop()
        if (current_domain,identity) in seen:continue
        seen.add((current_domain,identity))
        current=rows[current_domain][identity]
        if current_domain=='selection_decision':
            if (context.history_mode=='observed_system' and current.recorded_at>context.as_of) or current.status!='accepted':return False
        elif hasattr(current,'available_at'):
            # Filing clocks are resolved through the exact document certificate,
            # rather than through another instance from the same filing.
            from .public_clock import public_clock_enabled
            covered_by_source=(daily_clock_enabled(rows) and current_domain=='financial_source_document'
                and domain in {'source_financial_fact','fundamental_fact','derived_fact'})
            if not (public_clock_enabled(rows) and current_domain=='filing') and not covered_by_source and not visible(current,context,rows):return False
        if daily_clock_enabled(rows) and current_domain=='financial_daily_publication_evidence' and context.history_mode=='observed_system':
            if current.captured_at>context.as_of or current.recorded_at>context.as_of:return False
        if current_domain=='financial_source_document' and not current.extraction_complete:return False
        if current_domain=='derived_fact' and (not registered_definition(current) or not valid_metric_state(current)
                or current.value is not None and not current.input_refs):return False
        if not economic_dates_visible(current,context):return False
        if current_domain in {'source_financial_fact','fundamental_fact','price_observation','derived_fact'}:
            ok,_=adopted(current,current_domain,rows,context)
            if not ok:return False
        pending.extend(dependencies(current))
    return True


def economic_dates_visible(row,context):
    dates=[]
    for name in ('session','effective_session','period_end','instant'):
        value=getattr(row,name,None)
        if value is not None:dates.append(value)
    for metadata in (getattr(row,'period',{}),getattr(row,'basis',{})):
        if not isinstance(metadata,dict):continue
        for key in ('period_end','end','fiscal_end','instant','share_basis_as_of','price_session','share_snapshot_date','fx_fixing_date'):
            value=metadata.get(key)
            if value:
                try:dates.append(datetime.fromisoformat(str(value).replace('Z','+00:00')).date())
                except ValueError:return False
    cutoff_date=context.as_of.astimezone(timezone.utc).date()
    return all((value.date() if isinstance(value,datetime) else value)<=cutoff_date for value in dates)


def adopted(row,domain,rows,context,*,allow_source_only=False):
    if not isinstance(rows,QueryRows):rows=QueryRows(rows)
    all_decisions=rows.decisions.get((domain,row.id),())
    eligible=(all_decisions if context.history_mode=='reconstructed_public' else
              [item for item in all_decisions if item.recorded_at<=context.as_of])
    if not eligible:
        return (allow_source_only and not all_decisions and context.history_mode=='reconstructed_public'),None
    latest=eligible[-1]
    return latest.status=='accepted',latest


def query_state(session,context):
    release=session.get(DatasetRelease,context.release_id)
    if release is None:raise ValueError('release_not_found')
    bind_context(context,release.manifest)
    scope=release.manifest.get('research_scope')
    if scope and scope.get('kind') in {'financial_documents_v1','price_sessions_v1'} and context.required_profile not in scope.get('allowed_profiles',[]):
        raise ValueError('query_domain_outside_release_scope')
    return release,QueryRows(frozen_rows(session,release),release.manifest)


def envelope(release,context,rows,*,extra=None):
    payload={'release_id':release.id,'release_hash':release.content_hash,'as_of':context.as_of.astimezone(timezone.utc).isoformat(),
        'history_mode':context.history_mode,'quality_profile':context.required_profile,'disclosure_profile':context.disclosure_profile,
        'research_partial':context.research_partial,'tier':release.manifest['tier'],'policies':release.manifest['policies'],
        'target_count':release.manifest['target_count'],'coverage':release.manifest['coverage'],'rows':rows}
    if extra:payload.update(extra)
    # Detach the whole response, including manifest metadata and extra fields,
    # before exposing it to callers of a reusable ORM Session.
    payload=json_value(payload)
    payload['payload_hash']=digest(payload)
    return payload


def target_ids(release,ids,key):
    if not ids:return set(release.manifest[key])
    result=set(ids)
    if not result.issubset(set(release.manifest[key])):raise ValueError('entity_outside_frozen_target')
    return result


def source_revision_identity(item):
    """Expanded source identities allow equivalent namespace prefix aliases."""
    evidence=item.get('evidence') or {}
    return digest({**{field:item.get(field) for field in ('issuer_id','period_kind','period_start','period_end','instant','scope','dimensions')},
        'concept':evidence.get('concept_qname',item.get('concept')),
        'unit':sorted((unit['role'],unit['measure_qname']) for unit in evidence['unit_measure_bindings'])
            if evidence.get('unit_measure_bindings') else item.get('unit')})


def _standard_source_revision_family(source,document):
    """v7 aliases only approved versions of the exact same standard account."""
    from ..fundamentals.mapping_catalog import default_catalog,local_concept,taxonomy_group
    from ..fundamentals.materialize import _source_adapter
    item={**{field:getattr(source,field) for field in
        ('issuer_id','concept','period_kind','period_start','period_end','instant','scope','unit','dimensions')},
        'evidence':source.evidence}
    catalog=default_catalog()
    try:
        adapted=_source_adapter(source)
        local=local_concept(adapted)
        group=taxonomy_group(adapted.taxonomy,catalog)
    except ValueError:
        local=group=None
    accounting=document.accounting_basis
    if group and accounting in {None,'','unknown'}:
        accounting={'us-gaap':'US_GAAP','ifrs-full':'IFRS'}[group]
    qname=(source.evidence or {}).get('concept_qname')
    approved=group and local and any(entry.taxonomy_group==group and entry.concept==local
        for entry in catalog.entries)
    if approved and (qname is None or qname==f'{{{adapted.taxonomy}}}{local}'):
        item['evidence']={**(source.evidence or {}),'concept_qname':('standard_account',group,local)}
    return digest({'source_identity':source_revision_identity(item),'accounting_basis':accounting})


def _daily_revision_index(candidates):
    """Daily metadata order uses source publication day before session bound."""
    if not candidates or any(type(day) is not date for day,_,_,_ in candidates):return None
    latest=max(day for day,_,_,_ in candidates)
    winning=[index for index,(day,_,_,_) in enumerate(candidates) if day==latest]
    if any(candidates[index][2]!=candidates[winning[0]][2] for index in winning):return None
    bound=max(candidates[index][1] for index in winning)
    return min((index for index in winning if candidates[index][1]==bound),key=lambda index:candidates[index][3])


def _revision_public_order(availability,rows):
    value=availability.get('public_day' if daily_clock_enabled(rows) else 'verified_public_bound')
    if isinstance(value,str):return date.fromisoformat(value) if daily_clock_enabled(rows) else datetime.fromisoformat(value)
    return value


def _winning_public_revision_ids(groups,*,daily=False):
    from ..fundamentals.selection import public_revision_index
    winners=set()
    for candidates in groups.values():
        index=_daily_revision_index(candidates) if daily else public_revision_index(candidates)
        if index is None:continue
        bound,_,value,_=candidates[index]
        # Equal arithmetic semantics at the same bound are equivalent winners.
        # Independent canonical/source UUID tie-breaks must not contradict.
        winners.update(identity for public,_,candidate_value,identity in candidates
                       if public==bound and candidate_value==value)
    return winners


def _canonical_arithmetic_semantics(fact):
    """Fiscal labels and calculation flags can change arithmetic without value."""
    return (fact.value,fact.fiscal_year,fact.fiscal_quarter,fact.quarter_index,fact.fiscal_cycle,
        fact.directly_reported,fact.period_compatible,fact.sign_policy,tuple(sorted(set(fact.component_ids))))


def _current_public_financial_inputs(rows,context,issuer_id):
    """Index latest eligible source and canonical revisions once per issuer."""
    from ..fundamentals.materialize import canonical_to_financial_fact
    from ..fundamentals.selection import fact_identity
    from .public_clock import row_availability
    cache_key=(issuer_id,context.as_of,context.history_mode,context.disclosure_profile)
    if cache_key in rows.current_public_inputs:return rows.current_public_inputs[cache_key]
    source_groups=defaultdict(list)
    canonical_groups=defaultdict(list)
    for source in rows.sources_by_issuer.get(issuer_id,()):
        document=rows['financial_source_document'].get(source.document_id)
        if document is None or document.disclosure_profile!=context.disclosure_profile:continue
        if not lineage_visible(source,'source_financial_fact',rows,context):continue
        availability=row_availability(source,rows,context)
        if availability is None or availability['status']!='qualified':continue
        key=_standard_source_revision_family(source,document)
        source_groups[key].append((_revision_public_order(availability,rows),availability['effective_available_at'],
            (source.value,source.is_nil),source.id))
    for canonical in rows.canonicals_by_issuer.get(issuer_id,()):
        source=rows['source_financial_fact'].get(canonical.source_fact_id)
        document=rows['financial_source_document'].get(source.document_id) if source else None
        if document is None or document.disclosure_profile!=context.disclosure_profile:continue
        if not lineage_visible(canonical,'fundamental_fact',rows,context):continue
        availability=row_availability(canonical,rows,context)
        if availability is None or availability['status']!='qualified':continue
        _,decision=adopted(canonical,'fundamental_fact',rows,context)
        try:
            fact=canonical_to_financial_fact(canonical,source=source,decision=decision,availability=availability)
        except ValueError:continue
        canonical_groups[fact_identity(fact)].append((_revision_public_order(availability,rows),fact.available_at,
            _canonical_arithmetic_semantics(fact),fact.id))
    result={'source_financial_fact':_winning_public_revision_ids(source_groups,daily=daily_clock_enabled(rows)),
            'fundamental_fact':_winning_public_revision_ids(canonical_groups,daily=daily_clock_enabled(rows))}
    rows.current_public_inputs[cache_key]=result
    return result


def metric_public_revisions_current(row,rows,context):
    """v7/v8 reject metrics with superseded or ambiguous financial inputs."""
    if rows.manifest.get('policies',{}).get('query_version') not in {'release_query_v7','release_query_v8_daily'}:return True
    from .build import dependencies
    pending=[('derived_fact',row.id)]
    seen=set()
    while pending:
        domain,identity=pending.pop()
        if (domain,identity) in seen:continue
        seen.add((domain,identity))
        current=rows.get(domain,{}).get(identity)
        if current is None:return False
        if domain in {'fundamental_fact','source_financial_fact'}:
            winners=_current_public_financial_inputs(rows,context,current.issuer_id)
            if identity not in winners[domain]:return False
        pending.extend(dependencies(current))
    return True


def get_security_aliases(database,values,context,*,market=None,venue=None,namespace=None,session_date=None):
    """Resolve names and codes exclusively through the release's frozen assertions."""
    from ..catalog.aliases import resolve_records
    if context.required_profile!='security_identity':raise ValueError('identity_quality_profile_required')
    if not values:raise ValueError('identifier_required')
    with database_session(database) as session:
        release,rows=query_state(session,context)
        allowed=set(release.manifest['target_security_ids'])
        securities={key:value for key,value in release.manifest['catalog']['securities'].items() if key in allowed}
        # Outside-target corrections suppress old aliases before target filtering.
        records=list(rows['identifier_history'].values())
        output=[resolve_records(value,records,securities,as_of=context.as_of,history_mode=context.history_mode,
                    session_date=session_date,market=market,venue=venue,namespace=namespace) for value in values]
        return envelope(release,context,output,extra={'identity_scope':'frozen_target',
            'historical_identity_qualified':False,'unresolved_or_ambiguous_is_not_all_target':True})


def get_source_accounts(database,issuer_ids,context,*,latest_visible=False):
    if context.required_profile!='fundamentals_sql':raise ValueError('financial_quality_profile_required')
    with database_session(database) as session:
        release,rows=query_state(session,context)
        ids=target_ids(release,issuer_ids,'target_issuer_ids')
        output=[]
        for fact in rows['source_financial_fact'].values():
            if fact.issuer_id not in ids or not visible(fact,context,rows):continue
            doc=rows['financial_source_document'][fact.document_id]
            filing=rows['filing'][doc.filing_id]
            if doc.disclosure_profile!=context.disclosure_profile or not daily_clock_enabled(rows) and not visible(doc,context,rows):continue
            if not public_clock_enabled(rows) and not visible(filing,context):continue
            if public_clock_enabled(rows) and (not doc.extraction_complete or not economic_dates_visible(fact,context)):continue
            accepted,decision=adopted(fact,'source_financial_fact',rows,context,allow_source_only=True)
            if not accepted:continue
            mappings=[value for value in rows.mappings.get(fact.id,())
                      if visible(value,context,rows) and lineage_visible(value,'fundamental_fact',rows,context)]
            document_metadata={**row_payload(doc,exclude={'inventory'}),'inventory':{
                'counts':doc.inventory.get('counts',{}),'context':doc.inventory.get('contexts',{}).get(fact.context_id),
                'issues':doc.inventory.get('issues',[]),'issuer_identity':doc.inventory.get('issuer_identity'),
                'source_request_metadata':doc.inventory.get('source_request_metadata')}}
            output.append({**row_payload(fact),**availability_payload(fact,rows,context),'issuer_name':release.manifest['catalog']['issuers'][fact.issuer_id]['name'],
                'document':document_metadata,'filing':row_payload(filing),'mapping_status':'mapped' if mappings else 'unmapped',
                'mapping_record_ids':sorted(value.id for value in mappings),'selection_status':'accepted' if decision else 'source_extraction_only',
                'decision':row_payload(decision) if decision else None,'extraction_complete':doc.extraction_complete,
                'provenance_aliases':[value for value in rows.aliases.get(doc.id,())
                    if context.history_mode=='reconstructed_public' or datetime.fromisoformat(value['captured_at'])<=context.as_of]})
        if latest_visible:
            chosen={}
            ambiguous=set()
            if public_clock_enabled(rows):
                from decimal import Decimal
                from ..fundamentals.selection import public_revision_index
                groups=defaultdict(list)
                for item in output:groups[source_revision_identity(item)].append(item)
                for key,versions in groups.items():
                    candidates=[]
                    for item in versions:
                        public=_revision_public_order(item['public_availability'],rows)
                        available=item['public_availability']['effective_available_at']
                        candidates.append((public,
                            datetime.fromisoformat(available) if isinstance(available,str) else available,
                            (Decimal(item['value']) if item['value'] is not None else None,item['is_nil']),item['id']))
                    index=_daily_revision_index(candidates) if daily_clock_enabled(rows) else public_revision_index(candidates)
                    if index is None:ambiguous.add(key)
                    else:chosen[key]=versions[index]
            else:
                for item in output:
                    key=digest({field:item.get(field) for field in ('issuer_id','concept','period_kind','period_start','period_end','instant','scope','unit','dimensions')})
                    if key not in chosen or (item['available_at'],item['captured_at'],item['id'])>(chosen[key]['available_at'],chosen[key]['captured_at'],chosen[key]['id']):chosen[key]=item
            output=[item for key,item in chosen.items() if key not in ambiguous]
        output.sort(key=lambda item:(item['issuer_id'],item['concept'],item['available_at'],item['id']))
        extra={'revision_selection':'latest_visible' if latest_visible else 'all_visible'}
        if public_clock_enabled(rows):extra.update(public_version_order='metadata_public_day' if daily_clock_enabled(rows) else 'verified_public_bound',ambiguous_revision_groups=len(ambiguous) if latest_visible else 0,
            research_scope=release.manifest.get('research_scope'),missing_value_is_not_zero=True)
        return envelope(release,context,output,extra=extra)


def get_fundamentals(database,issuer_ids,context,*,metric_ids=None):
    """Return frozen canonical names and qualifications, not implied clean ratios."""
    if context.required_profile!='fundamentals_sql':raise ValueError('financial_quality_profile_required')
    with database_session(database) as session:
        release,rows=query_state(session,context)
        ids=target_ids(release,issuer_ids,'target_issuer_ids')
        output=[]
        for fact in rows['fundamental_fact'].values():
            if fact.issuer_id not in ids or metric_ids and fact.metric_id not in metric_ids:continue
            if not visible(fact,context,rows) or not lineage_visible(fact,'fundamental_fact',rows,context):continue
            source=rows['source_financial_fact'][fact.source_fact_id]
            doc=rows['financial_source_document'][source.document_id]
            if doc.disclosure_profile!=context.disclosure_profile:continue
            filing=rows['filing'][doc.filing_id]
            basis=fact.basis or {}
            gaps=[]
            if basis.get('scope') in {None,'unknown'}:gaps.append('scope_unqualified')
            if basis.get('accounting_basis') in {None,'unknown'}:gaps.append('accounting_basis_unqualified')
            if not basis.get('fiscal_lineage') or not basis.get('qualification_evidence'):gaps.append('fiscal_lineage_unqualified')
            if fact.metric_id in {'eps_basic','eps_diluted'} and not basis.get('share_basis'):gaps.append('share_basis_unqualified')
            accepted,decision=adopted(fact,'fundamental_fact',rows,context)
            output.append({**row_payload(fact),**availability_payload(fact,rows,context),'mapping_record_id':fact.id,'scope':basis.get('scope','unknown'),
                'qualification_gaps':gaps,'issuer_name':release.manifest['catalog']['issuers'][fact.issuer_id]['name'],
                'source_fact':row_payload(source),'filing':row_payload(filing),
                'document':row_payload(doc,exclude={'inventory'}),'decision':row_payload(decision)})
        output.sort(key=lambda item:(item['issuer_id'],item['metric_id'],item['available_at'],item['id']))
        return envelope(release,context,output,extra={'revision_selection':'all_visible',
                         'canonical_mapping_is_calculation_qualification':False})


def get_prices(database,security_ids,context,*,start=None,end=None,basis='as_traded'):
    if context.required_profile!='core_price_volume':raise ValueError('price_quality_profile_required')
    if basis not in {'as_traded','provider_adjusted'}:raise ValueError('unsupported_adjusted_price_policy')
    with database_session(database) as session:
        release,rows=query_state(session,context)
        allowed_bases={'as_traded','provider_adjusted'} if daily_clock_enabled(rows) else {'as_traded'}
        if basis not in allowed_bases:raise ValueError('unsupported_adjusted_price_policy')
        ids=target_ids(release,security_ids,'target_security_ids')
        selected={}
        for price in rows['price_observation'].values():
            if (price.security_id not in ids or price.basis!=basis or not visible(price,context,rows)
                    or not economic_dates_visible(price,context)):continue
            if start and price.session<start or end and price.session>end:continue
            accepted,decision=adopted(price,'price_observation',rows,context)
            if not accepted:continue
            # Distinct venue/feed/trade scopes remain distinct source series.
            key=(price.security_id,price.session,price.source_contract_id,price.feed,price.venue,price.trade_scope,price.basis,price.currency,price.quantity_unit)
            if key not in selected or (price.available_at,price.captured_at,price.id)>(selected[key][0].available_at,selected[key][0].captured_at,selected[key][0].id):selected[key]=(price,decision)
        output=[]
        for price,decision in selected.values():
            from .market_clock import enabled,availability
            clock={'market_availability':availability(price,rows)} if enabled(rows) else {}
            output.append({**row_payload(price),**clock,'security_name':release.manifest['catalog']['securities'][price.security_id]['name'],
                           'decision':row_payload(decision),'raw':row_payload(rows['raw_object'][price.raw_id]),
                           'source_contract':row_payload(rows['source_contract'][price.source_contract_id])})
        output.sort(key=lambda item:(item['security_id'],item['session'],item['feed'],item['id']))
        return envelope(release,context,output)


def get_financial_metrics(database,entity_ids,context,metric_ids):
    if context.required_profile not in {'fundamentals_sql','valuation'}:raise ValueError('financial_quality_profile_required')
    if any(metric not in definition_registry() for metric in metric_ids):raise ValueError('unsupported_metric_id')
    with database_session(database) as session:
        release,rows=query_state(session,context)
        allowed=set(release.manifest['target_issuer_ids'])|set(release.manifest['target_security_ids'])
        ids=set(entity_ids) if entity_ids else set(release.manifest['target_issuer_ids'])
        if not ids.issubset(allowed):raise ValueError('entity_outside_frozen_target')
        output=[]
        for identity in sorted(ids):
            for metric in metric_ids:
                candidates=[]
                for row in rows.metrics.get((identity,metric),()):
                    if not visible(row,context,rows):continue
                    if row.basis.get('disclosure_profile')!=context.disclosure_profile:continue
                    accepted,decision=adopted(row,'derived_fact',rows,context)
                    if not accepted:continue
                    if (lineage_visible(row,'derived_fact',rows,context)
                            and metric_public_revisions_current(row,rows,context)):candidates.append(row)
                if candidates:
                    chosen=max(candidates,key=lambda item:(item.evaluation_as_of,item.available_at,item.materialized_at,item.id))
                    output.append({**row_payload(chosen),**availability_payload(chosen,rows,context)})
                else:
                    output.append({'entity_id':identity,'metric_id':metric,'value':None,'status':'unavailable',
                                   'reason':'canonical_or_derived_data_unavailable','input_refs':[]})
        return envelope(release,context,output)


def cli_query(database,args):
    try:cutoff=datetime.fromisoformat(args.as_of.replace('Z','+00:00'))
    except (ValueError,AttributeError):raise ValueError('aware_as_of_required') from None
    financial=args.action in {'source-accounts','fundamentals','metrics','smoke'}
    profile='security_identity' if args.action=='resolve' else 'fundamentals_sql' if financial else 'core_price_volume'
    context=QueryContext(args.release_id,cutoff,args.history_mode,profile,'periodic_report' if financial else 'not_applicable',args.research_partial)
    identifiers=getattr(args,'identifier',None) or []
    ids=args.entity_id
    resolutions=None
    if identifiers or args.action=='resolve':
        if ids:raise ValueError('mixed_entity_ids_and_aliases_not_supported')
        if args.action=='metrics':raise ValueError('metrics_require_explicit_issuer_or_security_ids')
        alias_context=QueryContext(args.release_id,cutoff,args.history_mode,'security_identity','not_applicable',args.research_partial)
        market=getattr(args,'market',None)
        resolved=get_security_aliases(database,identifiers,alias_context,
            market=None if not market or market.lower()=='both' else market.upper(),
            venue=getattr(args,'venue',None),namespace=getattr(args,'namespace',None),
            session_date=getattr(args,'session_date',None))
        if args.action=='resolve':return {'status':'research_partial','data':resolved}
        resolutions=resolved['rows']
        for resolution in resolutions:
            if resolution['status']!='resolved':
                return {'status':'blocked','reason':'identifier_'+resolution['status'],'data':resolved}
        if financial:
            ids=[resolution['candidates'][0]['issuer_id'] for resolution in resolutions]
            if any(identity is None for identity in ids):
                return {'status':'blocked','reason':'issuer_link_unavailable','data':resolved}
        else:ids=[resolution['security_id'] for resolution in resolutions]
        ids=sorted(set(ids))

    def result(data):
        if resolutions is not None:
            data.pop('payload_hash',None)
            data['identifier_resolutions']=resolutions
            data['payload_hash']=digest(data)
        return {'status':'research_partial','data':data}

    if args.action=='source-accounts':return result(get_source_accounts(database,ids,context))
    if args.action=='fundamentals':return result(get_fundamentals(database,ids,context,
        metric_ids=getattr(args,'metric_id',None) or None))
    if args.action=='prices':return result(get_prices(database,ids,context,basis=getattr(args,'basis','as_traded')))
    if args.action=='metrics':return result(get_financial_metrics(database,ids,context,
        getattr(args,'metric_id',None) or list(definition_registry())))
    if args.action=='smoke':
        with database_session(database) as session:
            release,rows=query_state(session,context)
            ids=target_ids(release,ids,'target_issuer_ids')
            count=0
            for fact in rows['source_financial_fact'].values():
                doc=rows['financial_source_document'][fact.document_id]
                filing=rows['filing'][doc.filing_id]
                ok,_=adopted(fact,'source_financial_fact',rows,context,allow_source_only=True)
                if daily_clock_enabled(rows):
                    count+=bool(fact.issuer_id in ids and visible(fact,context,rows) and doc.extraction_complete
                        and economic_dates_visible(fact,context) and doc.disclosure_profile==context.disclosure_profile and ok)
                else:
                    count+=bool(fact.issuer_id in ids and visible(fact,context) and visible(doc,context) and visible(filing,context)
                                and doc.disclosure_profile==context.disclosure_profile and ok)
            data=envelope(release,context,[],extra={'visible_source_accounts':count})
            if resolutions is not None:
                data.pop('payload_hash',None)
                data['identifier_resolutions']=resolutions
                data['payload_hash']=digest(data)
            return {'status':'research_partial','release_id':args.release_id,'payload_hash':data['payload_hash'],
                    'target_count':data['target_count'],'visible_source_accounts':count,'coverage':data['coverage'],
                    **({'identifier_resolutions':resolutions} if resolutions is not None else {})}
    raise ValueError('unsupported_query_action')
