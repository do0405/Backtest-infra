# SQL 시점 분석 입력 변경분 데이터 계약

## 기존 계약과 신규 확장

기존 `security`, `issuer`, `identifier_history`, `source_contract`, `source_qualification`, `exchange_session`, `raw_object`, `ingestion_attempt`, `collection_job`, `target_universe_manifest`, `price_observation`, `corporate_action_observation`, `source_financial_fact`, `fundamental_fact`, `derived_fact`, `dataset_release`, `selection_decision`을 재사용한다. 아래 테이블은 신규 제안이며 기존 테이블에 이미 존재한다고 가정하지 않는다.

기존 evidence·fact·selection·sealed release는 append-only다. 운영 queue·lease·head pointer는 mutable로 두되 상태 이벤트를 남긴다. 증권 ID는 string UUID, 시간은 timezone-aware UTC, 경제 세션은 시장 calendar date, 금융 원값은 NUMERIC/Decimal, code는 문자열을 사용한다.

## 신규 테이블

| 테이블 | 주요 필드 | 고유성·참조·상태 |
|---|---|---|
| listing_history | id, security_id, venue, valid_from/to, symbol, product_type, available_at, captured_at, source_ref | versioned assertion+withdrawal, 동기간 충돌 격리 |
| security_lifecycle_event | id, security_id, type, effective_at, public_at, captured_at, terms, source_ref | source event/version hash, listing/delisting/merger/maturity |
| tradability_observation | id, security_id, session, status, scope, public_at, captured_at, evidence | source+security+session+version, official/supplier/unknown |
| product_metadata_fact | id, security_id, metric_id, value/text, effective_at, available_at, captured_at, source_ref | ETF/ETN 적용성·NAV·leverage·maturity, company 재무와 분리 |
| coverage_snapshot | id, release_id, target_hash, scope, denominator, counts, intervals, policy_hash | release+scope+policy, eligible/not_applicable/missing/unsupported |
| analysis_dataset | id, release_id, manifest_hash, scope_hash, mode, price_basis, clocks, quality, cache_refs | dataset fingerprint unique, sealed manifest |
| feature_snapshot | id, dataset_id, definition_hash, evaluation_at, input_refs, values, quality | feature+entity+session+dataset unique |
| strategy_definition | id, name, version, protocol_version, code_hash, config_schema, required_inputs, schedule, state_schema, output_kind, data_policy, engine_capabilities, definition_hash | immutable plugin version/hash, current pointer 별도 |
| research_run | id, fingerprint, dataset_id, strategy_hash, engine_version, eval_plan, cost_policy, status | fingerprint+attempt, 재실행 lineage·trial count |
| research_trial | id, run_id, params_hash, split_id, status, metrics, artifact_refs | run+params+split unique, 실패도 보존 |
| simulation_ledger | id, run_id, seq, session, security_id, event_type, qty, price, cash, fees, input_refs | run+seq unique, trade/cash/action/mark 구분 |
| regime_observation | id, market, session, model_hash, dataset_id, state, scores, input_refs, confirmed_at | model+market+session+dataset unique |
| screening_batch | id, type, scope_hash, cutoff, dataset_id, config_hash, counts, status | 종목/전략·eligible/excluded 집합 |
| screening_result | batch_id, entity_id, rank, values, score, status, reason_codes, input_refs | batch+entity unique, 제외 행도 저장 |
| strategy_selection_event | id, strategy_hash, scope_hash, eval_refs, selection_state, actor, reason, recorded_at | append event, 사용자 research/selected/retired 선택; validation은 별도 평가 증거 |
| portfolio_event | id, portfolio_id, seq, effective_at, recorded_at, type, payload, supersedes_id | 수동 import·trade·adjustment·note, source label |
| portfolio_snapshot | id, portfolio_id, as_of, ledger_hash, positions, cash, currency, stale_policy | 입력 원장 재생으로 생성, actual_user_recorded/simulated |
| signal_event | id, batch_id, strategy_hash, security_id, cutoff, release_id, portfolio_snapshot_id, state, expires_at, input_refs, supersedes_id | generation key unique, append-only state event |
| sync_batch | id, stream_id, seq, manifest_hash, prev_hash, state, imported_at, source_capture_bounds | stream+seq 및 manifest unique, same ID 다른 hash 차단 |
| sync_receipt | id, batch_id, stage, recorded_at, local_snapshot_ref, checksum | imported/validated/backed_up/ack 이벤트 |
| app_job | id, type, fingerprint, state, lease, fencing, progress, checkpoint, error_code | queue mutable+job events, claim 동시성 검증 |
| operating_budget_event | id, kind, scope, measured_at, actual, reserved, threshold, action | local wall-clock/RAM/cloud storage/requests 이력 |
| resolution_event | id, operation_id, requested_hash, resolved_hash, policy_id, reason, actual_source/engine, evidence_refs, recorded_at | 허용한 대체/복구 이력, 실제 request/result identity 연결 |

