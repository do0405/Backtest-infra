import React, { useEffect, useMemo, useRef, useState } from 'react';
import { loadActualDataset, request, validateActualResult } from './api.js';
import { basisLabel, exact, featureMapping, matchSeries, reasonLabel, selectionPayload, shortId } from './model.js';
import { ResearchChart } from './components/Charts.jsx';
import { SecurityPicker } from './components/SecurityPicker.jsx';
import { TraceExplorer } from './components/TraceExplorer.jsx';
import { AnalysisReport } from './components/AnalysisReport.jsx';
import { Empty, Evidence, ExactTable, JsonDetails, Panel } from './components/Shared.jsx';

const strategyNames = { trend_sma: '이동평균 추세', mean_reversion_rsi2: 'RSI 단기 회귀', price_momentum: '가격 모멘텀' };
const emptyRows = [];
const defaultParameters = { fast_period: 50, slow_period: 200, rsi_period: 2, entry_rsi: '5', exit_rsi: '50', max_hold_sessions: 5, momentum_lookback: 252, momentum_skip: 21, top_n: 20, rebalance_every: 21, allocation: '1', positive_only: true };

export default function App() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [start, setStart] = useState('2026-09-01');
  const [end, setEnd] = useState('2026-10-07');
  const [cutoff, setCutoff] = useState('2026-10-09T00:00:00+09:00');
  const [selections, setSelections] = useState([]);
  const [mapping, setMapping] = useState({});
  const [names, setNames] = useState({});
  const [dataset, setDataset] = useState(null);
  const [activeSeries, setActiveSeries] = useState('');
  const [features, setFeatures] = useState(null);
  const [featureError, setFeatureError] = useState('');
  const [sma, setSma] = useState(20);
  const [publications, setPublications] = useState(null);
  const [publicationError, setPublicationError] = useState('');
  const [strategy, setStrategy] = useState('trend_sma');
  const [parameters, setParameters] = useState(defaultParameters);
  const [cash, setCash] = useState('10000000');
  const [fee, setFee] = useState('0');
  const [slippage, setSlippage] = useState('0');
  const [result, setResult] = useState(null);
  const [session, setSession] = useState('');
  const [tab, setTab] = useState('price');
  const [savedId, setSavedId] = useState('');
  const [notice, setNotice] = useState('');
  const [screen, setScreen] = useState(null);
  const [screenBasis, setScreenBasis] = useState('as_traded');
  const [screenFeature, setScreenFeature] = useState('momentum');
  const [screenTop, setScreenTop] = useState(20);
  const restored = useRef(false);
  const selectedSeries = selections.find(item => item.series_key === activeSeries);
  const bars = useMemo(() => dataset && selectedSeries ? dataset.bars.filter(bar => matchSeries(bar.series, selectedSeries)) : emptyRows, [dataset, selectedSeries]);
  const sessions = dataset?.sessions || emptyRows;
  const selectedCount = dataset?.selected_security_ids?.length || new Set(selections.map(item => item.security_id)).size;
  const raw = selections.filter(item => item.basis === 'as_traded');

  function invalidate() {
    setDataset(null); setResult(null); setScreen(null); setFeatures(null);
    setFeatureError(''); setPublications(null); setPublicationError('');
    setActiveSeries(''); setSession(''); setNotice('');
  }
  function selectSources(values) { setSelections(values); invalidate(); }
  function addName(item) { setNames(current => ({ ...current, [item.security_id]: item.name || item.identifier || item.security_id })); }
  async function action(label, work) {
    setError(''); setNotice(''); setBusy(label);
    try { await work(); } catch (err) { setError(err.message); } finally { setBusy(''); }
  }
  function acceptResult(value) {
    validateActualResult(value); setResult(value); setSavedId(value.result_id); setSession(value.rows.at(-1).session); setTab('performance');
    const url = new URL(window.location.href); url.searchParams.set('result', value.result_id); window.history.replaceState(null, '', url);
  }
  async function restore(identifier) {
    if (!/^[0-9a-f]{64}$/.test(identifier)) throw new Error('저장 결과 ID는 64자리 식별자여야 합니다.');
    invalidate();
    const value = validateActualResult(await request(`/api/results/${identifier}`));
    const input = await loadActualDataset(`/api/datasets/${value.dataset_id}`);
    const identities = Object.entries(value.series_identities).map(([series_key, identity]) => ({ ...identity, series_key }));
    setSelections(identities); setDataset(input); setActiveSeries(value.raw_series_keys[0]); setMapping(Object.fromEntries(value.raw_series_keys.map(key => [key, value.feature_series_keys.find(feature => value.series_identities[feature].security_id === value.series_identities[key].security_id)])));
    setStart(input.sessions[0]); setEnd(input.sessions.at(-1)); setCutoff(input.cutoff);
    setStrategy(value.run_config.strategy); setParameters({ ...defaultParameters, ...value.run_config.parameters }); setCash(value.run_config.initial_cash); setFee(value.run_config.fee_bps); setSlippage(value.run_config.slippage_bps);
    acceptResult(value);
    await Promise.allSettled(input.selected_security_ids.map(async id => {
      const rows = await request(`/api/securities?${new URLSearchParams({ query: id, market: identities.find(item => item.security_id === id)?.market || 'both', limit: '1' })}`);
      if (rows[0]?.security_id === id) addName(rows[0]);
    }));
  }
  useEffect(() => {
    request('/api/health').then(setHealth).catch(err => setError(err.message));
    if (!restored.current) { restored.current = true; const id = new URLSearchParams(window.location.search).get('result'); if (id) action('저장된 결과를 불러오는 중', () => restore(id)); }
  }, []);
  useEffect(() => {
    setFeatures(null); setFeatureError('');
    if (!dataset || !activeSeries) return;
    const controller = new AbortController();
    request(`/api/features?${new URLSearchParams({ dataset_id: dataset.dataset_id, series_key: activeSeries, sma_period: String(sma), rsi_period: '2' })}`, { signal: controller.signal }).then(value => {
      if (!Array.isArray(value.rows)) throw new Error('지표 자료 응답을 읽을 수 없습니다.'); setFeatures(value);
    }).catch(err => { if (err.name !== 'AbortError') setFeatureError(err.message); });
    return () => controller.abort();
  }, [dataset, activeSeries, sma]);
  useEffect(() => {
    setPublications(null); setPublicationError('');
    if (!dataset || !selectedSeries) return;
    const controller = new AbortController();
    request(`/api/publications?${new URLSearchParams({ security_id: selectedSeries.security_id, cutoff: dataset.cutoff })}`, { signal: controller.signal }).then(setPublications).catch(err => { if (err.name !== 'AbortError') setPublicationError(err.message); });
    return () => controller.abort();
  }, [dataset, selectedSeries?.security_id]);
  async function loadDataset() {
    if (!selections.length) throw new Error('종목의 가격 자료를 먼저 선택하세요.');
    invalidate();
    const value = await loadActualDataset('/api/datasets', { method: 'POST', body: { selections: selections.map(selectionPayload), start, end, cutoff } });
    setDataset(value); setActiveSeries(selections[0].series_key); setSession(value.sessions.at(-1));
  }
  async function run() {
    if (!dataset) throw new Error('선택 자료를 먼저 로드하세요.');
    const mapped = featureMapping(selections, mapping);
    if ([...mapped.raw_series_keys, ...mapped.feature_series_keys].some(key => !dataset.bars.some(bar => matchSeries(bar.series, selections.find(item => item.series_key === key))))) throw new Error('선택한 거래 원본 또는 지표 자료에 사용 가능한 관측이 없습니다. 선택을 확인하세요.');
    setResult(null);
    const keys = strategy === 'trend_sma' ? ['fast_period', 'slow_period', 'allocation'] : strategy === 'mean_reversion_rsi2' ? ['rsi_period', 'entry_rsi', 'exit_rsi', 'max_hold_sessions', 'allocation'] : ['momentum_lookback', 'momentum_skip', 'top_n', 'rebalance_every', 'allocation', 'positive_only'];
    const value = await request('/api/backtests', { method: 'POST', body: { dataset_id: dataset.dataset_id, ...mapped, strategy, parameters: Object.fromEntries(keys.map(key => [key, parameters[key]])), initial_cash: cash, fee_bps: fee, slippage_bps: slippage } });
    acceptResult(value);
  }
  const featureRow = features?.rows?.find(row => row.session === session);
  const navRow = result?.rows?.find(row => row.session === session);
  return <>
    <header className="topbar"><a className="brand" href="/" aria-label="리서치 워크스페이스 처음으로"><span className="brand-icon">↗</span><strong>Backtest<span>RESEARCH WORKSPACE</span></strong></a><div className="top-right"><span className="local-status"><i className={health ? 'ready' : ''} />{health ? '로컬 연구 서버 연결됨' : '서버 연결 확인 중'}</span><span className="badge subtle">실제 자료 · 연구용</span></div></header>
    <main className="workspace"><section className="hero"><div><span className="eyebrow">FROM DATA TO DECISIONS</span><h1>성과 너머,<br />선택의 이유까지.</h1><p>가격, 전략, 그리고 매일의 종목 선택을 같은 근거 위에서 살펴보세요.</p></div><div className="scope-stat"><span>원래 수집 대상</span><strong>17,758<small>종목</small></strong><p>현재 선택 <b>{selectedCount}</b>개 · 한국 / 미국</p><small>선택한 부분집합의 연구 · 과거 전체 시장 구성 미검증</small></div></section>
      {error && <div className="error-box global-error" role="alert"><strong>자료 또는 실행을 확인해 주세요</strong><p>{error}</p><button type="button" onClick={() => setError('')} aria-label="오류 안내 닫기">×</button></div>}
      {busy && <p className="busy-banner" role="status"><span className="spinner" />{busy}…</p>}{notice && <p role="status" className="notice">{notice}</p>}
      <div className="workspace-grid"><aside className="controls">
        <Panel title="연구 기간" eyebrow="INPUT WINDOW"><div className="field-row"><label>시작일<input type="date" min="2016-01-01" max="2026-10-07" value={start} disabled={!!busy} onChange={event => { setStart(event.target.value); invalidate(); }} /></label><label>종료일<input type="date" min={start} max="2026-10-07" value={end} disabled={!!busy} onChange={event => { setEnd(event.target.value); invalidate(); }} /></label></div><label>자료 사용 마감 · 시차 포함<input type="text" value={cutoff} disabled={!!busy} onChange={event => { setCutoff(event.target.value); invalidate(); }} aria-describedby="cutoff-help" /></label><p id="cutoff-help" className="muted">예: 2026-10-09T00:00:00+09:00. 완료된 일자의 자료만 사용합니다.</p></Panel>
        <SecurityPicker start={start} end={end} selections={selections} onSelectionsChange={selectSources} onNames={addName} names={names} disabled={!!busy} />
        <button className="primary wide" type="button" disabled={!selections.length || !!busy} onClick={() => action('선택한 실제 자료를 고정하는 중', loadDataset)}>선택 자료 로드 <span>→</span></button>
        <Panel title="전략과 실행 조건" eyebrow="02 / RESEARCH"><label>전략<select value={strategy} disabled={!!busy} onChange={event => setStrategy(event.target.value)}>{Object.entries(strategyNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          {raw.map(item => <label key={item.series_key}>지표 자료 · {names[item.security_id] || item.name || shortId(item.security_id)}<select value={mapping[item.series_key] || ''} disabled={!!busy} onChange={event => setMapping(current => ({ ...current, [item.series_key]: event.target.value }))}><option value="">사용할 출처를 직접 선택</option>{selections.filter(value => value.security_id === item.security_id).map(value => <option key={value.series_key} value={value.series_key}>{value.source} · {basisLabel(value.basis)} · {value.feed}</option>)}</select></label>)}
          <p className="muted">체결·평가는 거래 원본을 사용합니다. 조정 자료는 지표 출처로 직접 선택할 때만 사용합니다.</p>
          <div className="parameter-grid">{(strategy === 'trend_sma' ? [['fast_period', '단기 평균 거래일'], ['slow_period', '장기 평균 거래일']] : strategy === 'mean_reversion_rsi2' ? [['rsi_period', 'RSI 거래일'], ['entry_rsi', '진입 RSI 미만'], ['exit_rsi', '청산 RSI 초과'], ['max_hold_sessions', '최대 유효 보유일']] : [['momentum_lookback', '모멘텀 과거 거래일'], ['momentum_skip', '최근 제외 거래일'], ['top_n', '선정 슬롯'], ['rebalance_every', '재조정 간격 거래일']]).map(([key, label]) => <label key={key}>{label}<input type="number" min={key === 'momentum_skip' ? 1 : key.includes('rsi') && key !== 'rsi_period' ? 0 : 1} max={6000} value={parameters[key]} disabled={!!busy} onChange={event => setParameters(current => ({ ...current, [key]: key.endsWith('rsi') ? event.target.value : parseInt(event.target.value, 10) }))} /></label>)}</div>
          {strategy === 'price_momentum' && <label className="checkbox-label"><input type="checkbox" checked={parameters.positive_only} onChange={event => setParameters(current => ({ ...current, positive_only: event.target.checked }))} />양수 모멘텀만 선정</label>}
          <label>총 목표 배분 (0~1)<input type="number" min="0" max="1" step="0.01" value={parameters.allocation} onChange={event => setParameters(current => ({ ...current, allocation: event.target.value }))} /></label>
          <label>초기 현금 · {raw[0]?.currency || '선택 통화'}<input type="text" inputMode="decimal" value={cash} onChange={event => setCash(event.target.value)} /></label><div className="field-row"><label>수수료 (bps)<input type="text" inputMode="decimal" value={fee} onChange={event => setFee(event.target.value)} /></label><label>슬리피지 (bps)<input type="text" inputMode="decimal" value={slippage} onChange={event => setSlippage(event.target.value)} /></label></div>
          <p className="muted">1 bps = 0.01%. 세금은 모델에 포함되지 않으며 0으로 계산됩니다. 기업행사·배당·실제 유동성은 검증되지 않았습니다.</p><button className="primary wide" type="button" disabled={!dataset || !!busy} onClick={() => action('실제 전략을 실행하는 중', run)}>백테스트 실행 <span>↗</span></button>
        </Panel>
        <Panel title="저장 결과 열기" eyebrow="REPRODUCIBLE"><label>결과 ID<input value={savedId} onChange={event => setSavedId(event.target.value.trim())} placeholder="64자리 결과 식별자" /></label><button className="secondary wide" type="button" disabled={!!busy || !savedId} onClick={() => action('저장 결과를 불러오는 중', () => restore(savedId))}>저장 결과 불러오기</button>{result && <button className="secondary wide" type="button" disabled={!!busy} onClick={() => action('같은 입력으로 재실행하는 중', async () => { const value = validateActualResult(await request(`/api/results/${result.result_id}/replay`, { method: 'POST' })); if (value.result_id !== result.result_id) throw new Error('재실행 결과 식별자가 원래 결과와 다릅니다.'); acceptResult(value); setNotice('같은 입력과 코드로 재실행한 결과가 저장 결과와 일치합니다.'); })}>같은 입력으로 재실행</button>}</Panel>
      </aside><div className="analysis">
        <nav className="view-tabs" aria-label="연구 보기">{[['price', '가격과 근거'], ['performance', '성과와 종목 선택'], ['screen', '부분집합 순위']].map(([value, label]) => <button key={value} type="button" aria-pressed={tab === value} className={tab === value ? 'active' : ''} onClick={() => setTab(value)}>{label}</button>)}<span className="view-meta">{dataset ? `${dataset.bars.length}개 관측 · ${sessions.length}개 선언 거래일` : '자료 선택 대기'}</span></nav>
        {dataset && <div className="snapshot-strip"><span className="snapshot-dot" /><div><strong>실제 원본으로 만든 고정 입력</strong><small>{shortId(dataset.dataset_id)} · 수집 스냅샷 {dataset.source_snapshot_at}</small></div><span className="badge">{reasonLabel(dataset.calendar_grade)}</span></div>}
        {tab === 'price' && <>
          <Panel title={selectedSeries ? `${names[selectedSeries.security_id] || selectedSeries.name || shortId(selectedSeries.security_id)} · 가격` : '가격과 지표'} eyebrow="PRICE & SIGNAL" extra={dataset && <label className="inline-label">가격 자료<select value={activeSeries} onChange={event => setActiveSeries(event.target.value)}>{selections.map(item => <option key={item.series_key} value={item.series_key}>{names[item.security_id] || item.name || shortId(item.security_id)} · {item.source} · {basisLabel(item.basis)}</option>)}</select></label>}>
            {selectedSeries && <div className="series-meta"><span className="badge">{basisLabel(selectedSeries.basis)}</span><span>{selectedSeries.source} · {selectedSeries.currency}</span><span>{selectedSeries.feed} / {selectedSeries.venue} / {selectedSeries.trade_scope}</span><span>조정 {selectedSeries.adjustments?.join(', ') || '없음'}</span></div>}
            <ResearchChart kind="price" bars={bars} sessions={sessions} features={features} selectedSession={session} onSessionChange={setSession} />
            {dataset && <div className="chart-controls"><label>확인 날짜<select value={session} onChange={event => setSession(event.target.value)}>{sessions.map(day => <option key={day}>{day}</option>)}</select></label><label>SMA 거래일<input type="number" min="1" max="1000" value={sma} onChange={event => setSma(parseInt(event.target.value, 10) || 20)} /></label><span className="muted">캔들·지표의 빈 날짜를 채우거나 압축하지 않습니다.</span></div>}
            {featureError && <p role="alert" className="error-box">{featureError}</p>}{featureRow && <ExactTable caption="선택일 지표 정확 수치" columns={['price', 'sma', 'rsi', 'simple_return', 'volatility', 'momentum'].map(key => [key, { price: '종가', sma: 'SMA', rsi: 'RSI (2)', simple_return: '일별 수익률 (비율)', volatility: '연환산 변동성 (비율)', momentum: '모멘텀 (252/21)' }[key], value => <><span className="numeric">{exact(value)}</span><small>{reasonLabel(featureRow.statuses?.[key]?.reason || featureRow.statuses?.[key]?.status)}</small></>])} rows={[featureRow]} />}
            {bars.length > 0 && <details className="table-details"><summary>가격 원본의 정확한 수치와 사용 가능 시점</summary><ExactTable caption="가격 관측 원본" columns={['session', 'open', 'high', 'low', 'close', 'volume', 'notional', 'available_at', 'captured_at', 'observation_id'].map(key => [key, { session: '날짜', open: '시가', high: '고가', low: '저가', close: '종가', volume: '거래량', notional: '거래대금', available_at: '사용 가능 시점', captured_at: '수집 시점', observation_id: '관측 ID' }[key]])} rows={bars} /></details>}
          </Panel>
          {dataset && <Panel title="출처와 품질" eyebrow="EVIDENCE"><Evidence items={dataset.quality_gaps} /><div className="detail-grid"><div><p>통화 {selectedSeries?.currency} · 수량 단위 {selectedSeries?.quantity_unit} · 거래량 기준 {selectedSeries?.volume_basis}</p><p>마감 {dataset.cutoff}</p><p>달력 {reasonLabel(dataset.calendar_grade)}</p><JsonDetails title="고정 입력·원래 대상의 식별자" value={{ dataset_id: dataset.dataset_id, content_hash: dataset.content_hash, manifest_id: dataset.manifest_id, manifest_hash: dataset.manifest_hash, selected_security_ids: dataset.selected_security_ids, calendar_evidence_refs: dataset.calendar_evidence_refs || [] }} /></div><div><h3>공시 시점 근거</h3><p className="muted">최근 공시의 메타데이터입니다. 재무 비율이나 수정 이력을 대체하지 않습니다.</p>{publicationError && <p role="alert" className="error-box">{publicationError}</p>}{publications && <ExactTable caption="공시 공개 및 사용 가능 날짜" columns={[['source', '출처'], ['public_day', '공개일'], ['effective_available_at', '사용 가능 시점'], ['status', '상태'], ['reason', '근거'], ['source_version_ref', '출처 버전']]} rows={publications} />}{publications?.length === 0 && <p className="muted">이 종목의 사용 가능한 공시 시점 근거가 없습니다.</p>}</div></div></Panel>}
        </>}
        {tab === 'performance' && (!result ? <Panel title="성과와 종목 선택"><Empty title="같은 자료, 재현할 수 있는 연구">거래 원본과 지표 출처를 선택한 뒤 백테스트를 실행하세요. 실행 결과는 식별자와 함께 저장됩니다.</Empty></Panel> : <>
          <div className="metric-grid">{[['total_return', '누적 수익률'], ['cagr', '연평균 성장률'], ['max_drawdown', '최대 낙폭'], ['sharpe', '샤프 비율']].map(([key, label]) => <article className="metric" key={key}><span>{label}</span><strong className="numeric">{exact(result.metrics[key]?.value)}</strong><small>{result.metrics[key]?.reason ? reasonLabel(result.metrics[key].reason) : key === 'sharpe' ? '무위험 이자율 0 · 거래일 252' : '서버가 계산한 원수치 · 비율'}</small></article>)}</div>
          <Panel title="자산 가치와 낙폭" eyebrow="PERFORMANCE" extra={<span className="badge">{strategyNames[result.run_config.strategy]} · {result.execution_profile.currency}</span>}><ResearchChart kind="nav" rows={result.rows} selectedSession={session} onSessionChange={setSession} /><ResearchChart kind="drawdown" rows={result.rows} selectedSession={session} onSessionChange={setSession} />{navRow && <ExactTable caption="선택일 포트폴리오" columns={[[ 'session', '날짜'], ['nav', '자산 가치'], ['cash', '현금'], ['drawdown', '낙폭 (비율)'], ['exposure', '투자 비중 (비율)']]} rows={[navRow]} />}</Panel>
          <TraceExplorer result={result} session={result.rows.some(row => row.session === session) ? session : result.rows.at(-1).session} onSessionChange={setSession} names={names} />
          <AnalysisReport key={result.result_id} result={result} selections={selections} names={names} session={session} onSessionChange={setSession} />
          <Panel title="실제 체결 기록" eyebrow="EXECUTION LEDGER" extra={<span className="badge">{result.ledger.length}건</span>}><ExactTable caption="전체 실제 엔진 체결 기록" columns={[['session', '체결일'], ['signal_session', '신호일'], ['security_id', '종목', value => names[value] || value], ['quantity', '수량'], ['raw_open', '원본 시가'], ['price', '체결 가격'], ['fee', '수수료'], ['slippage_cost', '슬리피지 비용'], ['tax', '모델 세금']]} rows={result.ledger} /><JsonDetails title="전체 체결 보류·거절 기록" value={result.execution_events} /></Panel>
          <Panel title="결과의 범위와 재현 근거" eyebrow="RESEARCH ASSUMPTIONS"><p className="note">선택한 자료로 계산한 표본 내 가격 연구입니다. 종가에 판단하고 다음 유효 시가에 정수 수량으로 거래합니다. 양수 일별 거래량은 사후 체결 가정이며, 실제 시가 유동성이나 공식 거래 가능성을 보증하지 않습니다. 현금 배당, 기업행사, 세금과 현금 이자는 반영되지 않습니다.</p><Evidence items={result.quality_gaps} /><p className="hash">저장 결과 ID <code>{result.result_id}</code></p><button className="secondary" type="button" onClick={() => { navigator.clipboard?.writeText(result.result_id).then(() => setNotice('결과 ID를 복사했습니다.')).catch(() => setNotice('아래 결과 ID를 직접 복사할 수 있습니다.')); }}>결과 ID 복사</button><ExactTable caption="전체 성과 수치" columns={[['metric', '지표'], ['value', '원수치'], ['reason', '계산 불가 근거']]} rows={Object.entries(result.metrics).filter(([, value]) => value && typeof value === 'object' && 'value' in value).map(([metric, value]) => ({ metric, ...value }))} /><JsonDetails title="비용 합계와 실행 가정" value={{ costs: result.metrics.costs, execution_profile: result.execution_profile, selection_trace_policy: result.selection_trace_policy }} /><JsonDetails title="입력·코드·엔진의 재현 식별자" value={{ fingerprint: result.fingerprint, run_config: result.run_config, source_snapshot_at: result.source_snapshot_at, cutoff: result.cutoff, manifest_hash: result.manifest_hash }} /></Panel>
        </>)}
        {tab === 'screen' && <Panel title="선택 종목의 순위" eyebrow="SUBSET SCREEN"><p className="note">고정된 입력의 마지막 선언 거래일에서 선택한 종목만 비교합니다. 높은 점수부터 정렬하고 동점은 종목 식별자 순서로 정합니다. 전체 17,758개 종목을 재구성한 과거 시장 순위가 아닙니다.</p><div className="screen-form"><label>가격 기준<select value={screenBasis} onChange={event => setScreenBasis(event.target.value)}><option value="as_traded">거래 원본</option><option value="provider_adjusted">공급자 조정</option></select></label><label>비교 지표<select value={screenFeature} onChange={event => setScreenFeature(event.target.value)}>{[['momentum', '모멘텀 (252/21)'], ['sma', 'SMA (20)'], ['rsi', 'RSI (2)'], ['volatility', '변동성'], ['simple_return', '일별 수익률']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>상위 개수<input type="number" min="1" max="100" value={screenTop} onChange={event => setScreenTop(parseInt(event.target.value, 10) || 1)} /></label><button className="primary" type="button" disabled={!dataset || !!busy} onClick={() => action('부분집합 순위를 계산하는 중', async () => { setScreen(null); setScreen(await request('/api/screen', { method: 'POST', body: { dataset_id: dataset.dataset_id, basis: screenBasis, feature: screenFeature, top_n: screenTop } })); })}>순위 계산</button></div>{screen ? <><div className="screen-counts">선택 {screen.selected_count} / 원래 대상 {screen.original_target_count} · 적격 {screen.eligible_count} · 제외 {screen.excluded_count}</div><ExactTable caption="부분집합 상위 순위" columns={[['rank', '순위'], ['security_id', '종목', value => names[value] || value], ['session', '기준일'], ['score', '정확 점수'], ['basis', '가격 기준', basisLabel], ['adjustments', '조정 내용', value => value?.join(', ') || '없음']]} rows={screen.rows} /><h3>명시적 제외</h3><ExactTable caption="순위 제외 종목과 이유" columns={[['security_id', '종목', value => names[value] || value], ['reason', '제외 이유', reasonLabel], ['series_key', '자료 식별자']]} rows={screen.excluded} /><Evidence items={screen.quality_gaps} /><JsonDetails title="순위 정의와 안정적 동점 정책" value={screen.definitions} /></> : <Empty title="조건을 정하고 비교하세요">선택 자료를 로드하면 동일 기준의 점수와 제외 사유를 확인할 수 있습니다.</Empty>}</Panel>}
      </div></div><footer className="workspace-footer">Backtest Research · 실제 원본 / 선택한 부분집합 / 표본 내 연구<span>최초 대상 2016.01.01 — 2026.10.07</span></footer>
    </main>
  </>;
}
