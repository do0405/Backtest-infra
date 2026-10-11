# Prices Volume VWAP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 공급자별 원가격·volume·거래대금·기업행동을 보존하고 검증된 조정계열과 정확 기간/Anchored VWAP을 만든다.

**Architecture:** 공급자 응답은 관측 테이블로만 들어온다. 조정·VWAP은 원문 및 source contract에 연결된 입력을 순수 함수로 처리하며 선택/릴리스가 가시성을 결정한다.

**Tech Stack:** Python Decimal, PostgreSQL NUMERIC, SQLAlchemy, Alembic, SQL window queries, pytest.

---

설계: [Clean Dataset 설계](../specs/2026-10-07-clean-dataset-design.md). 선행: Foundation A1~A3, live 수집은 A4 승인과 A5 전체 작업 집합에 의존한다. 대상은 양 시장 전 종목 및 ETF·ETN이다.

## Task B1: 원주가/거래량/거래대금 관측 스키마와 어댑터

**Create:** src/backtest_data/prices/models.py, normalize.py, adapters/alpaca.py, adapters/krx.py, adapters/kis.py; alembic/versions/0002_prices_actions.py; tests/unit/test_price_adapters.py; tests/integration/test_price_observations.py.

- [ ] adjusted 입력을 as_traded로 오인, 거래대금 누락을 0으로 변환, lot/주 및 누적/구간 혼동, invalid OHLC, duplicate/revised row, 요청 이후 봉 반환 사례의 failing tests를 만든다.
- [ ] price_observation=(security, listing/venue/feed, session, basis, source_contract, raw_ref, source_revision, observed_at)와 OHLC/volume/notional/currency/unit/availability를 저장한다. 가격은 accepted/available/captured를 별개로 유지한다.
- [ ] Alpaca 요청은 feed=sip, adjustment=raw를 명시한다. KIS는 endpoint별 옵션을 계약으로 정의해 기간별 raw 요청을 확인한다. KRX source는 A4에서 확인한 머신리더블 경로만 구현한다. 미확인 필드/규칙을 default로 생성하지 않는다.
- [ ] volume=0은 provider no-trade 증거가 있을 때 유효하며 missing과 구분한다. 부분/미완료 봉은 아직 완성 일봉으로 채택하지 않는다.
- [ ] python -m pytest tests/unit/test_price_adapters.py tests/integration/test_price_observations.py -q --require-postgres 실행 후 git commit -m 'feat: ingest explicit price and quantity bases'. 기대: revision 보존과 no-fill 검사 PASS.

## Task B2: 기업행동과 조정 기준

**Create:** src/backtest_data/prices/actions.py, adjustments.py, action_adapters.py; tests/unit/test_corporate_actions.py, test_price_adjustments.py; tests/fixtures/prices/split_reverse_dividend_cases.json.

- [ ] 분할/병합, 미래 이벤트, 이벤트 정정, 현금배당, 미지원 권리락/분사/합병, EPS/주식 수 기준 혼용을 검사하는 합성 fixtures를 만든다.
- [ ] 기업행동은 event type, announcement/available/captured, ex/effective/pay date, ratio/cash/received_security, 원공시 hash와 revision을 보존한다. 동일 공급자 정정은 별도 관측이다.
- [ ] as_of까지 공개되고 효력이 발생한 분할만 사용해 가격·수량 변환을 구현한다. 순수 분할은 P'=aP, V'=V/a, notional'=notional이다. known_at 이후 query에서만 revised event를 적용한다.
- [ ] total_return_index는 별도 배당 재투자 정책/통화/비율 계열로 만들고 거래가격/VWAP 입력으로 사용하지 않는다. 첫 정책은 세금/비용 없는 gross return, 배당락 기준 재투자 벤치마크이며 실현 포트폴리오 cash 처리와 구분한다. 미지원 복합 이벤트 구간은 해당 파생만 unavailable로 둔다.
- [ ] python -m pytest tests/unit/test_corporate_actions.py tests/unit/test_price_adjustments.py -q 후 git commit -m 'feat: derive action-aware price series'. 기대: P×V 보존과 미래정보 차단 PASS.

**핵심 회귀 기대값:**

~~~python
# 기존 주당 100, 100주 거래 -> 2:1 분할 기준 50, 200주
assert adjusted_price == Decimal("50")
assert adjusted_volume == Decimal("200")
assert adjusted_notional == Decimal("10000")
~~~

