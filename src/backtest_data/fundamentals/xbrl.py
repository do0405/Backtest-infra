"""Bounded, in-memory XBRL/iXBRL numeric extraction.

XML syntax is required; unstructured HTML/text notes are not certified by this
parser. Numeric nodes with unsupported transforms remain in the inventory and
are preserved with an explicit failure. No source document is written to disk.
"""

from dataclasses import replace
from collections import ChainMap
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import PurePosixPath
import re
import json
import stat
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from zipfile import BadZipFile, ZipFile

from .domain import Context, ExtractionInventory, ExtractionIssue, ExtractionResult, SourceFact, stable_hash
from .numeric import NUMERIC_POLICY, deterministic_decimal, exact_scale, exact_difference, exact_sum

XBRLI = 'http://www.xbrl.org/2003/instance'
XBRLDI = 'http://xbrl.org/2006/xbrldi'
IX_NAMESPACES = {'http://www.xbrl.org/2013/inlineXBRL', 'http://www.xbrl.org/2008/inlineXBRL'}
TRANSFORM_NAMESPACES = {f'http://www.xbrl.org/inlineXBRL/transformation/{version}' for version in
                        ('2010-04-20', '2011-07-31', '2015-02-26', '2020-02-12')}
MAX_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 128
MAX_NODES = 200000
MAX_NUMERIC_CHARS = 16384
MAX_NUMERIC_DIGITS = 4096
MAX_CONTINUATIONS = 64
MAX_EMITTED_BYTES = 64 * 1024 * 1024
MAX_NAMESPACE_BINDINGS = 1024
MAX_NAMESPACE_BYTES = 8 * 1024 * 1024
MAX_NUMERIC_EXPONENT = 4096


class _NamespaceScope(ChainMap):
    """Immutable-by-convention local deltas share inherited bindings."""

    def __init__(self, local: dict[str,str], parent: '_NamespaceScope | None'):
        super().__init__(local,*(parent.maps if parent is not None else ()))
        self.binding_count=(parent.binding_count if parent is not None else 0)+sum(
            prefix not in parent if parent is not None else True for prefix in local)


class UnsafeFinancialDocument(ValueError):
    pass


def _consistent_precision_duplicates(facts):
    """Preserve every representation; certify only a strict interval intersection.

    This conservative extraction policy does not implement XBRL calculation
    networks or silently choose a rounded value as a canonical account.
    """
    if any(fact.parse_status != 'ok' for fact in facts):
        return False
    if any(fact.is_nil for fact in facts):
        return all(fact.is_nil for fact in facts)
    if all(fact.value == facts[0].value for fact in facts):
        return True
    intervals = []
    for fact in facts:
        if fact.value is None or fact.decimals is None:
            return False
        if fact.decimals == 'INF':
            intervals.append((fact.value, fact.value, True))
            continue
        try:
            decimals = int(fact.decimals)
        except ValueError:
            return False
        if abs(decimals) > MAX_NUMERIC_EXPONENT:
            return False
        uncertainty = Decimal((0, (5,), -decimals - 1))
        intervals.append((exact_difference(fact.value, uncertainty), exact_sum((fact.value, uncertainty)), False))
    lower, upper = max(item[0] for item in intervals), min(item[1] for item in intervals)
    if lower < upper:
        return True
    if lower == upper and any(item[2] for item in intervals):
        return all(item[2] and item[0] == lower or item[0] < lower < item[1] for item in intervals)
    return False


def _split(tag: str) -> tuple[str, str]:
    return tuple(tag[1:].split('}', 1)) if tag.startswith('{') else ('', tag)


def _date(node: ET.Element, name: str) -> date | None:
    text = node.findtext(f'.//{{{XBRLI}}}{name}')
    return date.fromisoformat(text.strip()) if text else None


def _content(node: ET.Element, *, max_chars: int | None = None) -> str:
    parts=[]
    length=0
    pending=[node]
    while pending:
        item=pending.pop()
        if isinstance(item,str):
            content=item
        else:
            content=item.text or ''
            for child in reversed(item):
                pending.append(child.tail or '')
                ns,name=_split(child.tag)
                if not (ns in IX_NAMESPACES and name=='exclude'):
                    pending.append(child)
        length+=len(content)
        if max_chars is not None and length>max_chars:
            raise UnsafeFinancialDocument('unsafe_document:numeric_lexical_limit')
        parts.append(content)
    return ''.join(parts)


