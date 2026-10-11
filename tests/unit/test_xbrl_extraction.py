"""Extraction must retain custom numeric evidence independently of mappings."""

from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pytest


INSTANCE = b'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:custom="urn:custom"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <xbrli:context id="C"><xbrli:entity><xbrli:identifier scheme="urn:issuer">1</xbrli:identifier>
 <xbrli:segment><xbrldi:explicitMember dimension="custom:Region">custom:North</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
 <xbrli:period><xbrli:startDate>2020-01-01</xbrli:startDate><xbrli:endDate>2020-03-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
 <custom:UsefulNote contextRef="C" unitRef="USD" decimals="-3" precision="6">123456789.123456789</custom:UsefulNote>
 <custom:Missing contextRef="C" unitRef="USD" xsi:nil="true" decimals="INF" />
</xbrli:xbrl>'''


def test_native_custom_context_numeric_precision_and_nil_are_preserved():
    from backtest_data.fundamentals.xbrl import parse_xbrl

    extracted = parse_xbrl(INSTANCE)
    assert extracted.complete
    assert extracted.inventory.numeric_nodes == 2
    assert len(extracted.facts) == 2
    fact = extracted.facts[0]
    assert fact.concept == "custom:UsefulNote"
    assert fact.taxonomy == "urn:custom"
    assert fact.value == Decimal("123456789.123456789")
    assert fact.raw_value == "123456789.123456789"
    assert fact.decimals == "-3" and fact.precision == "6"
    assert fact.dimensions == {"{urn:custom}Region": "{urn:custom}North"}
    assert fact.period_start.isoformat() == "2020-01-01"
    assert fact.period_end.isoformat() == "2020-03-31"
    assert fact.unit == "USD"
    assert extracted.facts[1].is_nil and extracted.facts[1].value is None


def test_same_context_exact_duplicates_dedupe_but_conflicts_remain_flagged():
    from backtest_data.fundamentals.xbrl import parse_xbrl

    marker = b'</xbrli:xbrl>'
    repeated = b'<custom:UsefulNote contextRef="C" unitRef="USD" decimals="-3" precision="6">123456789.123456789</custom:UsefulNote>'
    extracted = parse_xbrl(INSTANCE.replace(marker, repeated + marker))
    assert len(extracted.facts) == 2 and extracted.inventory.deduplicated_nodes == 1
    conflicted = parse_xbrl(INSTANCE.replace(marker, repeated.replace(b'123456789.123456789', b'9') + marker))
    assert len(conflicted.facts) == 3
    assert any(issue.code == "conflicting_duplicate" for issue in conflicted.issues)
    assert not conflicted.complete


def test_inline_scale_sign_continuation_fraction_and_unsupported_transform():
    from backtest_data.fundamentals.xbrl import parse_xbrl

    inline = INSTANCE.replace(b'</xbrli:xbrl>', b'''<ix:nonFraction xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"
      name="custom:Inline" contextRef="C" unitRef="USD" format="ixt:num-dot-decimal" scale="3" sign="-" continuedAt="cont">1,<ix:exclude>ignore</ix:exclude>2</ix:nonFraction>
      <ix:continuation xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" id="cont">34.50</ix:continuation>
      <ix:fraction xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" name="custom:Fraction" contextRef="C" unitRef="USD"><ix:numerator>1</ix:numerator><ix:denominator>4</ix:denominator></ix:fraction>
      <ix:nonFraction xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:bad="urn:bad" name="custom:Unsupported" contextRef="C" unitRef="USD" format="bad:magic">100</ix:nonFraction>
      </xbrli:xbrl>''')
    extracted = parse_xbrl(inline)
    facts = {fact.concept: fact for fact in extracted.facts}
    assert facts['custom:Inline'].value == Decimal('-1234500')
    assert facts['custom:Inline'].raw_value == '1,234.50'
    assert facts['custom:Fraction'].value == Decimal('0.25')
    assert facts['custom:Unsupported'].value is None
    assert facts['custom:Unsupported'].parse_status == 'unsupported_transform'
    assert not extracted.complete


@pytest.mark.parametrize('data', [b'<!DOCTYPE x [<!ENTITY a "bad">]><x>&a;</x>', b'<!DOCTYPE x SYSTEM "file:///secret"><x/>'])
def test_rejects_xml_declarations_with_dtd_or_entities(data):
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl
    with pytest.raises(UnsafeFinancialDocument):
        parse_xbrl(data)


def test_bounded_document_and_unsafe_zip_paths_are_rejected():
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl, parse_xbrl_zip
    with pytest.raises(UnsafeFinancialDocument):
        parse_xbrl(INSTANCE, max_bytes=10)
    payload = BytesIO()
    with ZipFile(payload, 'w') as archive:
        archive.writestr('../secret.xml', INSTANCE)
    with pytest.raises(UnsafeFinancialDocument):
        parse_xbrl_zip(payload.getvalue())


def test_dart_receipt_identity_and_unmapped_account_inventory():
    from backtest_data.fundamentals.adapters.opendart import ReceiptMismatch, parse_financial_json
    row = {'rcept_no': '202001010001', 'account_id': 'custom_A', 'account_nm': 'Useful account', 'sj_div': 'CF', 'fs_div': 'CFS', 'currency': 'KRW', 'thstrm_amount': '1,234', 'frmtrm_amount': '-5'}
    result = parse_financial_json({'status': '000', 'list': [row]}, requested_receipt='202001010001')
    assert len(result.facts) == 2
    assert [fact.value for fact in result.facts] == [Decimal('1234'), Decimal('-5')]
    assert result.facts[0].account_name == 'Useful account'
    assert result.facts[0].scope == 'consolidated'
    assert result.inventory.numeric_nodes == 2
    with pytest.raises(ReceiptMismatch, match='latest_only'):
        parse_financial_json({'status': '000', 'list': [row]}, requested_receipt='199901010001')


def test_native_fraction_and_currency_namespace_alias_are_resolved():
    from backtest_data.fundamentals.xbrl import parse_xbrl
    document=INSTANCE.replace(b'xmlns:iso4217=',b'xmlns:money=').replace(b'iso4217:USD',b'money:USD')
    document=document.replace(b'</xbrli:xbrl>', b'<custom:Fraction contextRef="C" unitRef="USD"><xbrli:numerator>1</xbrli:numerator><xbrli:denominator>4</xbrli:denominator></custom:Fraction></xbrli:xbrl>')
    extracted=parse_xbrl(document)
    assert extracted.facts[0].unit=='USD'
    assert extracted.facts[0].original_unit=='money:USD'
    assert extracted.facts[-1].value==Decimal('.25')


def test_dart_malformed_thousands_group_is_preserved_as_explicit_failure():
    from backtest_data.fundamentals.adapters.opendart import parse_financial_json
    result=parse_financial_json({'status':'000','list':[{'rcept_no':'R','account_id':'A','thstrm_amount':'12,34'}]},requested_receipt='R')
    assert result.facts[0].raw_value=='12,34'
    assert result.facts[0].value is None and not result.complete


def test_unit_tag_identifies_numeric_node_with_missing_context_and_retains_failure():
    from backtest_data.fundamentals.xbrl import parse_xbrl
    document=INSTANCE.replace(b'</xbrli:xbrl>',b'<custom:Malformed unitRef="USD">123</custom:Malformed></xbrli:xbrl>')
    result=parse_xbrl(document)
    assert result.inventory.numeric_nodes==3
    assert result.facts[-1].parse_status=='missing_context' and not result.complete


def test_dart_json_bytes_are_size_bounded_before_loading():
    from backtest_data.fundamentals.adapters.opendart import parse_financial_json
    with pytest.raises(ValueError,match='document_size_limit'):
        parse_financial_json(b'{"status":"000","list":[]}',requested_receipt='R',max_bytes=1)


def test_locally_rebound_dimension_qnames_preserve_distinct_meaning_and_bindings():
    import xml.etree.ElementTree as ET
    from backtest_data.fundamentals.xbrl import parse_xbrl
    document=INSTANCE.replace(b'<xbrldi:explicitMember dimension="custom:Region">custom:North</xbrldi:explicitMember>',
        b'<xbrldi:explicitMember xmlns:d="urn:region" dimension="d:Axis">d:Member</xbrldi:explicitMember>'
        b'<xbrldi:explicitMember xmlns:d="urn:product" dimension="d:Axis">d:Member</xbrldi:explicitMember>')
    result=parse_xbrl(document)
    expected={'{urn:region}Axis':'{urn:region}Member','{urn:product}Axis':'{urn:product}Member'}
    assert result.complete and result.contexts['C'].dimensions==expected
    assert result.facts[0].dimensions==expected
    evidence=result.facts[0].evidence['context_dimension_bindings']
    assert [item['dimension_lexical'] for item in evidence]==['d:Axis','d:Axis']
    assert [item['member_lexical'] for item in evidence]==['d:Member','d:Member']
    assert [item['namespace_bindings']['d'] for item in evidence]==['urn:region','urn:product']
    assert 'xmlns:d="urn:region"' in result.contexts['C'].raw_xml
    assert 'xmlns:d="urn:product"' in result.contexts['C'].raw_xml
    ET.fromstring(result.contexts['C'].raw_xml)
    assert result.facts[0].evidence['concept_qname']=='{urn:custom}UsefulNote'
    assert result.facts[0].evidence['namespace_bindings']['custom']=='urn:custom'


def test_dimension_namespace_aliases_have_the_same_canonical_meaning():
    from backtest_data.fundamentals.xbrl import parse_xbrl
    aliased=INSTANCE.replace(b'xmlns:custom="urn:custom"',b'xmlns:custom="urn:custom" xmlns:other="urn:custom"')
    aliased=aliased.replace(b'dimension="custom:Region">custom:North',b'dimension="other:Region">other:North')
    original=parse_xbrl(INSTANCE)
    aliases=parse_xbrl(aliased)
    assert original.facts[0].dimensions==aliases.facts[0].dimensions=={'{urn:custom}Region':'{urn:custom}North'}
    assert aliases.facts[0].evidence['context_dimension_bindings'][0]['dimension_lexical']=='other:Region'


def test_custom_measure_namespaces_distinguish_same_lexical_units_and_divide_units():
    from backtest_data.fundamentals.xbrl import parse_xbrl
    additions=b'''<xbrli:unit id="widgets"><xbrli:measure xmlns:m="urn:widgets">m:Units</xbrli:measure></xbrli:unit>
    <xbrli:unit id="gadgets"><xbrli:measure xmlns:m="urn:gadgets">m:Units</xbrli:measure></xbrli:unit>
    <xbrli:unit id="speed"><xbrli:divide><xbrli:unitNumerator><xbrli:measure xmlns:m="urn:widgets">m:Units</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure xmlns:m="urn:time">m:Units</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
    <custom:Count contextRef="C" unitRef="widgets">7</custom:Count><custom:Count contextRef="C" unitRef="gadgets">7</custom:Count>
    <custom:Rate contextRef="C" unitRef="speed">7</custom:Rate>'''
    result=parse_xbrl(INSTANCE.replace(b'</xbrli:xbrl>',additions+b'</xbrli:xbrl>'))
    custom=result.facts[2:]
    assert len(custom)==3 and result.complete
    assert [item.unit for item in custom]==['{urn:widgets}Units','{urn:gadgets}Units','{urn:widgets}Units/{urn:time}Units']
    assert custom[0].signature!=custom[1].signature
    assert custom[0].original_unit==custom[1].original_unit=='m:Units'
    assert custom[0].evidence['unit_measure_bindings'][0]['namespace_bindings']['m']=='urn:widgets'


def test_xml_depth_and_numeric_continuation_size_are_explicitly_bounded():
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl
    deep=b'<root>'+b'<n>'*1200+b'</n>'*1200+b'</root>'
    with pytest.raises(UnsafeFinancialDocument,match='unsafe_document:depth_limit'):
        parse_xbrl(deep)
    large=b'1'*100000
    node=b'<ix:nonFraction xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" name="custom:Huge" contextRef="C" unitRef="USD" continuedAt="cont"></ix:nonFraction>'
    cont=b'<ix:continuation xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" id="cont">'+large+b'</ix:continuation>'
    with pytest.raises(UnsafeFinancialDocument,match='unsafe_document:numeric_'):
        parse_xbrl(INSTANCE.replace(b'</xbrli:xbrl>',node+cont+b'</xbrli:xbrl>'))


def test_shared_continuation_expansion_and_chain_length_have_document_limits():
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl
    nodes=b''.join(f'<ix:nonFraction xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" name="custom:N{i}" contextRef="C" unitRef="USD" continuedAt="cont"></ix:nonFraction>'.encode() for i in range(40))
    cont=b'<ix:continuation xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" id="cont">'+b'1'*100000+b'</ix:continuation>'
    document=INSTANCE.replace(b'</xbrli:xbrl>',nodes+cont+b'</xbrli:xbrl>')
    with pytest.raises(UnsafeFinancialDocument,match='unsafe_document:emitted_evidence_limit'):
        parse_xbrl(document,max_numeric_chars=200000,max_numeric_digits=200000,max_emitted_bytes=1000000)
    chain=b''.join(f'<ix:continuation xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" id="c{i}" continuedAt="c{i+1}">1</ix:continuation>'.encode() for i in range(65))
    first=nodes.split(b'</ix:nonFraction>')[0].replace(b'continuedAt="cont"',b'continuedAt="c0"')+b'</ix:nonFraction>'
    with pytest.raises(UnsafeFinancialDocument,match='unsafe_document:continuation_limit'):
        parse_xbrl(INSTANCE.replace(b'</xbrli:xbrl>',first+chain+b'</xbrli:xbrl>'))


def test_zip_budget_is_shared_across_documents_and_depth_limit_is_configurable():
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl, parse_xbrl_zip
    one=parse_xbrl(INSTANCE)
    assert one.inventory.emitted_bytes>0
    payload=BytesIO()
    with ZipFile(payload,'w') as archive:
        archive.writestr('one.xml',INSTANCE)
        archive.writestr('two.xml',INSTANCE)
    with pytest.raises(UnsafeFinancialDocument,match='emitted_evidence_limit'):
        parse_xbrl_zip(payload.getvalue(),max_emitted_bytes=one.inventory.emitted_bytes)
    with pytest.raises(UnsafeFinancialDocument,match='depth_limit'):
        parse_xbrl(INSTANCE,max_depth=2)


def test_explicit_larger_depth_limit_does_not_depend_on_python_recursion_limit():
    from backtest_data.fundamentals.xbrl import parse_xbrl
    nested=b'<ix:nonFraction xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" name="custom:Deep" contextRef="C" unitRef="USD">'
    nested+=b'<span>'*1200+b'1'+b'</span>'*1200+b'</ix:nonFraction>'
    result=parse_xbrl(INSTANCE.replace(b'</xbrli:xbrl>',nested+b'</xbrli:xbrl>'),max_depth=1500)
    assert result.facts[-1].value==Decimal('1') and result.complete


def test_namespace_scopes_share_inherited_bindings_and_charge_metadata_before_retention():
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl
    bindings=' '.join(f'xmlns:n{i}="urn:n{i}"' for i in range(1000))
    siblings=''.join(f'<n xmlns:local="urn:local:{i}"/>' for i in range(500))
    document=f'<root {bindings}>{siblings}</root>'.encode()
    with pytest.raises(UnsafeFinancialDocument,match='namespace|emitted_evidence_limit'):
        parse_xbrl(document,max_emitted_bytes=1)
    result=parse_xbrl(document)
    assert result.inventory.emitted_bytes>0
    assert result.inventory.namespace_entries==1500
    assert result.inventory.namespace_scopes==501


def test_namespace_scope_and_cumulative_namespace_metadata_have_limits():
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl
    document=b'<root xmlns:a="urn:a" xmlns:b="urn:b" />'
    with pytest.raises(UnsafeFinancialDocument,match='namespace_scope_limit'):
        parse_xbrl(document,max_namespace_bindings=1)
    with pytest.raises(UnsafeFinancialDocument,match='namespace_metadata_limit'):
        parse_xbrl(document,max_namespace_bytes=1)


def test_tiny_numeric_exponent_cannot_trigger_unbounded_sql_number_serialization():
    from backtest_data.fundamentals.xbrl import UnsafeFinancialDocument, parse_xbrl
    malicious=b'<custom:HugeExponent contextRef="C" unitRef="USD" scale="1">1e1000000000</custom:HugeExponent>'
    with pytest.raises(UnsafeFinancialDocument,match='numeric_exponent_limit'):
        parse_xbrl(INSTANCE.replace(b'</xbrli:xbrl>',malicious+b'</xbrli:xbrl>'))
    fraction=b'<custom:HugeFraction contextRef="C" unitRef="USD"><xbrli:numerator>1e1000000000</xbrli:numerator><xbrli:denominator>1</xbrli:denominator></custom:HugeFraction>'
    with pytest.raises(UnsafeFinancialDocument,match='numeric_exponent_limit'):
        parse_xbrl(INSTANCE.replace(b'</xbrli:xbrl>',fraction+b'</xbrli:xbrl>'))