## Task B3: 일별 충분 통계와 VWAP

**Create:** src/backtest_data/prices/vwap.py, trade_aggregation.py; tests/unit/test_vwap.py; tests/integration/test_vwap_queries.py.

- [ ] 서로 다른 분모, missing vs zero, 20세션 grid, 미래 anchor, 분할, 정정 체결, 중복 체결을 포함한 tests를 쓰고 FAIL을 확인한다.
- [ ] vwap_daily_stat에 sum_price_volume/eligible_volume, trade_scope, method, source/raw hash, available/captured, materialized_at과 revision을 저장한다. 일반 bar.volume을 matched denominator로 인증하기 전에는 사용하지 않는다. observed_system에서는 당시 채택/계산된 통계와 기업행동 결정만 사용하고 reconstructed_public에서는 원문 가용 시점을 지켜 나중 집계할 수 있다.
- [ ] 계약이 승인된 체결 입력은 원주가·주단위로 정규화하고 세션/조건을 필터링한다. 원천이 제공하는 correction/cancel을 적용하고 event ID로 dedupe한다. 역사 archive가 정정 후 상태만 제공하면 reconstructed_public로 표시하고 observed_system replay를 인증하지 않는다.
- [ ] 조회에서 source/time/coverage를 검증한 후 sum(N)/nullif(sum(D),0)를 계산한다. daily/rolling/anchor를 제공하고 anchor_known_at <= as_of를 요구한다. 정확 query는 matched_daily_statistics/trade_aggregated만 허용한다. provider_reported와 bar_proxy를 별도 반환하고 silent fallback하지 않는다.
- [ ] python -m pytest tests/unit/test_vwap.py tests/integration/test_vwap_queries.py -q --require-postgres 후 git commit -m 'feat: calculate eligible daily and anchored vwap'.

**숫자 회귀:** 두 날 N=1000,2000 및 D=10,40이면 VWAP=60이다. daily VWAP 100과 50을 단순 평균한 75는 오류다. 분할 전 100×100, 분할 후 60×200이면 2:1 조정 기준 기간 VWAP=22000/400=55다. volume만 그대로 사용한 가격 조정은 잘못된 가중치다.

## Task B4: 교차검증·채택과 전 종목 coverage

**Create:** src/backtest_data/prices/validation.py, selection.py; tests/unit/test_price_selection.py; tests/integration/test_price_sample_pipeline.py; docs/data/price-validation.md.

- [ ] 다른 session/feed/basis를 불일치 오류로 오인, 동일 wrapper를 독립으로 오인, spike를 무조건 삭제, repair 결과를 원문 덮어쓰기, volume 누락을 suspension으로 판단하는 회귀를 작성한다.
- [ ] source 계약이 동등할 때만 비교한다. price 차이는 verified tick/보고 precision, quantity 차이는 해당 provider 계약의 범위로 진단한다. 검증되지 않은 임의 % tolerance로 자동 채택하지 않는다.
- [ ] 선택 결정에는 observation IDs, validation issue IDs, rule_version, 이유, evidence grade, recorded_at을 기록한다. unresolved mismatch는 quarantined다. 단일 source 계약 검증 통과와 독립 교차검증 통과를 구분한다.
- [ ] A5의 전체 작업 목록으로 한국·미국 전 종목과 ETF·ETN을 수집해 원주가/분할/VWAP와 missing coverage를 생성한다. 특수 사례 fixture는 내부 검사일 뿐 수집 종목 제한이 아니다. 새 source로 비교하는 것은 기존 source history 교체가 아니다.
- [ ] python -m pytest tests/unit/test_price_selection.py tests/integration/test_price_sample_pipeline.py -q --require-postgres 후 git commit -m 'feat: select auditable price observations'.

## B 완료 기준

E2의 recent overlap/회전 historical scan/event repair를 가격·기업행동 source 계약에 연결한다. E1 freshness/profile과 E3 공유 segment를 통합 release 단계에서 적용한다. 모든 과거 바를 매일 복제·재계산하지 않고 변경 영향 범위를 기록한다.

같은 원문은 멱등, 새 원문/정정은 별도 revision. 모든 가격과 volume의 단위·세션·조정 기준을 추적할 수 있다. 정확 VWAP 지원 범위를 명시하며 unsupported 이벤트/누락 session을 숨기지 않는다. 미국 전체 기간의 정확 VWAP를 승인하려면 A4의 충분 통계/체결 권한 증거가 필요하다.