def _number(text: str, format_name: str | None, namespaces: dict[str, str], *,
              max_exponent: int = MAX_NUMERIC_EXPONENT) -> Decimal:
    text = text.strip().replace('\u00a0', ' ')
    if format_name:
        prefix, _, local = format_name.partition(':')
        if namespaces.get(prefix) not in TRANSFORM_NAMESPACES:
            raise ValueError('unsupported_transform')
        if local in {'num-dot-decimal', 'numdotdecimal', 'numcommadot'}:
            if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:[, ][0-9]{3})+)(?:\.[0-9]+)?', text):
                raise ValueError('invalid_numeric_format')
            text = text.replace(',', '').replace(' ', '')
        elif local in {'num-comma-decimal', 'numcommadecimal', 'numdotcomma'}:
            if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:[. ][0-9]{3})+)(?:,[0-9]+)?', text):
                raise ValueError('invalid_numeric_format')
            text = text.replace('.', '').replace(' ', '').replace(',', '.')
        elif local in {'zero-dash', 'zerodash', 'numdash'}:
            if text not in {'-', '\u2013', '\u2014'}:
                raise ValueError('invalid_numeric_format')
            text = '0'
        else:
            raise ValueError('unsupported_transform')
    if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', text):
        raise ValueError('invalid_numeric')
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError('invalid_numeric')
    if abs(number.as_tuple().exponent)>max_exponent:
        raise UnsafeFinancialDocument('unsafe_document:numeric_exponent_limit')
    return number


