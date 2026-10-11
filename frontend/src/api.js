const errors = {
  bounded_sql_unavailable: '데이터베이스 응답을 받을 수 없습니다. 잠시 후 다시 시도하세요.',
  input_or_evidence_unavailable: '선택한 범위의 입력 또는 근거를 사용할 수 없습니다. 자료와 기간을 확인하세요.',
  saved_input_or_result_not_found: '저장된 자료 또는 결과를 찾을 수 없습니다.', qualified_engine_unavailable: '검증된 실행 엔진을 사용할 수 없습니다.',
  local_same_origin_required: '이 연구 공간은 로컬 주소에서 열어야 합니다.', research_run_in_progress: '다른 연구 실행이 진행 중입니다.',
};
export async function request(path, { method = 'GET', body, signal } = {}) {
  if (!path.startsWith('/api/')) throw new Error('로컬 연구 API 주소가 필요합니다.');
  let response;
  try {
    response = await fetch(path, { method, signal, credentials: 'omit', ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('로컬 연구 서버에 연결할 수 없습니다. 서버 상태를 확인하세요.');
  }
  let data;
  try { data = await response.json(); } catch { throw new Error('서버 응답을 읽을 수 없습니다. 실제 자료가 로드되지 않았습니다.'); }
  if (!response.ok) {
    const code = data.error || (typeof data.detail === 'string' ? data.detail : '');
    throw new Error(`${errors[code] || '요청을 처리하지 못했습니다. 입력값과 서버 상태를 확인하세요.'} (${response.status}${code ? ` · ${code}` : ''})`);
  }
  return data;
}
export async function loadActualDataset(path, options) {
  const data = await request(path, options);
  if (data.origin !== 'actual_source' || data.original_target_count !== 17758) throw new Error('실제 원본 대상에서 만든 자료만 연구 화면에 사용할 수 있습니다.');
  if (!data.dataset_id || !data.bars?.length || !data.sessions?.length) throw new Error('사용할 수 있는 가격 자료가 없습니다.');
  return data;
}
export function validateActualResult(data) {
  if (data.origin !== 'actual_source' || !data.result_id || !data.rows?.length || !Array.isArray(data.selection_trace) || data.engine?.actual_framework_execution !== true) throw new Error('실제 실행이 확인된 연구 결과를 사용할 수 없습니다.');
  return data;
}
