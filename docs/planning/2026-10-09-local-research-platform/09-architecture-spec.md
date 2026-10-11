# 로컬 연구 플랫폼 아키텍처 명세

## 경계와 불변식

데이터 수집 계층은 원관측·정정·증거를 소유한다. 고정 조회 계층은 시점·release·품질을 소유한다. 분석 계층은 전략·모델·시뮬레이션·평가를 소유한다. 앱 계층은 사용자의 작업·보유 원장·화면 계약을 소유한다. 원격 transport는 파일 전달·checkpoint·ack를 소유하며 숫자의 금융 의미를 다시 해석하지 않는다.

기존 release_query_v5·두 history mode·immutable 이력은 유지한다. 신규 `analysis_price_query_v1` reader는 고정 release membership/hash를 검증하고 `historical_price_session_v1`로 해당 세션까지의 가격을 읽는다. 기존 reader의 available/captured predicate를 조용히 바꾸지 않는다. 재무 reader는 공개 버전 PIT를 유지한다. 검증/탐색은 provenance·qualification/해석 정책을 분리한다.

범위와 대체 경로는 [17 구현 품질 계약](17-implementation-guardrails.md)을 따른다. 운영 call graph에 mock·demo·random 응답을 붙여 요청을 성공시키지 않는다. 의미가 같은 cache 재생성·retry는 기록된 복구이고, 의미가 바뀌는 engine/source/model 선택은 명시 정책과 실제 실행 metadata를 요구한다.

## 디렉터리와 책임

아래 경로는 새로 구현할 책임 배치다. 기존 모듈 이동은 필요하지 않으며, 입력/조회 adapter로 연결한다.

| 경로 | 소유 책임 | 재사용·의존 |
|---|---|---|
| `src/backtest_data/history/` | listing·lifecycle·tradability·상품 이력 | catalog identity·source contracts |
| `src/backtest_data/quality/` | coverage·profile·전략 입력 적격성 | 기존 validation·release policy |
| `src/backtest_data/projections/` | 고정 읽기·청크·가격 세션 reader·재무 PIT·캐시 | 기존 봉인과 query policy 보존, 신규 analysis price policy 별도 |
| `src/backtest_data/ingestion/incremental.py` | 신규 날짜·공시·정정 작업 생성 | CollectionJob·target manifest |
| `src/backtest_data/ingestion/portable_state.py` | 원격 subset export/import | 기존 job·catalog ID·fencing |
| `src/backtest_data/sync/` | bundle·closure·원자 import·R2/폴더 transport | existing raw store·immutable facts |
| `src/backtest_research/contracts.py` | typed dataset·feature·strategy·engine 계약 | 금융 provider/웹 framework 비의존 |
| `src/backtest_research/features/` | 가격·재무 특징과 정의 registry | dataset protocol |
| `src/backtest_research/strategies/` | Python protocol/registry·새 플러그인·예시 | feature/state/portfolio/input policy contract |
| `src/backtest_research/engines/` | qualification된 framework adapter·capability | StrategyDecision·execution config, 손계산 oracle는 검증 전용 |
| `src/backtest_research/evaluation/` | 지표·walk-forward·검증 근거 | ledger·run config |
| `src/backtest_research/regimes/` | causal 규칙·후속 모델 adapter | versioned features/model |
| `src/backtest_research/screening/` | 종목·전략 ranking | features·evaluation·scope |
| `src/backtest_research/signals/` | selection/input gate·position diff·expiry | strategy output·quality·portfolio DTO |
| `src/backtest_app/` | FastAPI·API DTO·SQL repositories·job runner | research services·data adapters |
| `frontend/` | React/TypeScript·페이지·chart adapter | REST API만 |
| `scripts/start-local.ps1` | 상태 확인·thread limits·브라우저 열기 | Windows process·Docker 상태 |
| `scripts/stop-local.ps1` | 앱이 소유한 서비스 안전 종료 | 실행 PID/identity·checkpoint |
| `config/research-defaults.yaml` | 확정 기본값과 기능 flag | versioned config |

현재 코드는 `catalog/aliases.py`, `ingestion/supervisor.py`, `prices/adjustments.py`, `fundamentals/derive.py`, `releases/query.py`, `releases/build.py`, `releases/backup.py`를 재사용한다. 신규 API가 기존 internal 함수의 지원 범위를 우회하지 않게 계약 adapter를 둔다.

## 프로세스 구성

| 프로세스 | 역할 | 초기 한도 |
|---|---|---|
| PostgreSQL | 전체 이력·앱 메타·큐 | 기존 127.0.0.1:54328 유지, volume 위치 확인 |
| API server | 화면 조회·job 생성·상태 | 127.0.0.1:8000, 무거운 계산 없음 |
| Research worker | projection·백테스트·레짐·스크리닝·신호 | 무거운 worker 1개, BLAS/Numba threads 최대 4 후보 |
| Local sync/collector | 변경분 import·복구·명시 수집 | provider ownership·DB lease 준수 |
| Frontend | 개발 서버 또는 API가 제공하는 build | 개발 5173, 배포 build는 같은 API origin |
| Remote runner | transient SQL·collect·bundle·checkpoint | timeout·single provider owner·durable state |

Redis·Celery·Kubernetes·클라우드 영구 DB는 초기 필수 의존성이 아니다. 분석 job queue는 PostgreSQL의 상태 전이·claim·heartbeat로 구현한다. worker 장애 시 lease 만료와 재개 정책으로 복구하고, API 요청 중 계산하지 않는다.

## 분석 입력과 캐시

