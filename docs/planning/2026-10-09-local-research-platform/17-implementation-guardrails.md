# 구현 품질 대체 경로 방법론 비용 계약

버전 0.3 구현 기준 기획안. 이 문서는 기존 로컬 연구 플랫폼의 기획 범위만 다룬다. 외부 연구 프로젝트의 모델·코드·런타임 통합은 이번 범위에 추가하지 않는다. 허위 완료·암묵적 대체를 막는 불변식과 사용자와 확정한 trade-off를 구분한다.

## 정확도와 결과 제공의 경계

실제 입력으로 계산한 결과, 부족 입력의 정확한 진단, 이미 검증된 저장 결과의 조회는 각각 유용한 산출물이다. 어떤 상태에서도 가짜 결과나 다른 계산을 원래 요청 결과로 표시하지 않는다. 데이터 확보 범위·계산 구현 여부·운영 연결 여부·검증 수준을 별도로 보고한다.

최대한 clean dataset을 구축한다는 목표는 유지한다. 부족한 자료를 더 작은 전체 대상, 임의 시각, 대체 숫자, 다른 전략으로 바꾸어 완료율을 올리지 않는다. DEC-01에 따라 적격 일부 범위의 결과와 허용된 미검증 탐색 결과를 구분해 제공하고 전체 목표·누락 목록은 유지한다.

## 사용자 결정 원장

`pending`은 아직 답변을 받지 않은 선택이다. 제안이 권장값이라는 이유로 사용자 확정으로 바꾸지 않는다. 이미 확정한 시장·기간·일봉·clean 우선·자동 주문 없음·30일 미접속 목표는 재질문하지 않는다.

