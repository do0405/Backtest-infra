# 백테스트와 전략 실험 PRD

Python 전략을 계속 추가하고 로컬에서 반복 평가하는 플랫폼이다. 전략 정의를 스크리닝·시그널에 공유한다. 가격 추세·가격 모멘텀·재무 개선·평균회귀는 초기 연구 스타일의 예시이며 고정 네 전략 목록·전체 완료 기준이 아니다.

## 요구

| ID | 요구 | 완료 조건 |
|---|---|---|
| B01 | 고정 데이터와 universe | release·scope·history mode·시점별 membership·quality gap 고정 |
| B02 | 미래 정보 차단 | t 폐장 후 신호는 eligible t+1 시가부터, 재무는 공개 이후 |
| B03 | 현실적 회계 | 수수료·spread·slippage·세금·정수 주식·현금·미체결·배당·분할 |
| B04 | 생존편향 표시 | 상폐·정지·티커 변경·미해결 terminal event 반영 |
| B05 | 결과 재현 | 입력·코드·의존성·비용·seed·engine fingerprint와 ledger 저장 |
| B06 | 시간순 평가 | train·validation·holdout·walk-forward 분리, 선택 이력 저장 |
| B07 | 전략 비교 | 같은 scope·통화·기간·비용·benchmark의 표 비교 |
| B08 | 빠른 parameter 연구 | 작은 grid·청크·RAM 예산·중단·재개, trial count 노출 |
| B09 | 확장 가능한 framework adapter | 18 후보 qualification·손계산 oracle 대조·Python 전략 extension probe, 선택 engine/실제 version 표시 |
| B10 | 부분 결과의 한계 | excluded universe·PIT/정정 한계·적격성 flag를 화면과 export에 유지 |

## 사용자 흐름

사용자는 전략 템플릿과 시장·기간·universe·비용을 선택한다. 시스템은 실행 전 입력 coverage와 필요한 profile을 검사해 가능·부분 연구·차단 상태를 보여준다. 실행은 비동기 job으로 처리하고 진행·남은 구간·메모리·취소 상태를 표시한다. 결과는 누적수익·낙폭·월별 성과·거래 원장·비용·보유 비중·레짐별 성과·검증 구간·gap으로 나뉜다.

## 전략 템플릿

아래는 시작용 예시다. 새 Python 전략은 StrategySpec/protocol에 입력·warmup·schedule·state·출력·data policy·engine capability를 선언하고 등록한다. 엔진/SQL/UI 공통 코드 수정 없이 추가되는 실제 extension test를 인수한다. 화면의 조건 조합 builder는 초기 범위에 없다.

| 템플릿 | 입력 | 초기 규칙 후보 | 필수 gate |
|---|---|---|---|
| trend_sma_v1 | split-adjusted close, raw next open | SMA50>SMA200 진입 상태, 반대면 청산 | 200세션 warmup·기업행동·체결가능 |
| price_momentum_v1 | split-adjusted close·유동성 | 12−1개월 가격 모멘텀 상위, 월별 리밸런싱 | 시점별 universe·조정 기준·순위 eligible 집합 |
| fundamental_improvement_v1 | 공개된 fiscal 재무·가격 | 매출 YoY·영업마진 개선 rank를 0.5/0.5 합산; 흑자전환·CFO는 설명 열 | 공개시각·회계기간·scope·비교 기준 |
| mean_reversion_rsi2_v1 | split-adjusted close | RSI2<5 진입, RSI2>50 또는 5세션 경과 청산 | gap 없는 계산·거래가능·비용·corporate action |

기간·임계값은 구현·회계 검증용 시작 후보다. 성과를 보고 사후 최적값처럼 표현하지 않는다. 레짐 필터는 전략별 옵션이고 기본 결과와 필터 적용 결과를 함께 비교한다. 사용자 승인 없는 숨은 전략 전환이나 파라미터 자동 변경은 없다.

## 실행 모드

기본 검증 모드는 재무의 `observed_system`/`reconstructed_public` 계약과 가격의 `historical_price_session_v1`을 조합한다. 가격은 당시 거래 세션까지만 읽고 늦은 수신 자체로 과거 계산을 막지 않는다. 재무는 공개·정정 버전 cutoff를 지킨다. 기존 release_query_v5의 clock 의미를 바꾸지 않고 신규 분석용 가격 reader/policy로 분리한다.

사용자는 후속 DEC-01에서 미검증 자료의 탐색 결과도 별도 표시하기로 선택했다. 기본 `strict_pit`는 유지하고 `exploratory_snapshot`을 별도의 명시 연구 dataset 경로로 추가한다. 이는 기존 QueryContext의 history_mode에 값을 추가하거나 `research_partial`로 clock을 우회하는 구현이 아니다. 허용하는 미검증 항목·입력 고정 시점·가정·품질 gap을 manifest와 화면/export에 보존한다.