SQL release projection→검증 DTO→단위/세션 유지 panel→features→strategy/model→engine/evaluation 순서다. NUMERIC 원관측은 SQL에 보존하고 계산에 필요한 경우 float64로 변환한 정책·오차·원 input ref를 기록한다. 체결 원장과 최종 금액은 명시 반올림 정책으로 검증한다.

캐시 키는 release manifest hash, universe membership hash, scope, cutoff schedule, history mode, research mode, quality policy, price basis, currency, feature definition, engine/코드 version을 포함한다. 같은 release의 캐시는 원문을 재선택하지 않는다. 새 정정은 새 release·새 cache key로 처리한다.

원본 closure의 전체 hash 재검증은 build/validation에서 수행한다. 일반 차트 요청마다 수백만 evidence를 scan하지 않는다. cache를 발행할 때 full closure 검증 결과를 연결하고 페이지별 row/range hash를 검증한다. cache corruption·policy mismatch는 cache 재생성 또는 차단으로 처리한다.

전체 종목×기간×field×parameter 조합을 하나의 배열로 만들지 않는다. 종목·기간·parameter를 각각 청크 처리하고 필수 cross-sectional rank만 같은 session에서 합친다. Parquet은 disposable cache, DuckDB는 읽기 분석 adapter 후보이며 결과와 primary facts는 PostgreSQL에 남긴다.

가격 cache key에는 price_clock_policy·frozen revision policy·price basis·action adjustment cutoff를 추가한다. 2018년 지표 요청은 2018년까지의 봉과 해당 시점까지 유효한 조정만 사용한다. 전체 파일에 미래 봉이 존재해도 producer의 FeatureView에서 조회/fit 경계를 강제한다.

## Python 전략 확장 계약

`StrategySpec`은 protocol_version·id/version·code_hash·config_schema·required_inputs·warmup·decision_schedule·state_schema·output_kind·data_policy·engine_capabilities를 선언한다. `decide(FeatureView, PortfolioView, StrategyState, Config)`는 weights 또는 advisory signals·reason/input refs·새 state를 반환한다. signal-only 전략을 백테스트하려면 명시 allocation/sizing policy가 필요하며 자동 동일비중 변환은 하지 않는다.

등록은 로컬 Python 모듈/설치 패키지와 registry manifest를 이용한다. UI는 typed parameter와 실행/비교만 제공하고 코드를 임의 실행하는 endpoint를 만들지 않는다. 새 plugin 등록에는 새 hash/version·capability/data policy preflight가 필요하고 engine/SQL/UI core를 수정하지 않는 extension probe를 통과해야 한다. Strategy는 source HTTP·SQL 세션·framework native 객체·broker·React 상태를 알지 못한다.

Model/Feature extension protocol은 시간·단위·fit/decision 경계·artifact identity를 선언할 자리를 갖는다. 이번에는 HMM 등 고급 구현·provider·학습 의존성·운영 endpoint를 연결하지 않는다.

[20 연결 계약](20-pipeline-and-extension-contracts.md)의 작은 EvaluationContext를 기존 DTO에 공유한다. 같은 Python selector가 과거/현재의 전략 진입 풀을 만들고 관리 보유는 별도로 판단한다. 레짐의 표시·진입 필터·비중·청산 역할과 관련 정의를 bundle hash에 고정한다. 기본 required_inputs 검사에 필요 전략만 entry/exit 목록 분리 옵션을 둔다. targets의 explicit_subset/full_portfolio와 null/status를 구분해 생략을 청산으로 처리하지 않는다. 범용 workflow/그래프 엔진·두 번째 registry 서비스는 추가하지 않는다.

## 기술 결정과 실증

기존 Python·SQLAlchemy·Alembic·PostgreSQL을 유지한다. FastAPI/Pydantic·React/Vite/TypeScript·Lightweight Charts·NumPy/Pandas/Numba/vectorbt는 **새 의존성 후보**다. 구현 첫 spike에서 Windows·현재 Python·binary wheel·버전 호환을 확인하고 lock한다. 현재 Python 환경을 일괄 upgrade하지 않는다.

vectorbt 호환이 기존 Python과 맞지 않으면 분리 research venv/worker를 실증하고 파일/DTO·manifest 계약으로 통신한다. 기존 수집 환경은 그대로 유지한다. 새 라이브러리의 최신 API를 추정해 구현하지 않고 lock된 버전 공식 문서를 기준으로 adapter를 작성한다.

## 실행·오류 경계

job 상태는 queued→running→succeeded/failed/cancelled/paused다. heartbeat·progress·checkpoint·입력 fingerprint를 저장한다. pause는 세션/청크 경계에서, cancel은 cooperative cancellation으로 처리한다. 실패한 artifact는 성공 결과로 노출하지 않는다.

ProviderError, DataUnavailable, QualityBlocked, UnsupportedCorporateAction, ResearchOnly, ResourceBudgetExceeded, IntegrityFailure, ConfigurationMismatch를 구분해 API reason code로 전달한다. 사용자에게 raw stack trace·DSN·token을 표시하지 않는다.

예외를 넓게 잡고 빈 표/0값/기본 signal로 바꾸지 않는다. 정상 no-position·empty·unavailable·blocked·failed를 구분한다. 새 job의 성공은 실제 요청 fingerprint와 일치하는 저장 결과가 commit되고 후속 조회되는 경우에만 가능하다. optional의 unsupported는 mandatory의 완료 근거가 아니다.

## 아키텍처 검증

전략 모듈에서 HTTP·SQL·broker import가 없어야 한다. 데이터 adapter 교체 시 동일 fixture 결과가 유지돼야 한다. cold/warm query, cache 손상, release version mismatch, worker crash, parallel claim, app restart, offline을 검증한다. SQL 전체 이력·target universe·existing releases가 달라지지 않았다는 회귀 검사를 수행한다.
