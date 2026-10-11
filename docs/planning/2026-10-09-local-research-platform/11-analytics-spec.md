# 특징 전략 체결 레짐 평가 계산 명세

## 공통 계산 시점

전략 cutoff는 market calendar의 세션 폐장과 source finalization 뒤다. 가격 feature는 경제 세션≤cutoff로 제한하고 늦은 수신 자체는 제외 조건으로 쓰지 않는다. 재무 feature는 해당 cutoff에 공개된 정확한 버전만 사용한다. 미래 봉·fit·정정 재무의 과거 유입은 금지한다. desired target은 다음 eligible 세션 시가부터 모의체결한다.

전략은 `FeatureView + PortfolioView + PreviousStrategyState + StrategyConfig`를 받아 `StrategyDecision + ReasonCodes + InputRefs + NewState`를 반환한다. state/보유 feedback은 명시 DTO이며 broker 주문 전송은 없다. 백테스트·스크리닝·현재 신호는 같은 plugin definition을 사용한다.

## 특징 정의

| 특징 | 정의 | 결측·시점 규칙 |
|---|---|---|
| sma_n | 최근 n eligible 세션의 split-adjusted close 산술평균 | n개 가격·action coverage 필수 |
| price_mom_12_1 | P[t−21]/P[t−252]−1 | split-adjusted **가격** 모멘텀, 배당 제외를 표시 |
| realized_vol_20 | std(log(Pt/Pt−1), ddof=1)×sqrt(252) | 20 returns, gap·split coverage 확인 |
| rsi_2_wilder | gain/loss 최초 2변화 평균 후 Wilder α=1/2 | loss=0/gain>0→100, 둘 다 0→50; 정책 고정 |
| adv20 | 직전 20 eligible 세션의 동일 scope 거래대금 평균 | 미보고·scope 충돌은 unavailable; VWAP와 구분 |
| revenue_yoy | compatible fiscal Q 현재/전년−1 | 전년>0, 회계/scope/통화 일치 |
| operating_margin_delta_yoy | op_income/revenue 현재−전년 동기 | revenue>0·compatible fiscal, 단위 %p |
| profit_turnaround | 비교 순이익≤0 및 현재>0 여부 | same definition·scope, 성장률과 별도 flag |
| cfo_ttm_delta_yoy | compatible 4Q CFO 현재−전년 | 비율은 전년>0일 때 별도 산출 |

price momentum은 252세션을 12개월의 근사로 사용하는 정의다. 달력 12개월·정확 월말 momentum과 동일하게 표시하지 않는다. 별도 정의가 필요하면 새 version을 만든다. total-return momentum은 분배금·기업행동 적격성을 갖춘 별도 feature다.

재무 feature는 최초 유효 공시 이후 step function으로 이용할 수 있다. 미래 값으로 보간하지 않는다. 최신 값 유지에는 filing 유형별 age policy와 정정 discovery coverage를 적용한다. 회사 재무가 적용되지 않는 ETF/ETN은 not_applicable이며 가격 전략에서 제외 이유가 되지 않는다.

상장 전은 계산 기간에 포함하지 않고 충분한 warmup 이후부터 eligible이다. 세션 결측은 trading halt·no trade·missing source를 구분하며 휴일을 0 return으로 삽입하지 않는다. stale mark는 회계 평가에 사용할 수 있어도 fill price로 쓰지 않는다.

## 전략 확장 인수를 위한 예시 규칙

다음 규칙은 예시이며 고정 네 전략 제품이 아니다. 새 Python 전략이 자신의 feature·schedule·state·목표 비중 또는 signal-only·sizing/data policy를 선언하도록 09 protocol을 구현한다. 변경 없이 새 플러그인을 등록하는 probe와 stateful 결정/동일 definition 사용을 인수한다.

### 가격 추세

`trend_sma_v1`: 두 SMA가 적격일 때 SMA50>SMA200이면 long desired state, 그 외 cash state다. 이전 state와 desired state가 다르면 event를 생성한다. initial state·포지션 초기화·crossing 전후 규칙을 config로 고정한다. 개별 종목 전략과 다종목 target weights는 같은 날 cash 제약을 별도로 적용한다.