| 결정 | 정한 내용 | 현재 상태 | 확정 정책·선정 방향 | 반영 문서 |
|---|---|---|---|---|
| DEC-01 | 전수 확보 전 결과 제공 | confirmed | 미검증 탐색도 별도 표시; 재무 PIT·가격 세션·source-defined 계열은 DEC-10/11/16 적용 | 02/03/05/15 |
| DEC-02 | 일봉 모의체결 또는 실제 체결 증거 요구 | confirmed | 사용자 선택: 적절한 모의계산 허용; 익일 시가·명시 비용·미체결 가정으로 고정 | 05/11/15 |
| DEC-03 | 기업행동 부족 종목의 표시 | confirmed | 검증/탐색 구분; DEC-11에 따라 명시된 공급자 수정주가 탐색도 허용 | 03/08/12 |
| DEC-04 | 전략 범위 | confirmed | 네 전략 고정 제품이 아니라 확장 플랫폼; 초기 스타일은 예시 | 02/05/06/09/14 |
| DEC-05 | engine 선택 | confirmed direction | vectorbt 고정 해제, 더 적합한 프레임워크 비교·qualification; 자동 전환 미허용 | 05/09/14/18 |
| DEC-06 | 공급자 장애의 source 전환 | confirmed | 자동 전환 금지, 고정 source retry/resume·부족 진단; 새 source는 별도 명시 dataset | 03/04/10/13 |
| DEC-07 | 초기 유료 이용 상한 | confirmed | 자동 이용 0원; 합계 월 10,000원 이하 대안은 제안만, 사용자 확인 후 이용 결정 | 02/13/16/18 |
| DEC-08 | 이번 작업의 시간 정책 | confirmed | 이번 기획·구현·검증은 완료까지; 평상시 앱은 5시간 사용 목표 유지 | 00/04/13/14 |
| DEC-09 | 전략 작성 방식 | confirmed | Python 플러그인·UI 파라미터, 화면 조건 조합 builder는 초기 제외 | 05/08/09/12/14 |
| DEC-10 | 과거 재무 시점·버전 | confirmed | 사용자 선택: PIT 필수, 미래 공시·정정 소급 금지; 당시 버전 부족 시 해당 계산 차단 | 03/05/10/11/15 |
| DEC-11 | 공급자 수정주가 탐색 | confirmed | source가 조정 기준을 명시한 시계열 탐색 허용, 임의 조정·검증 총수익 주장 금지 | 03/05/10/11/12 |
| DEC-12 | 연구용 신호와 선택 전략 | confirmed direction | 모든 전략은 연구로 시작, 연구 신호 별도 표시; 선택 상태와 검증 등급 분리 | 06/07/10/12 |
| DEC-13 | 초기 고급 수학 방법 | confirmed | 확장 인터페이스만 제공; HMM 등 구체적 추가 방법은 초기 연결하지 않음 | 06/09/14/18 |
| DEC-14 | 5시간 해석 | confirmed | 사용자 재확인: 이번 작업 완료까지, 일상 앱 5시간 목표 유지 | 00/04/13/14 |
| DEC-15 | 선택 전략의 결정 주체 | confirmed | 사용자 최종 선택, 자동 승격 없음; 선택과 데이터/성과 검증은 독립 표시 | 06/07/10/12 |
| DEC-16 | 가격과 재무의 시간 기준 | confirmed | 가격은 거래 세션/계산 cutoff, 수신 날짜는 감사 정보; 미래 봉/학습 유입 금지. 재무는 공개·정정 버전 PIT | 03/05/09/10/11/15 |
| DEC-17 | 과거 거래가능 이력 부족의 연구용 가정 | confirmed | 유효한 원주가 OHLCV·양수 거래량의 체결 세션은 연구용 익일 시가 모의체결 허용. unknown은 보존하고 research_daily_tradability_v1 가정을 별도 기록; 확인된 정지·결측·잘못된 action은 차단 | 05/07/10/11/12/15/19 |
| DEC-18 | 판단별 입력 검사와 구현 복잡도 | confirmed direction | 사용자: 복잡도 억제. 설계 기본안: 공통 required_inputs 검사, 필요한 전략만 entry/exit 목록 분리 옵션; 범용 조건/의존성 엔진 없음 | 06/07/09/10/14/20 |
| DEC-19 | 스크리너와 전략 풀 | confirmed | 사용자: 전략별 풀이 진입 판단 풀. 같은 Python selector의 시점별 historical/current 실행, 보유 관리 별도 | 06/07/09/10/14/20 |
| DEC-20 | 원격과 로컬의 신호 계산 | pending | 원격 자동 과금 지양은 확정. 데이터만 원격 수집 후 로컬 일별 신호 저장인지 원격 무료 신호 계산까지인지 확인 중 | 04/07/13/14/20 |
| DEC-21 | 초기 거래 범위 | pending | 롱온리·현금 기본안 또는 공매도·차입 포함, 전체 상품 데이터 대상은 유지 | 01/05/11/14/18/20 |
| DEC-22 | 한국/미국의 자금과 환전 | pending | 시장별 독립 KRW/USD 연구 계좌 기본안 또는 초기 공유 자금 회계 | 01/05/10/11/14/20 |
| DEC-23 | 현재 신호의 수량 출력 | pending | 조건·근거·선언 목표 비중 기본안 또는 초기 실제 참고 수량과 수동 계좌 입력 | 01/07/08/12/14/20 |

위 결정의 어느 선택도 원관측·시각·전략 정의·봉인·인수 증거를 소급 조작하도록 허용하지 않는다. 실제 scope/정의의 변경은 새 버전·명시 설정·별도 결과로 표현한다.

DEC-01/11에 따라 별도 탐색 경로를 초기 범위에 추가하고 실제 데이터 clock/hash·미확보 qualification·한계를 보존한다. 구조적으로 잘못된 숫자·identity 충돌·무결성 실패·가짜 데이터는 어느 모드도 허용하지 않는다. 과거 재무는 탐색에서도 DEC-10의 PIT가 필수다. 가격은 DEC-16의 세션 기준이며 늦은 수신 자체로 과거 지표를 차단하지 않는다. 미래 봉·미래 학습과 확인된 미래 기업행동을 과거 판단 특징에 섞는 것은 금지한다. 공급자 조정 이력이 미확보인 탐색 계열은 한계를 표시하며, 이력 부족만으로 확인된 미래 정보 유입이라고 단정하지 않는다.

모든 새 전략은 research로 시작하고 연구 신호도 표시할 수 있다. 사용자 선택은 selected 상태로 기록하되 데이터나 성과의 validation을 자동 승격하지 않는다. selected 전략의 필수 입력이 부족하면 계산은 blocked다. 연구 결과·선택된 전략 결과·검증된 입력/성과의 상태를 UI/export에서 독립적으로 표시한다.

