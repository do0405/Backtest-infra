import React, { useEffect, useState } from 'react';
import { request } from '../api.js';
import { basisLabel, exact, reasonLabel } from '../model.js';
import { ResearchChart } from './Charts.jsx';
import { ExactTable, JsonDetails, Panel } from './Shared.jsx';

export function AnalysisReport({ result, selections = [], names = {}, session, onSessionChange }) {
  const [benchmark, setBenchmark] = useState('');
  const [report, setReport] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => { setBenchmark(''); setReport(null); setError(''); }, [result.result_id]);
  useEffect(() => {
    const controller = new AbortController();
    setReport(null); setError(''); setLoading(true);
    const query = benchmark ? `?${new URLSearchParams({ benchmark_series_key: benchmark })}` : '';
    request(`/api/results/${result.result_id}/analysis${query}`, { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      if (value.result_id !== result.result_id || value.dataset_id !== result.dataset_id || !value.trades?.summary || !value.analysis_id) throw new Error('분석 보고서의 저장 결과·입력 식별자가 일치하지 않습니다.');
      setReport(value);
    }).catch(err => { if (!controller.signal.aborted) setError(err.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [result.result_id, result.dataset_id, benchmark]);
  const choices = selections.filter(item => item.market === result.execution_profile.market && item.currency === result.execution_profile.currency);
  const summary = report?.trades.summary;
  const download = kind => `/api/results/${result.result_id}/export?${new URLSearchParams({ format: kind, ...(kind === 'analysis' && benchmark ? { benchmark_series_key: benchmark } : {}) })}`;
  return <>
    <Panel title="가격 기준 비교와 청산 손익" eyebrow="POST-RUN ANALYSIS">
      <p className="note">원래 저장 결과를 바꾸지 않고 같은 고정 입력에서 분석합니다. 분할 청산은 각각의 매입 물량 부분으로 집계하며, 남아 있는 물량은 승률에 포함하지 않습니다. 체결 가격에 포함된 슬리피지는 다시 차감하지 않습니다.</p>
      <div className="report-controls"><label>가격 비교 기준<select value={benchmark} onChange={event => setBenchmark(event.target.value)}><option value="">직접 선택 · 비교 기준 없음</option>{choices.map(item => <option key={item.series_key} value={item.series_key}>{names[item.security_id] || item.security_id} · {item.source} · {basisLabel(item.basis)} · {item.adjustments?.join(', ') || '조정 없음'}</option>)}</select></label></div>
      {loading && <p className="note" role="status">저장된 결과의 분석 보고서를 계산하는 중입니다.</p>}
      {error && <p role="alert" className="error-box">{error} 원래 결과와 체결 기록은 계속 열람할 수 있습니다.</p>}
      {summary && <><ExactTable caption="청산 부분 기준 손익 통계" columns={[[ 'closed_portion_count', '청산 부분 수'], ['open_quantity', '남은 수량'], ['realized_net_pnl', `실현 순손익 (${result.execution_profile.currency})`], ['win_rate', '청산 부분 승률', value => <>{exact(value?.value)}<small>{reasonLabel(value?.reason)}</small></>], ['profit_factor', '실현 이익 / 손실', value => <>{exact(value?.value)}<small>{reasonLabel(value?.reason)}</small></>]]} rows={[summary]} />
        {report.risk && <><ExactTable caption="추가 위험·노출 지표" columns={[[ 'metric', '위험·노출 지표'], ['value', '원수치'], ['reason', '계산 제한', reasonLabel]]} rows={Object.entries(report.risk).filter(([, value]) => value && typeof value === 'object' && 'value' in value).map(([metric, value]) => ({ metric: { sortino: '소르티노 비율 · 목표 0', calmar: '칼마 비율', max_drawdown_duration_calendar_days: '최장 낙폭 기간 · 달력 일수', mean_exposure: '평균 투자 비중', invested_session_fraction: '투자한 선언 거래일 비율' }[metric] || metric, ...value }))} /><JsonDetails title="추가 위험 지표의 정의" value={report.risk.definitions} /></>}
        {report.benchmark.status !== 'not_selected' && <><p className="note">선택한 종가를 첫 날짜 전략 자산 가치에 맞춘 비용 0의 가격 비교입니다. 배당 총수익이나 실제 매수·보유 체결이 아닙니다. 전략의 수수료·슬리피지는 전략 자산 가치에 그대로 남아 있습니다. 원주가·공급자 조정의 정의를 확인하세요.</p><ResearchChart kind="nav" rows={result.rows} benchmarkRows={report.benchmark.rows} selectedSession={session} onSessionChange={onSessionChange} /><ExactTable caption="가격 기준 대비 수익률 차이" columns={[[ 'value', '전략 수익률 − 가격 기준 수익률 (비율)'], ['reason', '계산 제한', reasonLabel]]} rows={[report.comparison.total_return_difference]} />{report.benchmark.normalization_reason === 'benchmark_initial_anchor_missing' && <p className="note">첫 비교 기준일 가격이 없어 전체 가격 경로를 같은 시작값으로 환산할 수 없습니다. 이후 날짜의 실제 가격 누락을 뜻하지 않습니다. 첫 날짜를 이후 날짜로 임의 변경하지 않습니다.</p>}{report.benchmark.missing_sessions?.length > 0 && <p className="note">가격 기준 자료 누락: {report.benchmark.missing_sessions.join(', ')}. 실제 관측이 없는 날짜만 표시합니다. 누락 날짜를 채우거나 삭제하지 않으며 전체 상대 성과는 계산하지 않습니다.</p>}<JsonDetails title="선택한 비교 출처와 정의" value={{ identity: report.benchmark.series_identity, policy: report.benchmark.policy }} /></>}
        <details className="table-details"><summary>청산 부분별 종목·매입·매도·비용·손익</summary><ExactTable caption="FIFO 청산 부분 원장" columns={[[ 'security_id', '종목', value => names[value] || value], ['quantity', '청산 수량'], ['entry_session', '매입일'], ['exit_session', '매도일'], ['entry_price', '매입 가격'], ['exit_price', '매도 가격'], ['entry_fee', '매입 수수료'], ['exit_fee', '매도 수수료'], ['gross_pnl', '체결 가격 차익'], ['net_pnl', '비용 후 순손익'], ['net_return', '비용 후 수익률'], ['holding_calendar_days', '경과 일수']]} rows={report.trades.closed_portions} /></details>
        {report.trades.open_lots.length > 0 && <details className="table-details"><summary>미청산 매입 물량과 남은 원가</summary><ExactTable caption="미청산 FIFO 매입 물량" columns={[[ 'security_id', '종목', value => names[value] || value], ['entry_session', '매입일'], ['quantity', '남은 수량'], ['entry_price', '매입 가격'], ['remaining_entry_cost', '남은 매입 비용'], ['cost_basis', '비용 포함 원가']]} rows={report.trades.open_lots} /></details>}
      </>}
    </Panel>
    <Panel title="연구 결과 내려받기" eyebrow="REUSABLE RESULTS"><p className="note">고정 입력 JSON과 결과 JSON을 함께 보관하면 원래 데이터·코드 식별자를 확인할 수 있습니다. CSV의 복합 근거는 JSON 문자열로 기록되고 수식으로 해석될 수 있는 텍스트는 보호됩니다.</p><div className="download-links">{[['result', '결과 JSON'], ['dataset', '고정 입력 JSON'], ['analysis', '분석 보고서 JSON'], ['decisions', '판단·선정 CSV'], ['ledger', '체결 CSV'], ['closed_portions', '청산 손익 CSV']].map(([kind, label]) => <a key={kind} className="secondary" href={download(kind)} download>{label}</a>)}</div>{report && <p className="hash">분석 식별자 <code>{report.analysis_id}</code></p>}</Panel>
  </>;
}