### 가격 모멘텀

`price_momentum_v1`: 각 시장의 **해당 월 마지막 거래 세션** 폐장에 적격 universe의 price_mom_12_1을 계산한다. 양수 score의 상위 20종목 후보를 동일 비중으로 선택한다. 20개 미만이면 확보된 종목만 각 5% 후보 비중을 갖고 나머지는 cash다. 상위 수와 비중은 config이며 시장·상품별 리밸런싱을 독립한다. 다음 eligible 세션 시가에 매도 우선, 매수 stable security_id 순서로 처리한다.

### 재무 개선 모멘텀

`fundamental_improvement_v1`: revenue_yoy와 operating_margin_delta_yoy의 시장 내 percentile rank를 0.5/0.5로 합산한다. 두 지표가 모두 적격이고 revenue_yoy>0 및 margin delta>0일 때만 후보다. 흑자전환·CFO 개선은 초기 explanatory column이고 임의 bonus를 주지 않는다. 별도 가중치는 config version 변경 후 검증한다.

월 마지막 거래 세션에 상위 20개×5% 후보 비중, 나머지 cash 정책을 사용한다. 재무 공개가 늦은 종목은 과거 분기 종료에 소급 포함하지 않는다. 현재 share/밸류에이션 정보가 없더라도 이 전략의 두 필수 feature가 적격이면 연구 가능하지만 거래 시그널은 추가 current tradability gate를 통과해야 한다.

### 평균회귀

`mean_reversion_rsi2_v1`: 미보유이고 RSI2<5일 때 desired long, 보유 중 RSI2>50 또는 모의 체결 이후 5 eligible 세션 경과면 cash다. 갭·새 상장·정지 시 feature/exit는 unknown 또는 pending으로 분리한다. 5세션은 휴일이 아닌 시장 eligible session으로 세고 실제 미체결 기간을 보유기간으로 세지 않는다.

진입이 실제 모의 체결된 eligible 세션의 종가를 보유 1세션으로 세고, 다섯 번째 eligible 세션 종가에서 시간 청산을 결정해 다음 eligible 세션 시가에 청산을 시도한다. 미체결 주문은 보유기간을 시작하지 않고, 청산 미체결은 포지션과 pending exit를 유지한다. RSI 청산 조건이 먼저 충족되면 그 다음 eligible 세션 시가에 청산을 시도한다.

SMA200 또는 레짐 필터는 선택 parameter다. 기본 전략에 숨겨서 추가하지 않는다. 추세·모멘텀과 평균회귀는 독립 run과 signal을 만들고 결합 포트폴리오는 후속 명시 조합 config로만 만든다.

## 선택 framework에 요구하는 하루 회계 순서

1. calendar·security lifecycle·tradability·해당 세션 corporate actions를 읽는다.
2. 분할 효력 개장 전에 이전 보유 수량×ratio, 단가/주식 basis÷ratio를 반영한다. reverse split의 fractional remainder는 실제 terms가 있으면 처리하고 없으면 unsupported로 차단한다.
3. 전 세션 보유 기준 ex-date 배당 entitlement를 receivable로 기록한다. 실제 pay-date에 cash로 이동한다. ex-date 또는 지급 근거 부족 시 선택한 보수적 모델과 한계를 표시한다.
4. 이전 cutoff에서 만든 target을 해당 raw 시가 기준으로 실행한다. 공식 정지·시가 결측·지원하지 않는 action이면 미체결/blocked다. 거래가능 관측의 unknown은 검증 경로에서 차단하고 명시 연구 경로에서는 아래 DEC-17 조건을 적용한다.
5. 매도 후 cash를 확보하고 매수를 stable security_id 순으로 실행한다. 정수 주식·현금·lot size·최대 비중·유동성 cap을 적용한다. 자금 부족은 fill을 축소하고 이유를 남긴다.
6. raw 종가로 positions+cash+receivables의 NAV를 mark한다. 결측 mark의 carrying rule과 unresolved event를 명시한다.
7. 해당 폐장 이후 known feature로 다음 세션 desired target을 계산한다.