큰 ledger·feature 값은 파티션과 적절한 index를 실측해 적용한다. 거대한 JSONB 하나에 전종목×10년 배열을 저장하지 않는다. 위 `values/positions/counts`는 작은 메타 또는 bounded 구조이고 대량 결과는 row 테이블·cache shard로 분리한다. Foreign key dependency와 migration rollback은 단계별 검증한다.

## 시각과 조회

| 시각 | 의미 |
|---|---|
| session/effective_at | 가격 경제 날짜 또는 사건 경제 효력 |
| published_at | 근거 있는 공개 시각, 날짜 정밀도 별도 |
| available_at | 검증된 정책상 사용 가능 시각 |
| captured_at | 해당 버전을 실제 원격/로컬 수신한 시각 |
| extracted_at/first_seen_at | SQL 원계정 추출·최초 발견 시각 |
| imported_at | 해당 원격 batch가 로컬 SQL에 적재된 시각 |
| materialized_at/generated_at | 특징·계산·시그널을 실제 만든 시각 |
| evaluation_at/as_of | 연구·분석이 평가하는 cutoff |

기존 `observed_system`은 실제 수집·추출·선택/계산의 clock 정책을 따른다. 원격은 같은 수집 시스템의 capture scope로 취급하고 `imported_at`을 capture로 덮어쓰지 않는다. “그때 로컬 PC에서 볼 수 있었는가”는 신규 `local_visibility_at` 제약으로 import receipt cutoff를 추가한다. 이를 기존 public PIT와 혼동하지 않는다.

기존 `reconstructed_public`은 해당 query contract의 공개 증거가 충분한 버전만 사용한다. 특히 재무의 날짜-only 공개일은 달력의 보수적 다음 eligible 세션 정책을 따른다. 재무 capture-only 값을 과거 공개 값으로 추정하지 않는다. 기존 partial은 clock/integrity를 우회하지 않는다. 신규 가격 세션 reader는 기존 public reader를 바꾸는 작업과 구분한다.

research_mode는 기본 검증 등급의 `strict_pit`와 명시 탐색 등급의 `exploratory_snapshot`을 구분한다. 이름 strict_pit는 재무 공개 버전 PIT+가격 세션 정책을 뜻하며 가격 수신 시각의 소급 사용 인증을 요구하지 않는다. 기존 QueryContext history_mode에는 두 기존 값만 남긴다. 탐색 manifest는 snapshot_cutoff·qualification gaps·bias flags·certification_eligible=false·signal_use_policy를 갖는다. 미래 봉·재무 정정·원 clock 조작을 허용하는 waiver는 없다.

DEC-10/16의 확정 규칙은 재무 공개 버전 PIT와 가격 세션 clock의 분리다. 신규 분석 dataset은 `financial_history_mode`, `price_clock_policy=historical_price_session_v1`, `price_revision_policy=frozen_latest_observation_v1`, `adjustment_as_of`, `data_quality`, `selection_state`, `validation_state`를 명시한다. 가격 captured_at/imported_at은 감사·복구용이며 2018년 봉을 2026년에 수신했다는 이유만으로 과거 가격 지표에서 제외하지 않는다.

`analysis_price_query_v1`은 봉인된 정확 membership·원행 hash·원 source/basis와 세션을 검증하는 신규 reader다. 기존 release_query_v5와 과거 release manifest의 clock/hash를 수정하지 않는다. 가격 row는 경제 세션 종료≤decision cutoff를 요구하고 미래 봉·centered window·전체기간 fit을 배제한다. 재무 row는 period_end가 아니라 해당 공개 버전의 사용 가능 cutoff를 요구한다. 최신 재무 정정을 원공시 시각에 붙이는 waiver는 어느 모드에도 없다.

가격 revision은 선택 release에 고정된 값을 사용하고 원관측/정정 이력과 revision policy를 보고한다. split-adjusted 계산은 해당 decision까지 효력이 발생한 사건으로 당시 basis를 맞춘다. 미래 split/dividend를 과거 절대 가격·threshold에 소급하거나 공급자 이미 조정된 계열에 현금배당을 중복 가산하지 않는다. source-defined series 탐색은 basis/정의·이력 부족·회계 가정을 별도 표시하고 검증/실제 원주가 ledger와 합치지 않는다.

