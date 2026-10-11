# 로컬 웹 화면과 REST API 명세

## 공통 화면 구조

좌측에는 홈, 데이터, 차트, 백테스트, 종목 스크리너, 전략 스크리너, 시그널, 보유·기록, 설정을 둔다. 상단에는 종목 검색·시장·release·세션·연결 상태가 있다. 각 결과에 연구 모드·가격 basis·통화·quality를 표시하고 생성/사용 가능 시각을 구분한다.

```text
┌ 메뉴 ─┬ 시장·종목 검색 ─ release / 기준 세션 / 동기화 상태 ┐
│ 홈    │ 핵심 상태: KR·US freshness / 레짐 / blocked / 예산 │
│ 데이터│ 대상  coverage  gaps  출처  변경분  작업             │
│ 차트  │ 종목·상품·거래상태 / raw·split·total-return 선택     │
│ 실험  │ 캔들·지표·volume + 공시·기업행동·시그널 marker        │
│ 순위  │ 재무 / 가격 / 원계정 / lineage / 검증 / 실험 탭       │
│ 신호  │ 데이터부족·부분연구·만료 사유를 결과 가까이에 표시    │
└───────┴───────────────────────────────────────────────────┘
```

UI에서 source raw·capture-only·PIT 같은 technical terms는 설명 tooltip과 함께 필요한 위치에만 노출한다. 일반 사용 흐름은 “어느 시점의 자료인지”, “사용할 수 없는 이유”, “무엇이 바뀌었는지” 중심으로 표현한다.

## 차트 계약

bar는 시장 session date를 daily time key로 쓰고 원 가격 소수 정밀도를 유지한다. historical_price_session_v1의 cutoff를 표시하며 수신 날짜는 provenance에 남긴다. source-defined 조정 계열 탐색과 검증 조정/원주가를 분리한다. client가 중복 bar를 임의 병합하거나 조정계수를 만들지 않는다.

crosshair·pan/zoom·기간 선택·volume pane·SMA/RSI·비교 선·event markers를 초기 제공한다. gap은 별도 데이터 상태로 표시한다. UI 합성 가격을 서버 연구 입력으로 되돌려 쓰지 않는다. viewport 변경은 bounded range query와 cursor로 추가 읽고 주간·월간 봉은 검증 달력·OHLC 집계로 생성한다.

Lightweight Charts version은 구현 spike에서 lock한다. chart adapter가 버전별 series/marker/pane API를 감싸고 React lifecycle에서 생성·resize·cleanup을 관리한다. NOTICE/링크는 앱 정보와 차트 주변에 노출한다. 외부 실시간 widget 데이터는 사용하지 않는다.

## 공통 API 규칙

base path는 `/api/v1`, JSON UTF-8이다. canonical decimal은 문자열, feature/plot float는 numeric+numeric_policy, UTC는 ISO8601, session은 YYYY-MM-DD다. 식별자는 기존 security/issuer UUID를 사용하고 code/name 검색 결과는 resolve 상태를 포함한다.

정렬은 stable keyset cursor, 기본 limit 100·최대 1000, bar 최대 5000/page 후보다. 응답에 request_id·schema_version·release_id·cutoff·quality·reason_codes를 담는다. GET 결과에 암묵적 최신 release를 섞지 않는다. 최신 선택 endpoint는 실제 선택한 release ID를 반환한다.

mutating POST는 세션 토큰·Origin 검증·idempotency key를 사용한다. CORS는 localhost 개발 origin만 allowlist로 두고 배포 build는 같은 origin에서 제공한다. API는 외부 bind·임의 SQL·임의 파일 실행·임의 URL 수집을 허용하지 않는다.

## endpoint 목록

| Method와 경로 | 입력·역할 | 반환 |
|---|---|---|
| GET `/health` | liveness | 상태·앱 version, 비밀값 없음 |
| GET `/system/status` | DB/worker/sync/자원 readiness | 구성요소·paused·last session·budget |
| GET `/releases` | tier·시장·limit | 릴리스 목록·profile·target·hash |
| GET `/coverage` | release_id·시장·상품·기간 | eligible/missing/unsupported/not_applicable 분모 |
| GET `/securities/resolve` | identifier·market·as_of·release_id | resolved/ambiguous/unresolved·후보 |
| GET `/securities` | 상품·시장·historical status·cursor | 전체 대상 검색·현재/과거 필터 |
| GET `/securities/{id}/bars` | release_id·cutoff·price_clock_policy·research_mode·basis·start/end·cursor | bounded OHLCV·품질·source/revision/clock policy·event refs |
| GET `/issuers/{id}/fundamentals` | release_id·cutoff·history_mode·metric | value/period/scope·public clock·lineage |
| GET `/securities/{id}/events` | 기간·event types | filing/action/lifecycle/signal markers |
| GET `/strategies` | applicability·research/selected·validation scope | plugin 정의·version·입력·capability·선택/품질/검증 |
| POST `/backtests` | RunConfig·idempotency key | 202 job_id·run fingerprint |
| GET `/backtests/{id}` | run id | config·status·metrics·qualification·artifact refs |
| GET `/backtests/{id}/ledger` | cursor·security·event type | 페이지별 ledger·cash·fill·cost·input refs |
| GET `/regimes` | market·dataset/release·session | state·scores·candidate/confirmed·basis |
| POST `/screenings` | stock/strategy·dataset·filter config | 202 job_id·batch_id |
| GET `/screenings/{id}/results` | cursor·include_excluded·sort | rank·values·reason·denominator |
| POST `/signals/generate` | research/selected strategy scope·dataset·portfolio snapshot | 202 job_id, 연구/선택 등급·품질을 보존한 참고 계산 |
| GET `/signals` | session·market·state·cursor | signal cards·reason·expiry·supersession |
| POST `/portfolios/{id}/events` | 수동 trade/note/import payload | 새 event ID·validation·snapshot job |
| POST `/strategies/{id}/selection-events` | 사용자 선택·scope·definition hash·reason·state | 새 research/selected/retired event, validation 불변 |
| GET `/jobs/{id}` | job id | progress·checkpoint summary·status |
| POST `/jobs/{id}/pause` | expected state | cooperative pause 요청 |
| POST `/jobs/{id}/resume` | paused fingerprint | 새 lease·재개 상태 |
| POST `/jobs/{id}/cancel` | expected state | cooperative cancel 요청 |
| POST `/sync/jobs` | transport config ref·budget | 202 job_id, 비밀값은 body에 없음 |
| POST `/exports` | 허용 result ID·format CSV/JSON | 202 artifact ref·qualification 유지 |