확인된 정지·상하한가의 미체결 제약은 동일 OHLC가 있어도 자동 fill하지 않는다. 공식 이력이 미확보인 경우는 연구 가정과 검증 경로를 분리한다. 일봉만으로 장중 stop·limit 순서를 정확히 알 수 없으므로 초기 market-at-open만 지원한다. intraday stop·limit가 추가되면 별도 ambiguity policy가 필요하다.

`modeled_next_open_v1`은 DEC-02에 따라 raw 시가·알려진 거래상태·지원 action·lot/cash·비용을 쓰는 일봉 체결 가정이다. 실제 호가/경매 수량 확인을 초기 필수로 요구하지 않고, 사전 알려진 adv20 cap과 참여 한계를 명시한다. unknown 관측·상폐 대가·unsupported action을 모의계산이라는 이유로 0·정상으로 바꾸지 않는다.

DEC-17의 `research_daily_tradability_v1`은 공식 거래가능 이력의 unknown만 연구 가정으로 분리한다. 모의체결 세션의 유효한 raw OHLCV·양수 거래량과 다른 필수 조건이 충족되면 다음 거래 세션 raw 시가 체결을 허용하며 observed status는 unknown으로 남긴다. 거래량 0/결측·잘못된 OHLC·확인된 정지·알려진 미체결 제약·필수 action 부족은 미체결/blocked다. 해당 세션 일일 거래량은 시가에 알려진 값이 아니므로 `ex_post_execution_assumption`이며 신호·sizing·사전 유동성 cap에는 사용하지 않는다. 가격·수수료 등 체결 가정과 assumed 종목/거래 수를 결과/API/export에 보존한다. strict 검증 경로는 연구 가정으로 자동 전환하지 않는다.

## 비용·체결 가격·통화

매수 가격은 raw open×(1+half_spread_bps/10000+slippage_bps/10000), 매도는 raw open×(1−동일항)으로 시작한다. commission은 notional 비율+최소 요금/정액 정책, 세금은 시장·상품·매도일별 versioned 비용 프로필로 계산한다. 수수료를 다시 slippage에 포함하지 않는다.

초기 fixture는 고정 테스트 비용을 사용한다. 실제 비용을 반영한 순성과 검증에는 사용자가 선택한 브로커·시장·상품 비용 프로필과 적용 시점 공식 세율·요금 근거가 필요하다. 사용자 전략 선택 자체를 이 검증과 합치지 않는다. 비용 0 연구는 명시적 연구 옵션이며 realistic-cost 검증을 대신하지 않고, 입력 누락을 자동 비용 0으로 처리하지 않는다.

매매 유동성 cap은 의사결정 시점의 adv20을 사용한다. 실제 다음 세션 전체 거래량을 주문 sizing에 미리 사용하지 않는다. 체결 성과 평가에 realized execution constraints를 적용할 경우 ex-post simulation 한계를 별도 표시한다. 정수 주식이 기본이며 fractional shares·short·margin·cash interest는 후속 opt-in이다.

KRW·USD 각각 cash/NAV를 계산한다. 시장 간 합산은 해당 valuation 시각의 검증된 FX가 있을 때만 한다. 미국 폐장과 한국 폐장을 같은 날 동시 지식처럼 처리하지 않는다.

## 지표

NAV return은 cash flow 없는 연구 계좌에서 NAV[t]/NAV[t−1]−1이다. CAGR은 (NAV_end/NAV_start)^(365.2425/elapsed_days)−1, 변동성과 Sharpe 연율화는 기록된 252세션 기준을 사용한다. risk-free는 초기 0 가정임을 표시하거나 일관된 시점별 rate series로 교체한다.