DEC-17은 공식 거래가능 이력의 미확보만 연구용 체결 가정으로 분리한다. `observed_tradability=unknown`을 실제 거래가능 true로 바꾸지 않는다. 체결 세션 OHLCV는 모의체결/사후 제약 평가에만 사용하고 그 거래량을 과거 신호나 주문 크기에 미리 사용하지 않는다. verified 체결가능 등급과 분리하며 다른 필수 입력 실패를 우회하지 않는다.

## 구현 불변식

| ID | 반드시 지킬 계약 | 확인할 증거 |
|---|---|---|
| G01 | 요청한 전략·engine·source·mode를 성공시키려고 조용히 바꾸지 않는다 | requested/resolved configuration과 변경 event, 금지 대체 반례 |
| G02 | Python 전략 확장·검증 프레임워크·SQL·웹앱·sync의 필수 기능을 몰래 optional로 바꾸지 않는다 | scope-lock의 mandatory/conditional/deferred와 변경 원장; 예시 전략 수는 제품 완료 기준 아님 |
| G03 | `research_partial`은 고정 범위의 clock·integrity·해당 계산의 필수 적격성을 우회하지 않는다 | 필요한 qualification 누락 시 blocked·reason·분모 유지 |
| G04 | 없는 가격·재무·기업행동·시각·거래가능·지수·FX 값을 추정 숫자로 채우지 않는다 | null/status·source refs·unknown/unsupported 진단 |
| G05 | 초기 rule regime를 HMM·AI·확률 예측으로 표시하거나 평균회귀를 추세 규칙으로 대체하지 않는다 | 실제 definition hash·수식·출력 타입·입력 lineage |
| G06 | 데이터 조회 실패·미구현 route에 샘플/랜덤/고정 JSON을 반환하지 않는다 | 실제 API→서비스→SQL/worker 경로의 통합 증거 |
| G07 | fixture·demo·mock 산출물을 운영 SQL·차트·스크리너·signal·승인 근거에 넣지 않는다 | data_origin·environment·DB/run 분리 및 운영 거부 검증 |
| G08 | 빈 결과를 정상 no-position, unknown, blocked, error와 구분한다 | API value/status/reason·job state·UI/export 일치 |
| G09 | 명시 engine가 미지원이면 기본은 rejection이다. 허용된 전환도 정책과 실제 engine를 기록한다 | engine capability·parity·DEC-05·신규 run fingerprint |
| G10 | 실행 중 source 자동 변경은 금지한다. 새 source는 qualification된 별도 dataset/config로만 등록한다 | pinned source·selection event·원 요청 재시도·DEC-06 |
| G11 | 단순 손계산 테스트와 실데이터/SQL/API 인수를 서로 대신하지 않는다 | hand fixture·real snapshot·저장/재시작·실패 주입 증거 각각 |
| G12 | 실패 테스트를 통과시키려고 기대값·허용 오차·필수 gate를 근거 없이 느슨하게 바꾸지 않는다 | 정의 변경 이유·새 version·독립 기준 fixture·diff 검토 |
| G13 | 성공을 선언하려면 입력부터 저장·응답·화면까지 실제 경로와 산출물 identity가 이어져야 한다 | release→dataset→feature→run/batch→API/export lineage |
| G14 | 모든 계산·정정·시그널·import는 기존 고정 이력과 정확한 시각을 보존한다 | frozen snapshot·idempotency·supersession·clock 반례 |
| G15 | 예산 부족/OOM/timeout을 종목·기간·필수 필드·trial의 조용한 삭제로 해결하지 않는다 | bounded chunk·spill·checkpoint·paused 및 전체 분모 |
| G16 | fallback·재시도·rollback은 허용 사유·범위·상한·actual path를 명시한다 | structured event·reason code·retry budget·원인 보존 |
| G17 | 미완료 기본 기능은 blocked/unimplemented로 보고하며 필수 기능에 임의 not_applicable를 쓰지 않는다 | feature별 구현·입력·운영·증거 상태 원장 |
| G18 | 유료 데이터·서비스·PRO·GPU·외부 유료 모델을 필수 의존성 또는 자동 실행 경로에 추가하지 않는다 | 월 합계10,000원 이내 제안과 사용자 이용 확인, 자동 이용 상한0원 |
| G19 | 연구 후보·비용·분할·지표를 결과 확인 뒤 자동 변경하거나 실패 trial을 숨기지 않는다 | frozen eval plan·전체 trial/holdout use·변경 원장 |
| G20 | 외부 입력 차단이 생겨도 영향 없는 문서·코드·fixture·UI·진단 경로 개발은 진행한다 | module verification과 operational blocked를 구분한 보고 |