@deterministic_decimal
def parse_xbrl(data: bytes, *, max_bytes: int = MAX_BYTES, max_depth: int = MAX_DEPTH,
               max_nodes: int = MAX_NODES, max_numeric_chars: int = MAX_NUMERIC_CHARS,
               max_numeric_digits: int = MAX_NUMERIC_DIGITS, max_continuations: int = MAX_CONTINUATIONS,
               max_emitted_bytes: int = MAX_EMITTED_BYTES,
               max_namespace_bindings: int = MAX_NAMESPACE_BINDINGS,
               max_namespace_bytes: int = MAX_NAMESPACE_BYTES,
               max_numeric_exponent: int = MAX_NUMERIC_EXPONENT) -> ExtractionResult:
    """Extract under explicit byte/depth/node/numeric/expansion budgets.

    Limit failures raise UnsafeFinancialDocument with unsafe_document reasons.
    ``inventory.emitted_bytes`` counts the charged XML/value/metadata evidence
    budget, conservatively including repeated and deduplicated source nodes.
    """
    if len(data) > max_bytes:
        raise UnsafeFinancialDocument('document_size_limit')
    # NUL removal also catches UTF-16/32 ASCII declarations before XML parsing.
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data.replace(b'\x00', b''), re.I):
        raise UnsafeFinancialDocument('DTD_and_entities_forbidden')
    node_namespaces: dict[int, _NamespaceScope] = {}
    stack: list[_NamespaceScope] = []
    pending: dict[str, str] = {}
    node_count=0
    emitted_bytes=namespace_bytes=namespace_entries=namespace_scopes=0

    def reserve(part: str) -> None:
        nonlocal emitted_bytes
        size=len(part.encode('utf-8'))
        if emitted_bytes+size>max_emitted_bytes:
            raise UnsafeFinancialDocument('unsafe_document:emitted_evidence_limit')
        emitted_bytes+=size

    def reserve_json(value: object) -> None:
        for chunk in json.JSONEncoder(ensure_ascii=False,separators=(',',':')).iterencode(value):
            reserve(chunk)
    try:
        for event, value in ET.iterparse(BytesIO(data), events=('start-ns', 'start', 'end')):
            if event == 'start-ns':
                prefix, uri = value
                size=len(prefix.encode('utf-8'))+len(uri.encode('utf-8'))
                if namespace_bytes+size>max_namespace_bytes:
                    raise UnsafeFinancialDocument('unsafe_document:namespace_metadata_limit')
                reserve_json({'prefix':prefix,'namespace':uri})
                namespace_bytes+=size
                namespace_entries+=1
                pending[prefix] = uri
            elif event == 'start':
                if len(stack)>=max_depth:
                    raise UnsafeFinancialDocument('unsafe_document:depth_limit')
                node_count+=1
                if node_count>max_nodes:
                    raise UnsafeFinancialDocument('unsafe_document:node_limit')
                if pending or not stack:
                    active=_NamespaceScope(pending,stack[-1] if stack else None)
                    if active.binding_count>max_namespace_bindings:
                        raise UnsafeFinancialDocument('unsafe_document:namespace_scope_limit')
                    namespace_scopes+=1
                    pending={}
                else:
                    active=stack[-1]
                stack.append(active)
                node_namespaces[id(value)] = active
            else:
                stack.pop()
        root = value
    except ET.ParseError as exc:
        raise ValueError(f'invalid_xml: {exc}') from exc

    def qname(tag: str, node: ET.Element) -> str:
        uri, local = _split(tag)
        for prefix, namespace in node_namespaces[id(node)].items():
            if namespace == uri:
                return f'{prefix}:{local}' if prefix else local
        return tag

    def expanded_qname(lexical: str, node: ET.Element) -> str:
        lexical=lexical.strip()
        prefix,separator,local=lexical.partition(':')
        bindings=node_namespaces[id(node)]
        if separator:
            if not prefix or not local or ':' in local or prefix not in bindings:
                raise ValueError('unbound_or_invalid_qname')
            return f'{{{bindings[prefix]}}}{local}'
        if not prefix:
            raise ValueError('unbound_or_invalid_qname')
        return f'{{{bindings[""]}}}{prefix}' if bindings.get('') else prefix

    def scoped_xml(node: ET.Element, inherited: dict[str,str] | None = None) -> str:
        """Retain bindings used only by QName attribute/text content.

        ElementTree drops those declarations during tostring(). A small XML
        serializer makes each stored fragment self-contained and preserves
        local prefix rebinding without changing the source node tree.
        """
        parts=[]

        def emit(part):
            reserve(part)
            parts.append(part)

        def emit_escaped(value, *, attribute=False):
            # Check a conservative expansion bound before allocating escaped
            # text; quoteattr/escape can multiply adversarial '&' strings.
            if emitted_bytes+6*len(value.encode('utf-8'))+2>max_emitted_bytes:
                raise UnsafeFinancialDocument('unsafe_document:emitted_evidence_limit')
            emit(quoteattr(value) if attribute else escape(value))

        pending_elements=[('element',node,inherited or {})]
        while pending_elements:
            operation,element,inherited_bindings=pending_elements.pop()
            if operation=='close':
                emit('</'+element+'>')
                continue
            if operation=='tail':
                emit_escaped(element)
                continue
            bindings=dict(node_namespaces[id(element)])

            def lexical_name(expanded: str, *, attribute: bool = False) -> str:
                uri,local=_split(expanded)
                if not uri:
                    return local
                if uri=='http://www.w3.org/XML/1998/namespace':
                    return 'xml:'+local
                for prefix,namespace in bindings.items():
                    if namespace==uri and (prefix or not attribute):
                        return f'{prefix}:{local}' if prefix else local
                index=0
                while f'evidence_ns{index}' in bindings:
                    index+=1
                prefix=f'evidence_ns{index}'
                bindings[prefix]=uri
                return f'{prefix}:{local}'

            tag=lexical_name(element.tag)
            attributes=[(lexical_name(key,attribute=True),value) for key,value in element.attrib.items()]
            declarations=[('xmlns:'+prefix if prefix else 'xmlns',uri) for prefix,uri in bindings.items()
                          if inherited_bindings.get(prefix)!=uri]
            emit('<'+tag)
            for name,value in declarations+attributes:
                emit(' '+name+'=')
                emit_escaped(value,attribute=True)
            emit('>')
            emit_escaped(element.text or '')
            pending_elements.append(('close',tag,None))
            for child in reversed(element):
                pending_elements.append(('tail',child.tail or '',None))
                pending_elements.append(('element',child,bindings))
        return ''.join(parts)

    contexts: dict[str, Context] = {}
    units: dict[str, str] = {}
    canonical_units: dict[str, str] = {}
    unit_bindings: dict[str,list[dict]] = {}
    invalid_units=set()
    issues: list[ExtractionIssue] = []
    for node in root.iter():
        ns, local = _split(node.tag)
        if ns == XBRLI and local == 'context':
            identifier = node.find(f'.//{{{XBRLI}}}identifier')
            dimensions: dict[str, str] = {}
            dimension_bindings=[]
            for member in node.iter():
                mns, mlocal = _split(member.tag)
                if mns == XBRLDI and mlocal in {'explicitMember', 'typedMember'}:
                    dimension_lexical=member.get('dimension','')
                    member_lexical=_content(member).strip() if mlocal=='explicitMember' else None
                    try:
                        dimension=expanded_qname(dimension_lexical,member)
                        member_value=(expanded_qname(member_lexical,member) if mlocal=='explicitMember'
                                      else ''.join(scoped_xml(child) for child in member))
                    except ValueError as exc:
                        issues.append(ExtractionIssue(str(exc),node.get('id',''),'dimension QName cannot be resolved'))
                        dimension=dimension_lexical
                        member_value=member_lexical or ''.join(scoped_xml(child) for child in member)
                    dimension_bindings.append({'kind':mlocal,'dimension_lexical':dimension_lexical,
                                               'member_lexical':member_lexical,'dimension_qname':dimension,
                                               'member_qname':member_value if mlocal=='explicitMember' else None,
                                               'namespace_bindings':dict(node_namespaces[id(member)]),
                                               'raw_xml':scoped_xml(member)})
                    if dimension in dimensions and dimensions[dimension]!=member_value:
                        issues.append(ExtractionIssue('conflicting_context_dimension',node.get('id',''),dimension))
                    else:
                        dimensions[dimension]=member_value
            instant = _date(node, 'instant')
            context = Context(node.get('id', ''), _content(identifier).strip() if identifier is not None else None,
                              identifier.get('scheme') if identifier is not None else None,
                              'instant' if instant else ('forever' if node.find(f'.//{{{XBRLI}}}forever') is not None else 'duration'),
                              _date(node, 'startDate'), instant or _date(node, 'endDate'), dimensions,
                              scoped_xml(node),evidence={'namespace_bindings':dict(node_namespaces[id(node)]),
                                                         'context_dimension_bindings':dimension_bindings})
            if context.id in contexts:
                issues.append(ExtractionIssue('duplicate_context_id', context.id, 'context identity repeated'))
            contexts[context.id] = context
            reserve_json(context.evidence)
        elif ns == XBRLI and local == 'unit':
            measures=[]
            def measure_name(measure,role):
                raw_measure=_content(measure).strip()
                try:
                    expanded=expanded_qname(raw_measure,measure)
                except ValueError as exc:
                    invalid_units.add(node.get('id',''))
                    issues.append(ExtractionIssue(str(exc),node.get('id',''),'unit measure QName cannot be resolved'))
                    expanded=raw_measure
                measures.append({'role':role,'measure_lexical':raw_measure,'measure_qname':expanded,
                                 'namespace_bindings':dict(node_namespaces[id(measure)])})
                uri,name=_split(expanded)
                return name if uri in {'http://www.xbrl.org/2003/iso4217',XBRLI} else expanded
            numerator = node.find(f'{{{XBRLI}}}divide/{{{XBRLI}}}unitNumerator')
            denominator = node.find(f'{{{XBRLI}}}divide/{{{XBRLI}}}unitDenominator')
            if numerator is not None and denominator is not None:
                unit = '*'.join(_content(m).strip() for m in numerator) + '/' + '*'.join(_content(m).strip() for m in denominator)
                canonical_unit='*'.join(sorted(measure_name(m,'numerator') for m in numerator))+'/'+'*'.join(sorted(measure_name(m,'denominator') for m in denominator))
            else:
                unit = '*'.join(_content(m).strip() for m in node)
                canonical_unit='*'.join(sorted(measure_name(m,'measure') for m in node))
            units[node.get('id', '')] = unit
            canonical_units[node.get('id','')]=canonical_unit
            unit_bindings[node.get('id','')]=measures
            reserve_json(measures)
    continuations = {node.get('id'): node for node in root.iter() if _split(node.tag)[0] in IX_NAMESPACES and _split(node.tag)[1] == 'continuation'}
    facts: list[SourceFact] = []
    signatures: dict[str, int] = {}
    duplicate_node_ids: dict[int, list[str]] = {}
    identities: dict[tuple[object, ...], list[SourceFact]] = {}
    numeric_nodes = deduplicated = unsupported = 0
    for node in root.iter():
        ns, local = _split(node.tag)
        inline = ns in IX_NAMESPACES and local in {'nonFraction', 'fraction'}
        if not inline and ('unitRef' not in node.attrib or ns in IX_NAMESPACES):
            continue
        numeric_nodes += 1
        node_id = node.get('id') or f'node:{numeric_nodes}'
        context_id = node.get('contextRef', '')
        context = contexts.get(context_id, Context(context_id))
        concept = node.get('name', '') if inline else qname(node.tag, node)
        taxonomy = node_namespaces[id(node)].get(concept.partition(':')[0], '') if inline else ns
        raw_parts=[_content(node,max_chars=max_numeric_chars)]
        is_nil = node.get('{http://www.w3.org/2001/XMLSchema-instance}nil') in {'true', '1'}
        status = 'ok'
        number = None
        scale = None
        concept_qname=node.tag if not inline else None
        raw_reserved=False
        try:
            if inline:
                concept_qname=expanded_qname(concept,node)
            continued = node.get('continuedAt')
            seen: set[str] = set()
            raw_length=len(raw_parts[0])
            while continued:
                if continued in seen or continued not in continuations:
                    raise ValueError('invalid_continuation')
                seen.add(continued)
                if len(seen)>max_continuations:
                    raise UnsafeFinancialDocument('unsafe_document:continuation_limit')
                continuation = continuations[continued]
                part=_content(continuation,max_chars=max_numeric_chars)
                raw_length+=len(part)
                if raw_length>max_numeric_chars:
                    raise UnsafeFinancialDocument('unsafe_document:numeric_lexical_limit')
                raw_parts.append(part)
                continued = continuation.get('continuedAt')
            if sum(sum(character.isdigit() for character in part) for part in raw_parts)>max_numeric_digits:
                raise UnsafeFinancialDocument('unsafe_document:numeric_digit_limit')
            for part in raw_parts:
                reserve(part)
            raw_reserved=True
            raw=''.join(raw_parts)
            if context_id not in contexts:
                raise ValueError('missing_context')
            if node.get('unitRef') not in units:
                raise ValueError('missing_unit')
            if node.get('unitRef') in invalid_units:
                raise ValueError('invalid_unit')
            if not is_nil:
                numerator = next((child for child in node if _split(child.tag)[0] in IX_NAMESPACES | {XBRLI} and _split(child.tag)[1] == 'numerator'), None)
                denominator = next((child for child in node if _split(child.tag)[0] in IX_NAMESPACES | {XBRLI} and _split(child.tag)[1] == 'denominator'), None)
                if inline and local == 'fraction' or numerator is not None or denominator is not None:
                    if numerator is None or denominator is None:
                        raise ValueError('invalid_fraction')
                    number = _number(_content(numerator), numerator.get('format'), node_namespaces[id(numerator)],max_exponent=max_numeric_exponent) / _number(_content(denominator), denominator.get('format'), node_namespaces[id(denominator)],max_exponent=max_numeric_exponent)
                else:
                    number = _number(raw, node.get('format'), node_namespaces[id(node)],max_exponent=max_numeric_exponent)
                if node.get('scale') is not None:
                    scale = int(node.get('scale', '0'))
                    if abs(scale) > 1000:
                        raise ValueError('scale_limit')
                    number = exact_scale(number,scale)
                if node.get('sign') not in {None, '-'}:
                    raise ValueError('invalid_sign')
                if node.get('sign') == '-':
                    number = number.copy_negate()
                if abs(number.as_tuple().exponent)>max_numeric_exponent:
                    raise UnsafeFinancialDocument('unsafe_document:numeric_exponent_limit')
        except UnsafeFinancialDocument:
            raise
        except (ValueError, InvalidOperation, ZeroDivisionError) as exc:
            number = None
            status = str(exc) or 'invalid_numeric'
            unsupported += 1
            issues.append(ExtractionIssue(status, node_id, f'cannot certify {concept} numeric value'))
            if not raw_reserved:
                for part in raw_parts:
                    reserve(part)
            raw=''.join(raw_parts)
        original_unit = units.get(node.get('unitRef', ''))
        unit = canonical_units.get(node.get('unitRef',''))
        evidence={'concept_lexical':concept,'concept_qname':concept_qname,
                  'namespace_bindings':dict(node_namespaces[id(node)]),
                  'context_dimension_bindings':context.evidence.get('context_dimension_bindings',[]),
                  'unit_measure_bindings':unit_bindings.get(node.get('unitRef',''),[]),'numeric_policy':NUMERIC_POLICY}
        reserve_json({'evidence':evidence,'dimensions':context.dimensions,'unit':unit,'original_unit':original_unit})
        fact = SourceFact(node_id, concept, taxonomy, context_id, number, raw, unit, original_unit,
                          dimensions=context.dimensions, period_kind=context.period_kind,
                          period_start=context.period_start, period_end=context.period_end, scope=context.scope,
                          decimals=node.get('decimals'), precision=node.get('precision'), scale=scale,
                          sign=node.get('sign'), is_nil=is_nil, format=node.get('format'), parse_status=status,
                          raw_xml=scoped_xml(node), source_node_ids=(node_id,),
                          evidence=evidence)
        identity = (fact.taxonomy, fact.concept, context_id, unit)
        if fact.signature in signatures:
            index = signatures[fact.signature]
            duplicate_node_ids.setdefault(index, list(facts[index].source_node_ids)).append(node_id)
            deduplicated += 1
            continue
        identities.setdefault(identity, []).append(fact)
        signatures[fact.signature] = len(facts)
        facts.append(fact)
    for index, node_ids in duplicate_node_ids.items():
        facts[index] = replace(facts[index], source_node_ids=tuple(node_ids))
    # Each completed group is checked once. A large group cannot force
    # quadratic interval reconstruction inside the extraction loop.
    for group in identities.values():
        if len(group) > 1 and not _consistent_precision_duplicates(group):
            issues.append(ExtractionIssue('conflicting_duplicate', group[-1].id,
                                         f'{group[-1].concept} has conflicting same-context values'))
    return ExtractionResult(tuple(facts), contexts, units,
                            ExtractionInventory(numeric_nodes, numeric_nodes, tuple(contexts), tuple(units),
                                                deduplicated, unsupported_nodes=unsupported,emitted_bytes=emitted_bytes,
                                                namespace_entries=namespace_entries,namespace_scopes=namespace_scopes), tuple(issues),
                            parser_version='financial_extraction_v5')


