import React, { useState } from 'react';
import { exact, filterTrace, reasonLabel, shortId } from '../model.js';
import { ExactTable, JsonDetails, Panel } from './Shared.jsx';

const stages = [['all', '전체 후보'], ['candidate', '선정 후보'], ['target', '목표 선택'], ['pending', '대기 주문'], ['filled', '실제 체결'], ['held', '실제 보유']];
export function TraceExplorer({ result, session, onSessionChange, names = {} }) {
  const [stage, setStage] = useState('all');
  const sessions = result.rows.map(row => row.session);
  const dateRows = filterTrace(result.selection_trace, session);
  const rows = filterTrace(result.selection_trace, session, stage);
  const index = sessions.indexOf(session);
  return <Panel title="날짜별 종목 선택" eyebrow="DECISION → EXECUTION → HOLDINGS" extra={<span className="badge">종가 판단 · 다음 유효 시가 체결</span>}>
    <div className="trace-toolbar"><div className="date-step"><button type="button" aria-label="이전 거래일" disabled={index <= 0} onClick={() => onSessionChange(sessions[index - 1])}>←</button><label>판단일<select value={session} onChange={event => onSessionChange(event.target.value)}>{sessions.map(day => <option key={day}>{day}</option>)}</select></label><button type="button" aria-label="다음 거래일" disabled={index < 0 || index >= sessions.length - 1} onClick={() => onSessionChange(sessions[index + 1])}>→</button></div><span className="muted">차트의 날짜를 클릭해도 연결됩니다.</span></div>
    <div className="stage-tabs" aria-label="선택 단계">{stages.map(([value, label]) => <button key={value} type="button" className={stage === value ? 'active' : ''} aria-pressed={stage === value} onClick={() => setStage(value)}>{label}<span>{filterTrace(result.selection_trace, session, value).length}</span></button>)}</div>
    <p className="note">후보 선정, 목표 비중, 대기 주문, 당일 체결, 종가 보유는 서로 다른 상태입니다. 후보가 바뀌어도 재조정일 전에는 이전 목표가 유지될 수 있습니다.</p>
    <div className="table-wrap trace-table"><table><caption className="sr-only">{session}의 종목별 후보 및 실제 거래 상태</caption><thead><tr>{['종목', '점수 / 순위', '적격 / 제외', '후보 / 목표', '대기 주문', '당일 실제 체결', '종가 실제 보유', '판단 근거'].map(label => <th key={label} scope="col">{label}</th>)}</tr></thead><tbody>{rows.map(row => <React.Fragment key={`${row.security_id}-${row.raw_series_key}`}><tr>
      <th scope="row"><strong>{names[row.security_id] || row.security_id}</strong><small>{result.series_identities?.[row.raw_series_key]?.currency} · {shortId(row.raw_series_key)}</small></th>
      <td><span className="numeric">{exact(row.score)}</span><small>순위 {exact(row.rank)}</small></td>
      <td><span className={`pill ${row.eligible ? 'positive' : 'neutral'}`}>{row.eligible ? '적격' : '제외'}</span><small>{row.exclusion_reason ? reasonLabel(row.exclusion_reason) : '—'}</small></td>
      <td>후보 {exact(row.candidate_selected)}<small>목표 선택 {exact(row.selected)}</small><span className="numeric">{exact(row.target_weight)}</span><small>목표 비중 (비율)</small></td>
      <td>{row.pending_target ? <><span>{reasonLabel(row.pending_target.status)}</span><small>신호일 {row.pending_target.signal_session}</small><span className="numeric">{exact(row.pending_target.target_weight)}</span></> : '없음'}</td>
      <td>{row.actual_fills?.length ? row.actual_fills.map((fill, i) => <div key={i} className="fill-line">{fill.quantity > 0 ? '매수' : '매도'} <span className="numeric">{exact(fill.quantity)} @ {exact(fill.price)}</span></div>) : '체결 없음'}</td>
      <td><span className="numeric">{exact(row.actual_holding_quantity)}</span><small>수량 · {exact(row.actual_held_sessions)} 유효 보유일</small><span className="numeric">{exact(row.actual_holding_weight)}</span><small>실제 비중 (비율)</small></td>
      <td>{row.decision_reasons?.map((reason, i) => <small key={i} title={reason}>{reasonLabel(reason)}</small>)}{row.rebalance_due !== null && row.rebalance_due !== undefined && <small>재조정일 {exact(row.rebalance_due)}</small>}</td>
    </tr><tr className="trace-detail"><td colSpan="8"><details><summary>판단 근거와 원본 입력 보기</summary><div className="detail-grid"><div><h4>사용한 지표와 상태</h4>{Object.entries(row.feature_values || {}).map(([key, value]) => <p key={key}><strong>{key}</strong> <span className="numeric">{exact(value)}</span> <small>{reasonLabel(row.feature_statuses?.[key]?.reason || row.feature_statuses?.[key]?.status)}</small></p>)}<JsonDetails title="거래 원본과 지표 자료 식별자" value={{ raw_series_key: row.raw_series_key, feature_series_key: row.feature_series_key }} /><JsonDetails title="체결 보류·거절 기록" value={row.execution_events || []} /></div><div><h4>경계와 현재 관측</h4><p className="muted">아래 요약 참조는 전체 계산 구간의 원본 목록을 대신하지 않습니다.</p><ExactTable caption="경계 관측 참조" columns={[['session', '날짜'], ['observation_id', '관측 ID'], ['input_hash', '입력 해시']]} rows={row.input_observation_refs || []} />
      {Object.entries(row.feature_input_windows || {}).map(([feature, window]) => <details key={feature}><summary>{feature} · 전체 계산 구간 {window.start_session} ~ {window.end_session}</summary><p>관측 {exact(window.observation_count)} / 선언 거래일 {exact(window.declared_session_count)} · 초기 필요 거래일 {exact(window.required_initial_sessions)}</p><p className="hash">구간 참조 해시 {window.input_refs_hash}</p><ExactTable caption={`${feature} 전체 입력 관측`} columns={[['session', '날짜'], ['observation_id', '관측 ID'], ['available_at', '사용 가능 시점'], ['captured_at', '수집 시점'], ['input_hash', '입력 해시'], ['raw_id', '원본 ID'], ['decision_id', '채택 판단 ID']]} rows={(result.input_observation_catalog || []).filter(ref => ref.series_key === window.series_key && ref.session >= window.start_session && ref.session <= window.end_session)} /></details>)}
    </div></div></details></td></tr></React.Fragment>)}</tbody></table></div>
    {!rows.length && <p className="table-empty">이 단계에 해당하는 종목이 없습니다.</p>}
    <p className="footnote">당일 전체 후보 {dateRows.length}개 · 원래 대상 {result.original_target_count || 17758}개 중 선택한 자료 범위의 연구입니다.</p>
  </Panel>;
}