## 분석 입력 DTO

모든 `AnalysisDataset`은 `dataset_id`, `release_id`, `manifest_hash`, `target_manifest_hash`, `universe_scope`, `membership_policy`, `history_mode`, `research_mode`, `cutoff_schedule`, `market_calendar_version`, `price_basis`, `currency`, `quality_policy`, `qualified_flags`, `input_refs`, `excluded_entities`를 갖는다.

위 `history_mode`는 재무 공개/관측 조회 정책을 뜻하며 가격은 신규 price_clock_policy로 명시한다. domain clock을 하나의 available_at에 억지로 합치지 않는다. certification_eligible=false는 검증 인증 금지이며 사용자 selected 상태의 금지가 아니다. 사용자 선택은 입력 품질/validation을 바꾸지 않는다.

20의 EvaluationContext는 기존 dataset/feature/decision DTO에 market cutoff·pool selector/config/schedule·definition_bundle_hash·state/portfolio refs를 고정한다. bundle은 전략과 참조하는 feature/pool/regime 정의·역할·parameter·execution 계약을 연결하며 새 테이블/서비스 없이 기존 immutable definition/config와 run metadata를 사용한다. 목표의 explicit_subset/full_portfolio, entry/exit status·reason, pool membership·managed positions를 구분한다. SQL 쿼리/기존 release의 clock을 이 새 앱 DTO 때문에 바꾸지 않는다.

신규 분석 manifest는 `data_origin=actual_source|test_fixture|demo`와 environment를 갖는다. 운영 계산·시그널·사용자 선택 목록의 실제 결과 경로는 test_fixture/demo를 거부한다. 원천 clock·hash가 실제 기록이어야 actual_source로 등록할 수 있고 label만 바꿔 실제 데이터로 인증하지 않는다. 기존 sealed release에 필드를 소급 추가하지 않고 신규 dataset adapter에서 source evidence로 판정한다.

연구·스크리닝·시그널의 manifest는 requested/resolved configuration·resolution_policy_id/events·actual_engine/sources·quality_status·execution_assumptions·trial_history를 보존한다. default alias는 사전 qualification lock으로 시작 전에 resolve한다. 실행 중 source/engine 자동 전환은 금지한다. 별도 명시 config의 변경은 새 fingerprint·dataset/run과 원 요청·이유를 연결한다.

DEC-17의 실행은 `execution_policy=research_daily_tradability_v1`, `observed_tradability`, `tradability_assumption`, `ex_post_execution_assumption`, `blocked_reasons`를 별도 기록한다. unknown 관측을 정상 assertion으로 바꾸거나 data_quality를 qualified로 올리지 않는다. t 폐장 decision 입력과 t+1 모의체결/사후 조건 입력의 refs를 구분해 미래 OHLCV가 전략 판단·sizing으로 돌아가지 않도록 한다. 연구 체결 가정이 없으면 기존 unknown gate를 유지한다.

bar는 security_id·session·raw OHLCV·basis·source scope·known clock·tradability·action refs를 갖는다. 재무 feature는 issuer_id·period interval·scope·currency·definition·public clock·input fact refs·status를 갖는다. column name만으로 numeric 의미를 추정하지 않는다.

source selection과 quality policy를 거친 데이터만 panel로 변환한다. `missing`, `not_applicable`, `unsupported`, `unknown`, `quarantined`, `stale`, `nonpositive_comparison`을 0 또는 False로 치환하지 않는다. 조건 판정은 three-valued logic이며 unknown은 gate에 따라 blocked가 된다.

## 변경분 형식

`bundle_v1`은 정형 JSON manifest와 gzip NDJSON table delta·기존 raw object 파일로 시작한다. Decimal은 문자열, UTC는 ISO8601, nil은 null, binary는 별도 hash-addressed 객체다. 압축 전·후 hash/bytes·row count·schema version·종속 table 순서·entity ID를 검증한다.

```json
{
  "bundle_version": "bundle_v1",
  "batch_id": "example-uuid",
  "stream_id": "collector-kis-account-a",
  "sequence": 42,
  "previous_manifest_sha256": "<sha256>",
  "target_manifest_id": "<existing-uuid>",
  "schema_revision": "<alembic-head>",
  "policy_hash": "<sha256>",
  "owner_epoch": 7,
  "files": [{"key": "facts/prices.ndjson.gz", "sha256": "<sha256>", "rows": 1000, "bytes": 50000}],
  "dependency_refs": [{"kind": "source_contract", "id": "<uuid>", "hash": "<sha256>"}],
  "checkpoint_after_hash": "<sha256>",
  "capture_bounds_utc": ["2026-10-09T07:00:00Z", "2026-10-09T07:10:00Z"]
}
```