탐색 경로의 구조·identity·무결성과 미래 봉/학습 차단은 필수다. 재무 공개 시각·정정 버전은 탐색에서도 PIT 필수다. 공급자 수정주가 탐색은 DEC-11의 조정 기준·기업행동 미검증을 보존한다. 미래 기업행동으로 과거 절대 가격/특징을 바꾸는 것으로 확인된 입력은 탐색에서도 과거 전략 판단에 넣지 않는다. 조정 이력이 미확보라는 이유와 확인된 미래 정보 유입은 서로 다른 상태다. 선택된 전략 상태와 데이터/성과 validation은 독립 기록한다.

DEC-02에 따라 실제 호가·경매 수량이 없어도 versioned `modeled_next_open_v1` 계약의 일봉 모의계산을 제공한다. 검증 여부는 data_quality, 체결 가정은 execution_assumptions로 각각 표시한다. 시가·단위·비용·현금·정수 주식·알려진 정지·누락 open·지원 action 제약을 적용하고 실제 체결 재현으로 부르지 않는다. 모의체결 허용은 가격이나 기업행동 입력을 지어내는 허용이 아니다.

DEC-17의 `research_daily_tradability_v1`은 공식 거래가능 이력이 부족해도 유효한 raw OHLCV·양수 거래량의 체결 세션에서 연구용 시가 모의체결을 허용한다. observed 상태는 unknown으로 유지하고 가정을 별도 기록한다. 확인된 정지·알려진 미체결 제약·결측 시가·잘못된 action은 차단한다. 체결 세션 거래량은 사후 모의체결 조건이며 과거 signal·sizing에 미리 쓰지 않는다. 이 결과의 data quality·체결 가정을 strict 검증 등급과 합치지 않는다.

데이터 불완전으로 작동하는 전략이 줄더라도 전체 target을 축소하지 않는다. run은 실제 eligible 집합과 제외 이유를 고정해 분모를 보존한다.

## 프레임워크 선택과 검증

[18 프레임워크 비교](18-framework-evaluation.md)의 첫 후보는 Zipline-reloaded, 두 번째는 LEAN 오픈소스 엔진이다. 실제 Windows·Python·own SQL data·KR/US calendar·기업행동·자원 qualification을 통과한 framework를 기본으로 lock한다. vectorbt는 선택 연구 도구다. 유료 LEAN CLI나 데이터 구매는 기본 이용 경로에 넣지 않는다.

vectorbt는 신호/비중 배열을 빠르게 평가하는 어댑터 후보다. vendor downloader를 앱 데이터 경로에 넣지 않고 고정 SQL projection만 전달한다. 버전별 비용·cash sharing·call sequence·정수 단위·기업행동 구현 차이를 fixture로 확인한다. 지원하지 않는 회계는 feature flag로 차단한다.

현재 라이선스는 Apache 2.0에 Commons Clause 조건이 포함되어 있으므로 개인 로컬 연구와 향후 유료 서비스 제공을 동일하게 취급하지 않는다. 상용화 시 별도 검토가 필요하다. PRO는 필수 의존성이 아니다. [프로젝트 라이선스](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md).

작은 deterministic 손계산 ledger는 framework cash·quantity·PnL·fee·분할·배당 검증의 oracle로만 둔다. 생산 엔진을 자체 구현으로 고정하지 않는다. 선택 framework가 지원하지 않는 권리·합병·상폐 대가는 capability와 정책에 따라 명시 unsupported로 처리한다.

필수는 전략 확장 protocol과 qualification된 생산 framework 연결이다. 선택 engine의 실패를 다른 engine로 숨기는 전환은 금지한다. 후보가 모두 실패하면 이유·추가 비용·대안을 제시하고 선정 미완료로 남긴다. 실제 source→계산→SQL 저장→API 결과 연결과 재시작 증거를 인수한다.

## 결과 지표

순수익·CAGR·연율 변동성·최대낙폭·Sharpe/Sortino 가정·Calmar·거래수·승률·평균 보유·turnover·노출·비용·현금·benchmark 차이를 제공한다. 통화·annualization·risk-free rate·누락 세션·warmup·경계 평가 규칙은 [분석 계약](11-analytics-spec.md)에 고정한다.

최적화 화면은 전체 trial·실패·범위와 OOS 구간을 함께 보여준다. 우수한 전체기간 수익률만으로 검증 완료를 인증하거나 전략을 자동 선택하지 않는다. 가격만 있는 상품과 회사 재무가 필요한 전략의 applicability를 구분한다.
