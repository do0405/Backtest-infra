# 시장 레짐과 종목 전략 스크리닝 PRD

한국·미국 시장 상태를 각각 추세와 변동성으로 설명하고, 현재 조건에 맞는 종목·전략 후보를 찾는다. 레짐은 관측된 상태 분류이며 향후 수익을 보장하는 예측 문구로 사용하지 않는다.

## 레짐 요구

| ID | 요구 | 완료 조건 |
|---|---|---|
| R01 | 시장별 상태 | KR/US 별 benchmark·세션·freshness와 상태 제공 |
| R02 | 초기 단순 분류 | 추세×변동성 네 상태 + unknown, 근거 숫자·정의 버전 |
| R03 | 인과적 시계열 | t 상태는 t까지 정보만, 미래 데이터 추가해도 이전 상태 불변 |
| R04 | 전환 안정화 | candidate·confirmed·확정 시각·persistence 구분 |
| R05 | 상태 이력 | release·모델·feature 입력·cutoff·계산 시각 저장 |
| R06 | 모델 비교 | 단순 기준 대비 시간순 OOS·상태 안정성·전략 영향 |

초기는 검증된 시장 대표 ETF proxy의 split-adjusted close, SMA200, 20세션 실현 변동성을 사용한다. 공식 KOSPI/S&P500 자료가 없을 때 해당 이름의 지수 상태로 위장하지 않는다. 실제 benchmark security와 가격 범위를 표시한다. 역사 breadth는 당시 constituent·universe가 확보된 이후 별도 feature로 추가한다.

분류는 `up_low_vol`, `up_high_vol`, `down_low_vol`, `down_high_vol`, `unknown`이다. 초기 rule_regime_v1 threshold는 11 명세의 t−1까지 rolling 756세션 80th percentile로 고정하고, 전체 역사로 fit하지 않는다. 훈련 구간 고정 등 다른 규칙은 별도 version으로만 추가한다. 3세션 persistence는 세 번째 날부터 확정하며 앞선 날로 소급하지 않는다. 충분한 입력이 없으면 unknown이다.

## 두 종류의 스크리닝

| ID | 대상 | 출력·완료 조건 |
|---|---|---|
| C01 | 종목 스크리너 | 시장·상품·유동성·거래상태·가격 모멘텀·재무 개선 rank·제외 사유 |
| C02 | 전략 스크리너 | 전략/파라미터/버전별 OOS·낙폭·비용·거래수·레짐 성과 |
| C03 | 시점 일치 | 같은 cutoff·release·universe·feature definitions로 batch 계산 |
| C04 | rank 재현 | eligible count·결측·동점 tie-break·가중치·score lineage 저장 |
| C05 | 저장 필터 | versioned screener config, 수정 시 새 버전 생성 |
| C06 | drill-down | rank→차트·재무·백테스트·제외 근거로 연결 |
| C07 | 안전한 결과 활용 | 단순 순위·research/selected·data quality·validation·신호 결과를 구분 |

### 종목 스크리너

전체 대상에서 거래 가능·상품 적용성·필수 입력 적격성으로 **실행 eligible 집합**을 만들되 제외 집합과 이유도 저장한다. ETF/ETN을 회사 재무가 없다는 이유로 전체 target에서 제거하지 않는다. 회사 재무 전략에만 not_applicable 표시를 붙인다.

가격 모멘텀·재무 개선 모멘텀·평균회귀 상태를 별도 열로 보여준다. 기본은 세 점수를 하나로 합치지 않는다. 복합 스크리너에서는 가중치와 결측 처리 정책을 명시한다. 동점은 안정적인 security_id로 정렬하고 회사명·현재 ticker 변경으로 순위가 달라지지 않게 한다.

DEC-19에 따라 전략별 풀이 진입 판단 풀이다. 해당 전략의 Python selector를 historical/current에서 같은 정의·schedule·cutoff로 실행하고 스크리너는 그 결과를 보여준다. 이미 보유한 종목의 매도/유지는 진입 풀 밖에서도 관리하며, 후보 제외 자체를 자동 청산으로 해석하지 않는다. 여러 전략 합집합 화면도 각 pool membership·기준일을 보존한다. [20 연결 계약](20-pipeline-and-extension-contracts.md)에 같은 판단 문맥·레짐 역할·확장 위치를 명시한다.

### 전략 스크리너

등록된 Python 전략 결과를 같은 시장·기간·통화·비용·benchmark 조건 안에서 비교한다. 초기 스타일 예시에 제한하지 않는다. 최종 검증 구간·하위 구간·낙폭·거래수·레짐 표본·비용 성과와 입력 품질을 제공한다.

모든 새 전략은 `selection_state=research`로 시작한다. 사용자 최종 선택만 `selected`로 바꾸고 `retired`를 별도 기록한다. `validation_state`·data_quality·평가 scope는 별도 필드이며 선택했다고 검증 완료로 바꾸지 않는다. 자동 승격은 없다. 연구용 신호도 별도 표시할 수 있고 selected 전략의 필수 입력이 부족하면 계산은 blocked다.

## 수학적 확장 경로

이번 구현은 Feature/Model/Strategy protocol·시간/단위·artifact·capability를 확장할 자리만 만든다. HMM·Kalman·online 변화점·고급 최적화 등을 실제 연결하거나 임시 이름으로 노출하지 않는다. 이 방법을 추가하려면 별도 요청에서 데이터·비용·train-only/causal·baseline 비교 계약을 먼저 고정한다. 미래 smoothing을 과거 판단에 넣는 확장은 허용하지 않는다.

팩터 평가의 rank IC·ICIR·block bootstrap·multiple testing, covariance shrinkage·HRP·risk parity는 후속 별도 작업이다. 현재부터 모든 방법을 의존성에 넣지 않고 공통 feature/model 계약으로 추가 가능성을 확보한다.

거시 feature는 관측 기간과 발표·수정 vintage를 분리한다. 최신 FRED 값만 과거에 넣는 방식은 허용하지 않는다. [FRED API](https://fred.stlouisfed.org/docs/api/fred/)는 후속 공급자 후보이며 초기 레짐의 필수 조건은 아니다.

초기 rule regime는 HMM·학습된 확률·AI confidence로 표시하지 않는다. 고급 방법은 비용·데이터·검증 부담과 baseline 대비 개선 가설을 [17 계약](17-implementation-guardrails.md)에 기록한 뒤 후속으로 선택한다. 방법 이름만 붙인 임시 상태나 상수 confidence를 운영 결과로 반환하지 않는다.