예시 값은 schema 구조를 설명하기 위한 것으로 실제 manifest가 아니다. hash·row count·size·path allowlist·schema/policy compatibility·identity collision·dependency closure 중 하나라도 실패하면 import를 거부한다. 임의 SQL·pickle·외부 executable·압축 path traversal은 허용하지 않는다.

## 영속 commit과 소유권

R2 객체 경로는 `objects/<sha256>`, `streams/<id>/batches/<seq>/<manifest-hash>.json`, `control/<provider-account>.json`, `acks/<local-id>/<batch-id>.json`으로 둔다. provider account와 collector stream은 1:1로 고정하며 KIS KR/US는 같은 stream을 쓴다. 객체·manifest는 immutable이고 **하나의 control 객체**가 owner_id·epoch·lease_until·committed_sequence·manifest_hash·checkpoint_hash·rate resume을 함께 소유한다.

payload와 checkpoint를 먼저 올리고 hash를 검증한다. immutable manifest를 저장한 뒤 현재 owner epoch·lease 잔여·이전 sequence를 확인하고 control을 읽은 ETag의 `If-Match` 또는 최초 `If-None-Match`로 갱신한다. 이 단일 CAS가 새 head/checkpoint와 해당 owner epoch를 동시에 commit하는 경계다. 새 owner 획득·renew도 같은 control ETag를 바꾸므로 stale writer의 commit은 실패한다. CAS 실패 시 새 control을 다시 읽고 같은 owner/epoch·head인지 확인하기 전 재시도하지 않는다.

lease 종료 직전에는 최대 request timeout+clock skew safety margin보다 잔여가 적으면 새 요청·commit을 시작하지 않는다. lease expiry만으로 새 writer를 무조건 시작하지 않고 control CAS로 takeover하고 이전 실행의 drain/grace를 확인한다. endpoint request와 네트워크 지연이 hard exactly-once 호출을 보장하지 않으므로 audit/idempotent ingest로 보완한다. 성공한 control의 head chain만 committed stream으로 읽는다. orphan은 참조 closure 확인 후 grace 정리한다. [R2 S3 호환표](https://developers.cloudflare.com/r2/api/s3/api/)와 실제 CAS 충돌·재시도를 실증한다.

각 work boundary에서 control의 owner·epoch·lease를 확인한다. 소유권을 잃은 request 응답은 audit로 보존할 수 있어도 authoritative control을 commit하지 않는다. 원격 소유 중 local backfill은 해당 공급자를 사용하지 않고 요청을 대기시킨다. 기존 legacy collector는 이 protocol에 참여하기 전 신규 요청 중단·in-flight 종료·checkpoint 보존·SQL lease 해제를 완료해야 한다.

batch sequence는 하나의 collector stream에서 전체 domain을 포괄하며 head 업데이트를 직렬화한다. 독립 SEC·DART stream은 병렬 가능하나 같은 source account는 single owner다. 로컬은 stream 순서와 dependency를 확인하고 gap을 건너뛰어 cursor를 완료 처리하지 않는다.

## 로컬 import와 ack

1. batch chain·schema·hash·capture clock·부모 refs·identity를 검증한다.
2. raw 파일을 허용된 상대 root에 원자 저장하고 fsync/존재를 확인한다.
3. SQL transaction에서 FK 순서로 append하고 기존 ID+hash는 reuse한다.
4. 같은 ID에 다른 내용은 collision으로 차단한다. sync_batch와 imported receipt를 같은 transaction에 commit한다.
5. 읽기 검증 후 validated receipt를 남긴다. backup이 해당 SQL+raw closure를 포함하면 backed_up receipt를 남긴다.
6. ack 업로드 실패는 재시도한다. SQL import를 반복해도 fact·capture를 추가 생성하지 않는다.

파일 저장 뒤 SQL 실패 시 파일은 orphan으로 남을 수 있지만 committed DB가 없는 파일을 참조하지 않아야 한다. remote 정리는 backed_up ack와 checkpoint/reference 보호 조건을 모두 만족할 때만 한다. backup이 독립 장치인지 여부를 ack metadata에 표시하고 동일 장치 사본을 재해복구 보증으로 표시하지 않는다.