`NotImplementedError`, 빈 dataframe, 기본 0값 자체를 문자열 검색만으로 금지하지 않는다. 실제 production call graph에서 요청한 기능이 계산 없이 성공했는지를 확인한다. 테스트 전용 substitute·명시 unavailable 응답·등록하지 않은 후속 기능은 운영 성공 경로와 구분한다.

## 대체 경로 분류

| 사건 | 유지해야 할 의미 | 허용 가능한 처리 | 성공으로 처리하면 안 되는 것 |
|---|---|---|---|
| HTTP 429·일시 장애 | 같은 요청·source·clock·scope | backoff/Retry-After·bounded retry·resume | 다른 소스/기간을 성공 응답으로 감춤 |
| auth/key 누락 | configured 상태·원 요청 보존 | 해당 source 작업 blocked, 다른 독립 작업 진행 | 빈 결과로 source 수집 완료 |
| cache 손상 | 같은 frozen dataset identity | SQL에서 같은 key 재생성 또는 fail | 최신 DB·다른 release로 결과 대체 |
| vectorbt 미지원 | 선택 engine·지원 사건 범위 | DEC-05에 따른 rejection/명시 전환 | reference 결과를 vectorbt 결과라고 표시 |
| 가격 source 장애 | 고정 source·금융 의미·identity·출처 | 같은 source retry/resume·gap; 별도 source dataset는 명시 선택 | 자동 source 전환·IEX/SIP·raw/adjusted·정규/전체 세션 혼합 |
| 기업행동 입력 부족 | 해당 조정/회계의 적격성 | 원주가·진단·DEC-11 허용 공급자 계열의 별도 탐색; 필수 action 계산 blocked | 가격 급변을 임의 split ratio로 교정·탐색을 검증 총수익으로 표시 |
| 재무 필수 입력 결측 | 같은 fiscal·scope·정의 | unavailable·입력 보강·독립 전략 진행 | 가격/다른 이익/별도 재무로 score를 채움 |
| RAM/time 초과 | 같은 전체 대상·계산 정의 | chunk·spill·pause/resume | 앞 N종목만 실행하고 전수 처리로 표시 |
| 원격 quota 부족 | 미수신 batch·checkpoint·target | hold·진단·safe owner handoff | payload 삭제·cursor 전진·자동 유료 전환 |
| 최적화 실패 | 원래 constraints·objective | infeasible 이유와 요청 차단 | constraints 제거·동일비중을 같은 run으로 반환 |
| benchmark/FX 부족 | 원 비교 대상·통화 | unavailable 또는 별도 명시 proxy config | ETF를 공식 지수로 명명·FX=1 처리 |

cache 재생성·동일 요청 retry 같은 의미를 바꾸지 않는 기술적 복구는 매번 사용자 확인을 요구하지 않는다. 새 source/engine/방법론이 결과 의미를 바꾸는 선택은 확정한 정책을 적용하고 근거를 남긴다.

## 저비용 방법론 범위

### 초기 필수

초기 필수는 Python 전략 protocol/registry·feature 확장·검증된 framework adapter·일봉 모의체결·품질 등급·시간순 평가다. SMA·가격 모멘텀·재무 개선·RSI2는 사용자가 원하는 연구 스타일을 보여줄 예시이며 네 가지를 제품의 닫힌 전략 목록이나 전체 완료 조건으로 삼지 않는다. 예시 정의는 11 명세를 따르고 새 전략은 같은 계약으로 추가한다.

새 전략 추가 인수는 서로 다른 가격/재무 입력 플러그인과 stateful 결정의 실제 경로, 추가 Python 모듈 등록 후 SQL/engine/UI 공통 코드를 바꾸지 않고 실행하는 extension probe로 확인한다. 손계산 ledger는 framework 회계 검증 oracle이며 생산 엔진으로 고정하지 않는다. rule regime와 동일 비중·현금·상한은 기본 모듈이고 이름만 고급 모델로 표시하지 않는다.

초기에는 회사 재무를 ETF/ETN에 적용하지 않는다. 유료 consensus·실시간 tick·borrow·장중 호가·고객 그래프·고빈도 정책 반응을 기본 전략의 필수 입력으로 만들지 않는다. 전체 상품 대상과 품질 gap은 계속 보존한다.

### 조건부·후속

