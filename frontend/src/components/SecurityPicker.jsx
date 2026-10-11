import React, { useState } from 'react';
import { request } from '../api.js';
import { basisLabel, exact, shortId } from '../model.js';
import { Panel } from './Shared.jsx';

export function SecurityPicker({ start, end, selections, onSelectionsChange, onNames, names = {}, disabled }) {
  const [query, setQuery] = useState('');
  const [market, setMarket] = useState('both');
  const [results, setResults] = useState(null);
  const [security, setSecurity] = useState(null);
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  async function search(event) {
    event.preventDefault(); setError(''); setLoading(true);
    try { const rows = await request(`/api/securities?${new URLSearchParams({ query, market, limit: '20' })}`); if (!Array.isArray(rows)) throw new Error('종목 검색 응답을 읽을 수 없습니다.'); setResults(rows); }
    catch (err) { setResults(null); setError(err.message); } finally { setLoading(false); }
  }
  async function choose(value) {
    setSecurity(value); setSeries(null); setError(''); setLoading(true); onNames(value);
    try { const rows = await request(`/api/series?${new URLSearchParams({ security_id: value.security_id, start, end })}`); if (!Array.isArray(rows)) throw new Error('가격 자료 목록을 읽을 수 없습니다.'); setSeries(rows); }
    catch (err) { setError(err.message); } finally { setLoading(false); }
  }
  function toggle(value) {
    if (selections.some(item => item.series_key === value.series_key)) { onSelectionsChange(selections.filter(item => item.series_key !== value.series_key)); return; }
    if (selections.length >= 20) { setError('한 번에 가격 자료를 최대 20개 선택할 수 있습니다.'); return; }
    if (selections.some(item => item.market !== value.market || item.currency !== value.currency)) { setError('한 연구에서는 같은 시장과 통화의 자료를 선택하세요.'); return; }
    onSelectionsChange([...selections, { ...value, name: security.name || security.identifier }]);
  }
  return <Panel title="연구 종목" eyebrow="01 / SELECT" extra={<span className="badge">{new Set(selections.map(item => item.security_id)).size}개 선택</span>}>
    <form onSubmit={search} className="search-form"><label>종목명 또는 코드<input value={query} onChange={event => setQuery(event.target.value)} maxLength={80} placeholder="삼성전자, AAPL, 005930…" /></label><div className="field-row"><label>시장<select value={market} onChange={event => setMarket(event.target.value)}><option value="both">한국 + 미국</option><option value="KR">한국</option><option value="US">미국</option></select></label><button className="secondary" disabled={loading || disabled}>종목 검색</button></div></form>
    {error && <p role="alert" className="error-box">{error}</p>}{loading && <p role="status" className="muted">실제 자료를 조회하고 있습니다…</p>}
    {results && <div className="security-list">{results.map(item => <button type="button" key={item.security_id} className={`security-row ${security?.security_id === item.security_id ? 'active' : ''}`} onClick={() => choose(item)} disabled={loading || disabled}><span className="market-mark">{item.market}</span><span><strong>{item.name || item.identifier}</strong><small>{item.identifier} · 과거 동일성 {item.historical_identity === 'unknown' ? '미확인' : exact(item.historical_identity)}</small></span><span aria-hidden="true">＋</span></button>)}{!results.length && <p className="muted">원래 대상 안에서 일치하는 종목이 없습니다.</p>}</div>}
    {security && series && <div className="series-list"><h3>{security.name || security.identifier} · 가격 자료</h3><p className="muted">출처와 가격 기준을 확인한 후 직접 선택하세요. 원본과 조정 자료를 자동으로 섞지 않습니다.</p>{series.map(item => <label key={item.series_key} className="series-choice"><input type="checkbox" checked={selections.some(value => value.series_key === item.series_key)} onChange={() => toggle(item)} disabled={disabled} /><span><strong>{item.source} · {basisLabel(item.basis)}</strong><small>{item.currency} · {item.feed || '피드 미지정'} · {item.venue || '거래소 미지정'}</small><small>조정: {item.adjustments?.join(', ') || '없음'} · 거래량: {item.volume_basis} · 단위: {item.quantity_unit}</small><small>범위: {item.trade_scope} · {shortId(item.series_key)}</small></span></label>)}{!series.length && <p className="muted">이 기간에 사용할 가격 자료가 없습니다. 기간을 바꾼 뒤 다시 검색하세요.</p>}</div>}
    {selections.length > 0 && <div className="selected-list"><h3>선택한 자료 <span>{selections.length}/20</span></h3>{selections.map(item => <div className="selected-item" key={item.series_key}><span><strong>{names[item.security_id] || item.name || shortId(item.security_id)}</strong><small>{item.source} · {basisLabel(item.basis)} · {item.currency}</small></span><button type="button" aria-label={`${names[item.security_id] || item.name || item.security_id} ${basisLabel(item.basis)} 제거`} disabled={disabled} onClick={() => onSelectionsChange(selections.filter(value => value.series_key !== item.series_key))}>×</button></div>)}</div>}
  </Panel>;
}
