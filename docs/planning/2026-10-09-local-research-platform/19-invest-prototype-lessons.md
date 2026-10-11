# Invest Prototype 검토와 재발 방지

기준일 2026-10-09 KST. `E:/side project/invest-prototype-main`의 현재 문서·코드·회귀 테스트 정의와 과거 문제 기록을 읽기 전용으로 검토했다. 전체 코드 감사·신규 runtime 실행·현재 운영 결과 인증은 수행하지 않았다. 이전 문제의 기록, 현재 유지되는 정책, 이번 플랫폼에 적용할 설계 판단을 구분한다. Invest Prototype의 코드·데이터를 가져오거나 두 프로젝트를 연결하는 명세가 아니다.

## 추가 확정이 필요한 범위

핵심 DEC-01–16은 유지한다. 연구 결과 제공, 재무 PIT, 가격 세션 기준, Python 전략 확장, 사용자 최종 선택, source 고정, 비용 한도, 고급 수학 연결 제외를 다시 질문하지 않는다. 추가로 질문한 연구용 거래가능 경계는 사용자가 “가능으로하면 좋을듯”이라고 답변해 DEC-17로 확정했다. 구현 호환성·공급자 실제 coverage·성능은 사용자 취향 질문으로 대신할 수 없으며 실측할 사항이다.

| ID | 추가 선택 | 상태 | 선택지와 영향 |
|---|---|---|---|
| DEC-17 | 과거 공식 거래가능 이력 부족 시 연구용 체결 가정 | confirmed | 유효한 원주가 OHLCV·양수 거래량의 체결 세션은 연구용 익일 시가 가정을 허용. 정확한 시가 체결·미발견 정지를 인증하지 않고 research_daily_tradability_v1로 기록한다 |

`research_daily_tradability_v1`은 연구용 execution policy다. 유효한 원주가 OHLCV·양수 거래량과 calendar/listing·lot/cash·비용·지원 action 조건을 확인하고, 알려진 정지·상하한가의 미체결 제약이 없을 때 익일 시가 모의체결을 허용한다. unknown 관측은 그대로 두고 가정을 별도 기록한다. 연구용 허용은 미래 거래량을 신호·주문 크기에 미리 쓰거나, 확인된 정지·결측 시가·미지원 기업행동·잘못된 숫자를 정상으로 처리하는 허용이 아니다. 체결 세션의 일일 거래량은 사후 조건이므로 `ex_post_execution_assumption`으로 기록하고 의사결정 입력과 구분한다. 실제 호가·경매 수량이나 시가 체결 보장은 주장하지 않는다. 사용자의 selected 상태는 이 가정을 실제 거래가능 증거로 바꾸지 않는다. T49로 연구/검증 경계와 미래 입력 차단을 인수한다.

## 확인한 사례와 이번 예방 조건

### IP01 보관 창과 연구 원본의 혼동

현재 가격 정책은 최신 756봉, 활성 재무 CSV는 최신 8분기다. 코드에도 US/KR 가격 상한과 `MAX_QUARTERS = 8`이 있다. 이는 기존 최신 분석의 명시 정책이며, 그 자체를 잘못된 구현이라고 판정하지 않는다. 그러나 이번의 10년 이상 가격·PIT 연구 저장소에 복사하면 목표를 충족할 수 없다.

근거: [기존 README 데이터 계약](../../../../invest-prototype-main/README.md), [US 상한](../../../../invest-prototype-main/data_collector.py), [KR 창 절단](../../../../invest-prototype-main/data_collectors/kr_ohlcv_collector.py), [재무 CSV 창](../../../../invest-prototype-main/screeners/financial_screening/csv_store.py).

이번에는 PostgreSQL의 원관측·재무 버전·release evidence를 기준 이력으로 보존한다. 최근 구간 cache·UI 조회 창·feature warmup은 읽는 범위이며 원본 삭제 기준이 아니다. 수집에서 757번째 가격이나 9번째 분기가 들어와도 이전 역사와 공시 버전이 남아야 한다. 압축·index·partition·disposable cache로 비용을 줄이고 원본 기간 축소로 메모리 문제를 해결하지 않는다. 기존 원문 문서 보관 범위는 별도 원래 정책을 유지하며, 이 조건 때문에 모든 문서를 신규 영구 저장하도록 확대하지 않는다. Task04/05/06/20, T43으로 인수한다.

### IP02 일부 오류를 전체 실패로 확대

2026-09-17 기록에는 US 331개 target-missing 파일 중 330개가 과거의 다른 잘못된 행 때문에 거절된 사례와 KR overlap 충돌 때문에 새 세션 적재가 막힌 사례가 있다. 2026-09-21 기록에는 정상 단계·부분 게시가 있어도 일부 stale/빈 파일 때문에 시장 전체가 반복 실패한 사례가 있다. 현재 코드는 행 검증·정정 upsert·계산 종목 allowlist·부분 완료로 보완했고 관련 회귀 테스트가 존재한다. 이번 검토에서 테스트를 새로 실행했다는 뜻은 아니다.