def parse_xbrl_zip(data: bytes, *, max_bytes: int = MAX_BYTES, max_entries: int = 1000,
                   **parser_limits) -> tuple[ExtractionResult, ...]:
    """Forward parser limits; the emitted evidence budget covers the whole ZIP."""
    if len(data) > max_bytes:
        raise UnsafeFinancialDocument('archive_size_limit')
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > max_entries or sum(entry.file_size for entry in entries) > max_bytes:
                raise UnsafeFinancialDocument('archive_expansion_limit')
            for entry in entries:
                path = PurePosixPath(entry.filename.replace('\\', '/'))
                mode = entry.external_attr >> 16
                if path.is_absolute() or '..' in path.parts or ':' in entry.filename or stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise UnsafeFinancialDocument('unsafe_archive_entry')
            remaining=parser_limits.pop('max_emitted_bytes',MAX_EMITTED_BYTES)
            results=[]
            for entry in entries:
                if not entry.is_dir() and PurePosixPath(entry.filename).suffix.lower() in {'.xml', '.xbrl', '.xhtml', '.htm', '.html'}:
                    parsed=parse_xbrl(archive.read(entry),max_bytes=max_bytes,max_emitted_bytes=remaining,**parser_limits)
                    remaining-=parsed.inventory.emitted_bytes
                    results.append(parsed)
            return tuple(results)
    except BadZipFile as exc:
        raise UnsafeFinancialDocument('invalid_archive') from exc