max drawdown은 NAV/cummax(NAV)−1의 최솟값이다. turnover는 거래 gross notional/전 세션 NAV, exposure는 실제 invested/NAV, fee/slippage/tax는 별도 누적한다. trade win rate는 닫힌 lot에 대한 비용 포함 결과이며 열린 포지션·배당 할당 정책을 표시한다. 거래가 없거나 std=0이면 분모 없는 지표는 null+reason이다.

benchmark는 같은 유효 평가 구간·통화·배당 기준·initial capital 가정으로 계산한다. 비용·분배금 기준이 다르면 차이를 표시한다. total-return 가격으로 ledger fill/mark를 하고 현금배당까지 더하는 혼합은 허용하지 않는다.

## 시간순 검증과 사용자 선택

첫 실증은 60/20/20 시간순 분리 후보, 충분한 기간에서는 train 5년/validation 1년/test 1년의 rolling walk-forward와 마지막 2년 최종 holdout 후보를 쓴다. 둘을 중복 최종 평가로 혼합하지 않고 선택한 eval plan을 사전 고정한다.

모든 scaling·quantile·model fit·parameter 선택은 training 구간 안에서 한다. validation은 선택, test/holdout은 평가만 한다. warmup에 과거 train 값은 사용할 수 있지만 미래 test는 fit하지 않는다. label horizon이 겹치는 실험에는 purge/embargo를 추가한다. test fold가 겹치면 성과를 중복 합산하지 않는다.

평가 정책은 최소 거래수·낙폭·비용·구간 안정성·재무 PIT·가격 시간/quality 조건을 사전 config로 고정하고 결과 상태를 보고한다. 이 평가는 사용자 전략 선택을 자동 수행하지 않는다. 모든 전략은 research로 시작하고 사용자만 selected로 바꾼다. 선택은 OOS 검증 인증이나 data gate waiver가 아니다. 전체 trial·실패·holdout 사용을 기록한다.

## 레짐 계산

SMA200보다 높은 close를 up, 그 외 down으로 분류한다. high vol은 t−1까지의 trailing 756세션 realized_vol_20의 80th percentile보다 t vol이 높을 때다. 충분한 trailing history가 없으면 unknown이다. 이 규칙은 설명 가능한 시작 baseline이며 레짐 최적 임계값이라는 주장 없이 평가한다.

candidate state가 3 eligible 세션 연속일 때 그 세 번째 세션부터 confirmed 상태를 전환한다. 초기 state·transition·unknown 처리 정책을 고정한다. 공식 지수가 없으면 검증 ETF proxy security를 명시한다.

HMM 후속은 forward alpha filtering, Kalman은 forward filtering만 live feature로 사용한다. 미래 전체 sample smoothing은 연구 결과에만 별도 표기한다. 미래 입력을 append했을 때 earlier states/features가 달라지지 않는 prefix invariance test를 모든 model adapter에 적용한다.

## 선택 framework adapter 검증

보유·매도·현금 부족·동시 종목·비용·split·배당·정지·missing open·lot·hold interval fixture를 손계산 oracle와 대조한다. 금액은 통화 최소 단위의 명시 반올림 후 같아야 하며 float feature는 사전 고정 tolerance로 비교한다. 미지원 사건은 capability로 명시해 run 생성 전에 거부한다. 빠르지만 ledger가 다른 adapter를 기본 engine으로 지정하지 않는다.

여기서 기준은 손계산 oracle이며 자체 생산 엔진으로 고정한다는 뜻이 아니다. framework의 기본 adjustment·fill-forward·cash dividend·시장 달력이 우리 계약과 다른지 확인하고 설정/adapter로 맞추거나 미지원으로 보고한다. 선택된 engine가 실패했다고 다른 후보로 runtime 전환하지 않는다.

위 정의를 구현하기 어렵다는 이유로 SMA·RSI·재무 score·warmup·선택 비중·cost를 다른 단순 규칙으로 바꾸지 않는다. definition version·허용 오차·손계산 기준은 결과를 보기 전에 고정하고 gate를 느슨하게 바꿔 테스트를 맞추지 않는다. 인수 시 [17 계약](17-implementation-guardrails.md)의 정의 일치·실제 경로·비용 조건을 함께 확인한다.