근거: [정정 upsert 문제와 설계](../../../../invest-prototype-main/docs/superpowers/specs/2026-09-17-ohlcv-current-revision-upsert-design.md), [부분 수집 완료 문제와 설계](../../../../invest-prototype-main/docs/superpowers/specs/2026-09-21-partial-acquisition-completion-design.md), [US 회귀 정의](../../../../invest-prototype-main/tests/test_us_ohlcv_storage_contract.py), [KR 회귀 정의](../../../../invest-prototype-main/tests/test_kr_ohlcv_collector.py).

이번에는 가격의 잘못된 행과 영향받는 feature 구간·종목을 격리하고, 적격 다른 종목·기간의 계산은 계속한다. 단순 제외로 rolling window를 당겨 결측을 지우지 않는다. 해당 전략의 연속 세션 입력이 부족하면 그 계산을 차단한다. 재무는 공시/context/단위·기간 의존성의 최소 안전 범위로 격리한다. 시장 identity·schema·hash·원본 무결성 실패는 row-local 오류로 내려서 성공 처리하지 않는다. 최신 가격 정정은 같은 source의 신규 관측으로 보존하고 새 release의 frozen revision에서 선택하며, 이전 SQL 관측·봉인 결과를 덮어쓰지 않는다. Task04/06/07/20, T44로 인수한다.

### IP03 성공의 의미와 재시도의 범위

기존 보완은 isolated 종목 문제와 공급자 전체 실패를 구분하고 CLI·supervisor가 같은 완료 계약을 읽도록 한다. 2026-09-25 설계는 복구된 traceback 문자열 자체와 실제 실패 상태도 분리한다. 이 구분은 이번에도 필요하다.

근거: [부분 완료 계약](../../../../invest-prototype-main/docs/superpowers/specs/2026-09-21-partial-acquisition-completion-design.md), [구조적 실패와 로그의 구분](../../../../invest-prototype-main/docs/superpowers/specs/2026-09-25-bounded-e2e-gates-design.md), [SEC 복구의 source completion/coverage 구분](../../../../invest-prototype-main/docs/runtime/sec-official-disclosure-recovery.md).

`job_status`, `data_quality`, `coverage`, `result_status`를 독립 표시한다. 작업이 정상 종료한 partial 결과도 전수 clean 완료로 인증하지 않는다. 부족 종목·날짜·공시 identity를 복구 큐에 남기고 성공한 전체 작업을 매번 재실행하지 않는다. CLI·worker·UI가 같은 구조화 결과를 해석한다. source 순회 완료·HTTP200·SQL insert 증가만으로 필수 재무/가격 coverage를 인증하지 않는다. 응답 소실·429·transport에는 상한 있는 재시도·checkpoint를 적용하고, invalid hash/시장 identity에는 같은 요청을 무한 반복하지 않는다. Task07/17/20, T45로 인수한다.

### IP04 제공되지 않은 기업행동을 0으로 정규화

현재 KR 정규화에는 누락된 dividends/splits를 0, split_factor를 1, adj_close를 close로 채우는 경로가 있고 테스트도 그 호환 출력을 기대한다. 이 값만으로 실제 기업행동이 없었다는 뜻은 입증되지 않는다. 모든 기존 소비자가 이를 검증 완료로 오용한다고 단정하지 않는다.

근거: [KR 정규화 코드](../../../../invest-prototype-main/data_collectors/kr_ohlcv_collector.py), [기본값을 기대하는 테스트](../../../../invest-prototype-main/tests/test_kr_ohlcv_collector.py).

이번 SQL에서는 `not_reported`, `unknown`, `reported_zero`, `verified_no_event`를 구분한다. action 필드가 없는 응답의 raw close를 표시할 수는 있지만 검증된 adjusted close로 인증하지 않는다. 허용된 supplier-defined 계열 탐색은 원 basis·한계를 표시한다. 없는 사건을 숫자로 만들어 거래 원장에 넣지 않는다. 03/10/17의 기존 계약과 T09/T16/T38을 적용한다.

### IP05 오늘의 종목 목록을 과거 모집단으로 사용

기존 AGENTS는 historical universe가 current metadata 기반이며 생존편향 제한을 갖는다고 명시한다. 원시 가격 이력이 길다는 사실만으로 역사 모집단을 확보한 것은 아니다.

근거: [기존 시장 레짐·universe 한계](../../../../invest-prototype-main/AGENTS.md), [현재 metadata history 경계](../../../../invest-prototype-main/docs/runtime/canonical-source-map.md).

이번 strict 역사 계산은 시점별 security/listing membership을 요구한다. 미확보 구간의 current-list-only 탐색은 `universe_basis=current_membership`, `survivorship_bias=unresolved`를 표시하고 역사 전체 인증에서 제외한다. 이를 전체 target으로 바꾸거나 당시 상폐 종목이 없었다고 해석하지 않는다. 이미 허용된 미검증 탐색의 구체적 품질 표시이며 미래 재무를 허용하는 새로운 waiver가 아니다. Task03/06/20, T46으로 인수한다.