selection endpoint는 사용자 최종 선택을 기록하고 definition/scope·actor·reason·시각을 고정한다. 데이터나 성과를 검증 완료로 바꾸지 않는다. signals/generate는 각 data policy로 실제 계산 또는 blocked를 반환하며 주문을 전송하지 않는다. 같은 탭/CSV에서 연구 결과를 검증 결과처럼 보이지 않게 selection·validation·data quality·execution assumptions를 함께 표시한다.

## RunConfig 예시

```json
{
  "dataset_id": "<sealed-analysis-dataset-uuid>",
  "strategy_definition_hash": "<sha256>",
  "market": "KR",
  "start": "2016-01-01",
  "end": "2026-10-08",
  "research_mode": "strict_pit",
  "execution": {"fill": "next_session_open", "long_only": true, "integer_shares": true},
  "cost_policy_id": "<versioned-policy-id>",
  "evaluation_plan_id": "<frozen-split-id>",
  "engine": "configured_default",
  "parameter_grid": null,
  "seed": 42
}
```

이는 예정 DTO 예시다. 실제 runtime에서 입력 ID·기간·상품·engine capability·dataset qualification을 검증하고 시작 전에 rejection reasons를 반환한다. 재무 strategy를 ETF에 적용하는 요청은 not_applicable 사유가 된다.

configured_default는 실제 qualification된 engine tag/commit와 policy로 시작 전에 resolve하고 run fingerprint에 기록한다. default가 미선정이면 ENGINE_NOT_QUALIFIED이며 reference/dummy 엔진으로 대신 성공하지 않는다. 전략 plugin/config schema·requested/resolved config·actual engine/source·resolution events·data_origin을 result/export에 보존한다. 실패 시 고정 JSON·빈 정상 결과·랜덤 차트를 반환하지 않는다.

## 오류와 빈 상태

| HTTP | code | 사용자 표시 |
|---|---|---|
| 400 | INVALID_CUTOFF / INVALID_SCOPE | 입력 기간·시각·대상 확인 |
| 404 | UNRESOLVED_SECURITY / RESULT_NOT_FOUND | 종목 후보·필터를 확인 |
| 409 | POLICY_VERSION_MISMATCH / OWNER_CONFLICT / IDEMPOTENCY_CONFLICT | 다른 정책·소유자·내용 충돌 |
| 422 | QUALITY_BLOCKED / INAPPLICABLE_METRIC / UNSUPPORTED_ACTION | 해당 계산의 부족 입력·적용성 |
| 429 | RESOURCE_BUDGET_EXCEEDED | 작업 대기·pause·예산 다음 창 |
| 503 | DB_NOT_READY / WORKER_UNAVAILABLE | 복구 동작과 현재 데이터 조회 가능 여부 |

```json
{"error":{"code":"QUALITY_BLOCKED","message":"검증된 분할 이력이 부족합니다.","reason_codes":["corporate_action_coverage_gap"],"request_id":"example"}}
```

금융 값 unavailable은 정상 200 응답에서 value=null+status+reason으로 표시할 수 있다. 전체 작업이 불가능하면 422를 쓴다. 빈 표·0거래·미산출·네트워크 실패를 같은 UI로 보여주지 않는다.

## 화면 인수

홈→종목 검색→10년 차트→재무 개선 근거→백테스트→OOS 비교→스크리닝→시그널 근거→수동 메모의 전체 흐름을 fixture와 고정 실데이터에서 검증한다. 키보드 검색·pagination·chart cleanup·offline·stale·expired·conflict·unknown이 동작해야 한다. 화면 표시와 CSV 상태가 일치해야 한다.

연구용 거래가능 가정은 카드·거래 원장·run DTO·CSV에 execution_policy, observed_tradability, tradability_assumption, ex_post_execution_assumption과 assumed 거래 수로 노출한다. 사용자가 선택한 전략의 결과에도 같은 품질 표시를 유지한다. unknown 관측을 검증된 거래가능으로 표시하거나 다음 세션 모의체결 결과를 현재 신호 생성의 입력으로 반환하지 않는다.