| 방법 | 초기 역할 | 확장 전 검증 |
|---|---|---|
| engine 후보·vectorbt | 18 비교에 따른 qualification, vectorbt는 선택 연구 도구 | 호환·회계 parity·RSS/time·license·unsupported matrix |
| Parquet/DuckDB | 필요 시 disposable cache | SQL 대비 실제 이득·same identity·삭제 후 재생성 |
| HMM·Kalman·online 변화점 | 초기 구현 제외, interface만 유지 | 별도 후속 요청에서 baseline 대비 기여·train-only/causal·fit 비용 |
| IC·block bootstrap·다중검정 | 후속 연구 평가 | sample/시계열 의존·trial count·통계 가정·계산량 |
| HRP·risk parity·covariance 최적화 | 후속 배분 연구 | 실제 입력·동일 horizon·행렬 규모·추정 오차·비용후 OOS |
| CNN/deep learning·대규모 시나리오·강화학습 | 초기 범위 밖 | 별도 가설·데이터·자원·학습/검증 비용을 먼저 결정 |
| 외부 유료 LLM·GPU·PRO 기능·유료 데이터 의존 방법 | 초기 자동 이용 금지 | 무료 대안·월합계10,000원 이하 제안·사용자 이용 확인 |

“복잡해서 더 정확하다”는 이유만으로 방법을 추가하지 않는다. 동일 입력·기간·비용에서 기본 방법보다 어떤 오류·위험·연구 질문을 개선하는지 정의하고 확인한다. 유료 데이터 부재를 임의의 유사 열로 대체해 고급 방법이 작동했다고 표시하지 않는다.

## 방법론과 기능의 실행 기록

초기 구현 Task01에서 method/feature manifest를 생성한다. 각 항목에 `id`, `mandatory|conditional|deferred`, `definition_hash`, `input_requirements`, `source_cost`, `dependency_cost`, `estimated_calls`, `measured_peak_memory`, `measured_elapsed`, `evaluation_plan`, `capabilities`, `completion_evidence`를 기록한다. 단순 method manifest 자체는 해당 방법이 구현됐다는 증거가 아니다.

개별 실행에는 `requested_configuration`, `resolved_configuration`, `resolution_policy_id`, `resolution_events`, `dataset_origin`, `actual_engine`, `actual_sources`, `quality_status`, `execution_assumptions`, `trial_history`를 남긴다. 원래 의미가 바뀌면 fingerprint도 바뀐다. unknown 비용·지원 여부는 0/true로 채우지 않는다.

구현 완료·입력 준비·운영 연결·현재 계산 결과 상태는 독립 필드로 보고한다. 예: `implementation=verified`, `data_readiness=blocked`, `operation=not_connected`, `result=unavailable`. 이 경우 “입력·운영 연결까지 완료”라고 묶어 보고하지 않는다. 필수 core의 전체 완료는 해당 acceptance 증거를 모두 갖춰야 한다.

## 확정 정책과 구현에서 확보할 증거

DEC-01–17의 정책 답변을 반영했다. 기본 원칙은 사용자 최종 전략 선택·재무 공개 버전 PIT·가격 세션 cutoff·source 자동 전환 금지·유료 이용은 확인 후·수학은 interface만이다. 프레임워크는 문서 비교 후보이며 runtime qualification은 구현 Task에서 수행한다. 연구 탐색량·운영 성능값은 Task01 실측으로 lock하고, 예산·데이터 의미·완료 범위를 몰래 바꾸지 않는다.

전체 연결을 재점검한 DEC-18/19의 답변은 20에 반영했다. DEC-20–23은 추가 질문의 답변 전까지 pending이며 기존 기본안을 적용한다. 매수 풀·보유 관리·레짐 역할·definition bundle·historical/current 판단 일치를 작은 공통 DTO/Python 함수로 구현한다. 확장성을 이유로 범용 workflow·초기 고급 모델·추가 원격 분석 서비스를 자동 도입하지 않는다.

[19 Invest Prototype 검토](19-invest-prototype-lessons.md)는 최근 cache 창의 원본 보관 상한 오용·전체 실패 확대·기업행동 0/1 기본값·현재 모집단의 과거 소급·state 재시작·display 결과 역류를 구체적 사례로 연결한다. 추가 거래가능 경계는 사용자가 연구용 가정 허용으로 답변해 DEC-17로 확정했다.