### IP06 계산 창의 이동과 누적 상태의 초기화

2026-09-07 감사는 보관 정리기가 파일을 보호해도 모델 생산자가 새 창에서 초기화하면 누적 상태가 보존되지 않는다고 재현했다. 현재 momentum fact 코드에도 756세션 shadow 창이 존재한다. 해당 감사의 모델 상태 문제를 오늘 모두 해결됐거나 미해결이라고 재인증하지 않는다.

근거: [보관·누적 상태 감사](../../../../invest-prototype-main/docs/audits/2026-09-07-market-observation-history-retention-audit.md), [현재 bounded fact history](../../../../invest-prototype-main/market_observations/momentum_facts.py).

이번은 고급 모델을 연결하지 않는다. 다만 초기 stateful Python 전략과 기본 레짐부터 definition/config/dataset·마지막 세션·pending transition·보유 상태의 checkpoint를 소유한다. 같은 입력의 순차 전체 재생과 chunk·재시작 실행이 같아야 한다. 계산 창이 이동할 때 state를 초기값으로 바꾸지 않는다. 과거 가격 정정은 새 release에서 필요한 state를 무효화/재생하되 과거 고정 run을 바꾸지 않는다. Task11/15/19/20, T47로 인수한다.

### IP07 과거 성공 결과의 표시와 오늘의 입력

기존 최신 성공 표시 계층은 source session과 current attempt를 따로 보존하고 display-only 결과가 계산/PIT 입력으로 역류하지 않도록 한다. 이 점은 유지할 좋은 경계다.

근거: [현재 display 계층](../../../../invest-prototype-main/orchestrator/latest_display.py), [현재 dashboard/authority 경계](../../../../invest-prototype-main/docs/runtime/canonical-source-map.md).

이번도 과거 차트·백테스트는 조회할 수 있다. 그러나 이전 신호를 오늘의 active 신호로 재표시하거나 source date를 오늘로 덮어쓰지 않는다. 캐시는 같은 frozen dataset identity에서만 재생성한다. frontend가 다른 release·기간·engine의 결과를 빈 응답 대신 넣지 않는다. Task16/17/18/20, T48로 인수한다.

## 구현 순서의 보완

한 요청으로 실행하되 중간 증거가 있는 순서로 완성한다. 먼저 한국·미국의 작은 고정 실데이터로 SQL→가격 세션 조회→API→차트를 연결하고, framework/Python plugin의 작은 회계 probe를 검증한다. 다음에 screening·stateful signal·저장/재시작을 연결하고, 그 경로를 그대로 전수/장기간/원격으로 확대한다. 작은 검증 표본은 전체 제품 대상·기간을 줄이는 것이 아니다. 단계 종료마다 매번 사용자 승인을 요구하지 않으며 이미 확정한 범위의 작업은 계속 진행한다.

engine 설치·own-data 최소 실행은 Task01에서 위험을 확인하고, Task13에서 회계/달력/상품 qualification을 확정한다. 거대한 cache·전수 feature 계산을 만든 뒤 engine이 입력을 받을 수 없음을 발견하는 순서를 피한다. framework 미선정 자체를 테스트 손계산 엔진의 생산 채택으로 해결하지 않는다.

runtime에는 수집/원격 실행 owner, 분석 worker, 로컬 API/UI의 역할을 명시한다. UI의 브라우저 새로고침이 수집·무거운 계산을 직접 실행하지 않는다. 새 wrapper·일회 runner·별도 상태 저장소로 현재 오류를 우회하지 않고 canonical owner를 수정한다. Invest Prototype의 supervisor·CSV 원본 정책을 이번 SQL 플랫폼의 의존성으로 추가하지 않는다.

## 재발 방지 인수 연결

| 사례 | 기존 계약 | 추가 인수 | 구현 연결 |
|---|---|---|---|
| IP01 보관 창 | G04/G14/G15, D03/D06 | T43 | Task04/05/06/20 |
| IP02 최소 범위 격리·정정 | G04/G08/G14, B02 | T44 | Task04/06/07/20 |
| IP03 부분 완료·복구 범위 | G08/G16/G17/G20 | T45 | Task07/17/20 |
| IP04 action 기본값 | G04/G12, D04 | T09/T16/T38 | Task04/06/12/13 |
| IP05 과거 모집단 | G03/G14, D01/B04 | T46 | Task03/06/20 |
| IP06 상태 재시작 | G13/G14, B05/R03 | T47 | Task11/15/19/20 |
| IP07 오래된 표시 | G01/G06/G08, S02/S06 | T48 | Task16/17/18/20 |
| DEC-17 연구용 거래가능 가정 | B02/B03/S02/G03/G04/G14 | T49 | Task06/12/13/16/20 |

이 문서는 재발 방지 설계다. 기존 문제의 발생 숫자는 당시 기록이며 오늘의 운영 수치가 아니다. 코드/테스트 존재는 회귀 의도와 현재 정책의 증거이고 새 실행 통과를 대신하지 않는다.
