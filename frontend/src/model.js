export const selectionFields = ['security_id', 'source_contract_id', 'basis', 'feed', 'venue', 'trade_scope', 'currency', 'quantity_unit', 'series_key'];
export const exact = value => value === null || value === undefined ? '—' : typeof value === 'boolean' ? value ? '예' : '아니요' : String(value);
export const basisLabel = basis => basis === 'as_traded' ? '거래 원본' : basis === 'provider_adjusted' ? '공급자 조정' : exact(basis);
export const shortId = value => value ? `${value.slice(0, 10)}…${value.slice(-6)}` : '—';
export const selectionPayload = series => Object.fromEntries(selectionFields.map(field => [field, series[field]]));
export const matchSeries = (a, b) => selectionFields.filter(field => field !== 'series_key').every(field => a[field] === b[field]);
export function featureMapping(selections, mapping) {
  const raw = selections.filter(series => series.basis === 'as_traded');
  if (!raw.length || new Set(raw.map(series => series.security_id)).size !== raw.length) throw new Error('종목별 거래 원본을 하나씩 선택하세요.');
  const features = raw.map(series => {
    const feature = selections.find(candidate => candidate.series_key === mapping[series.series_key] && candidate.security_id === series.security_id);
    if (!feature) throw new Error('각 거래 원본에 사용할 지표 자료를 직접 선택하세요.');
    return feature.series_key;
  });
  return { raw_series_keys: raw.map(series => series.series_key), feature_series_keys: features };
}
export function filterTrace(rows, session, stage = 'all') {
  const filters = { all: () => true, candidate: row => row.candidate_selected === true, target: row => row.selected === true,
    pending: row => Boolean(row.pending_target), filled: row => row.actual_fills?.length > 0, held: row => row.actual_holding_quantity > 0 };
  return rows.filter(row => row.session === session && (filters[stage] || filters.all)(row));
}
function chartNumber(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}
const dateValid = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(value));
export function chartCandles(bars, sessions) {
  const lookup = new Map(bars.map(bar => [bar.session, bar]));
  return [...new Set(sessions)].filter(dateValid).sort().map(time => {
    const bar = lookup.get(time);
    if (!bar) return { time };
    const [open, high, low, close] = ['open', 'high', 'low', 'close'].map(field => chartNumber(bar[field]));
    if ([open, high, low, close].some(value => value === null || value <= 0) || low > Math.min(open, close) || high < Math.max(open, close) || low > high) return { time };
    return { time, open, high, low, close };
  });
}
export function chartLine(rows, field) {
  const unique = new Map(rows.filter(row => dateValid(row.session)).map(row => [row.session, row]));
  return [...unique].sort(([a], [b]) => a.localeCompare(b)).map(([time, row]) => {
    const value = chartNumber(row[field]);
    return value === null ? { time } : { time, value };
  });
}
const reasons = {
  available: '사용 가능', unavailable: '사용 불가',
  warmup: '계산에 필요한 과거 자료 부족', missing_session: '계산 구간에 거래일 자료 누락', feature_unavailable: '지표를 계산할 수 없음',
  nonpositive_momentum: '모멘텀이 양수가 아님', outside_top_n: '선정 순위 밖', momentum_selected: '모멘텀 상위 종목 선정',
  periodic_hold_target: '다음 재조정일까지 기존 목표 유지', trend_long: '단기 평균이 장기 평균보다 높음', trend_cash: '추세 조건 미충족',
  enter: '진입 조건 충족', exit: '청산 조건 충족', hold: '보유 목표 유지', cash: '현금 목표 유지',
  queued_next_open: '다음 유효 시가 대기', pending_unfilled: '미체결 목표 유지',
  subset_selection_bias: '선택한 종목에 한정된 결과', historical_universe_unverified: '과거 전체 시장 구성 미검증',
  market_calendar_unverified: '공식 거래일 달력 미검증', provisional_weekdays: '임시 평일 달력 · 공휴일 미확정',
  provider_declared_alpaca_calendar: '공급자가 선언한 거래일 달력', observed_session_union: '관측된 날짜의 합집합',
  missing_selected_series_sessions: '선택 자료에 거래일 누락', corporate_action_coverage_unknown: '기업행사 자료 범위 미확인',
  price_only_no_cash_dividends: '현금 배당을 포함하지 않는 가격 연구', unknown_official_tradability: '공식 거래 가능 여부 미확인',
  ex_post_daily_volume_execution_assumption: '일별 양수 거래량을 가정한 체결', stale_mark_carrying: '누락된 종가는 마지막 원본 종가로 평가',
  requested_basis_unavailable: '요청한 가격 기준 자료 없음', ambiguous_series_selection: '동일 기준의 자료가 여러 개',
  historical_adjustment_reference_unverified: '과거 조정 기준 시점 미검증', latest_source_revision_unavailable: '최신 자료 개정 사용 불가',
};
export const reasonLabel = value => reasons[value] || exact(value);
