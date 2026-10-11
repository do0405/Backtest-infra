# Fundamentals PIT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 한국·미국 전 종목의 유용한 기업형/ETF·ETN 펀더멘탈 값을 SQL로 저장하고 최초/정정 이력과 PIT·분기/TTM을 제공한다.

**Architecture:** SEC/OpenDART 문서는 임시로 파싱하고 계정 값·문맥·공시 정보는 SQL에 append-only로 저장한다. source account에서 canonical metric으로의 매핑과 파생은 별도 버전 책임이며 latest값 선택은 PIT query에서 수행한다. 문서 영구 archive는 만들지 않는다.

**Tech Stack:** Python Decimal, PostgreSQL, SQLAlchemy, Alembic, XBRL/iXBRL 파서, pytest.

---

설계: [Clean Dataset 설계](../specs/2026-10-07-clean-dataset-design.md). 선행: A1~A3. 실제 전수 백필은 A4 승인/A5 전체 대상 필요. B2는 EPS의 선택적 분할 환산을 사용할 때만 필요하다.

## Task C1: filing/fact와 공개·수집 시간

**Create:** src/backtest_data/fundamentals/models.py, availability.py; alembic/versions/0003_fundamentals.py; tests/unit/test_filing_availability.py; tests/integration/test_fundamental_revisions.py.

- [ ] fiscal end를 공시일로 사용, UTC/현지 날짜 혼동, 날짜밖에 없는 공시를 exact로 표시, captured 현재를 과거 observed_system로 사용한 failing tests를 만든다.
- [ ] filing 및 financial_source_document에 issuer/source/accession 또는 receipt/form/period/source URL/content hash/공개·가용·수집 시각과 precision을 저장한다. 문서 bytes/file FK는 필수로 두지 않는다. 접수와 실제 공개가 동일하다고 증명되지 않으면 conservative policy를 적용한다.
- [ ] source_financial_fact에는 모든 유용 numeric source 계정, name/concept/context/dimensions, value 원표기/Decimal/scale/decimals/unit, period/scope와 filing을 SQL로 저장한다. fundamental_fact는 해당 source rows와 canonical mapping version을 연결한다. metric 미매핑을 source row 삭제 이유로 삼지 않는다. 식별자/단위/문맥이 다르면 distinct fact다.
- [ ] source SQL fact의 extraction_version/extracted_at/source_fact_first_seen_at을 문서 captured_at과 분리한다. 재파싱에서 처음 발견한 숫자/context의 실제 시각을 저장하고 observed_system cutoff 이전인지 검사한다. 재확인으로 최초 시각을 덮어쓰지 않는다.
- [ ] date_only는 source 현지 날짜 다음 trading session open을 available_at으로 만든다. issuer 거래시장별 query 가용성을 계산하고 delay/calendar rule version을 노출한다. unknown timestamp를 ISO timestamp로 꾸미지 않는다.
- [ ] python -m pytest tests/unit/test_filing_availability.py tests/integration/test_fundamental_revisions.py -q --require-postgres 후 git commit -m 'feat: preserve filing-based financial observations'.

## Task C2: 전체 공시/정정 discovery와 계정 값 SQL 추출

**Create:** src/backtest_data/fundamentals/adapters/sec.py, opendart.py, xbrl.py; tests/unit/test_sec_filing_adapter.py, test_dart_receipt_adapter.py; tests/integration/test_filing_resume.py.

- [ ] SEC 예전 submissions shard/정정/custom context, DART 정정 포함 검색/receipt 불일치/latest-only 응답, pending 공시 재시도 tests를 쓰고 FAIL을 확인한다.
- [ ] SEC는 A5 전체 대상의 issuer CIK별 공시를 백필하고 accession별 XBRL/iXBRL을 임시 파싱한다. companyfacts 표준 계정과 custom/부문/주석의 numeric fact를 전수 SQL화한다. source numeric node/account inventory와 저장 행을 대조한다. latest selection 및 8분기 slice를 제거한다.
- [ ] C2 discovery는 첫 output as_of 전에 이미 공개된 마지막 fiscal period부터 지표별 warm-up 분기/평균 잔액/주식 snapshot을 찾는다. 필요한 source facts를 output_start 이전에도 SQL 저장하며 공시 제출일이나 경제적 기간을 output_start로 잘라 버리지 않는다. TTM YoY는 호환되는 8분기와 공시 가시성을 확인하고 미래 공시로 부족한 과거 입력을 채우지 않는다.
- [ ] OpenDART는 전체 issuer의 정정 포함 목록과 receipt별 자료를 파싱해 BS/IS/CIS/CF/SCE의 모든 계정과 유용한 numeric 주석 값을 SQL에 넣는다. full-financial API가 다른 receipt를 반환하면 원공시 fact로 채택하지 않는다. 원본 버전을 읽을 수 없으면 latest_only/coverage gap을 기록한다.
- [ ] 수신/파싱/source SQL commit/정규화/검증 checkpoint를 분리한다. 구조화 SQL transaction 성공 후에만 임시 문서를 정리한다. 실패 공시는 원문 URL/receipt/hash와 pending 재수집 상태를 유지한다. 가격 raw archive 경로를 재무에 사용하지 않는다. 공시 discovery watermark가 전진해도 실패 종목을 완료 처리하지 않는다.
- [ ] E4 inventory commit gate로 숫자/context/단위/기간과 SQL 저장 결과를 대조한다. periodic_report와 earliest_ingested_disclosure는 승인된 입력 범위가 다른 profile이며 accounting_basis/assurance를 보존한다. 재파싱에서 새로 발견한 값의 수집/추출 시각을 과거로 소급하지 않는다.
- [ ] E2/E4의 동일 accession/receipt content replacement 계약을 적용한다. hash가 바뀐 숫자에는 원공시 available_at을 재사용하지 않고 official revision evidence 또는 detected/captured 기반 보수적 시각을 부여한다. original/amendment/replacement/withdrawal을 구분해 옛 버전과 PIT 이력을 보존한다.
- [ ] python -m pytest tests/unit/test_sec_filing_adapter.py tests/unit/test_dart_receipt_adapter.py tests/integration/test_filing_resume.py -q --require-postgres 후 git commit -m 'feat: collect historical filings and amendments'.

## Task C3: canonical 지표와 호환되는 분기 파생

**Create:** src/backtest_data/fundamentals/normalize.py, derive.py, validation.py; config/fundamental_mappings.yaml; tests/unit/test_financial_normalization.py, test_financial_derivation.py.

- [ ] 매출/순이익 범위·연결/별도·원/천원·부호·YTD·53주·fiscal 변경·annual EPS 차감 오류를 합성 원문 fixtures로 만든다.
- [ ] SQL source 계정은 설계 8절의 유용 계정 전수다. canonical 매핑은 핵심 지표부터 구현하고 source taxonomy/concept/context/scope를 key로 한다. custom/unmapped 숫자도 SQL에서 source 이름으로 조회할 수 있게 보존하며 매핑 상태만 구분한다. 소스 추출 누락과 canonical 미매핑을 별도 집계한다.
- [ ] YTD 차분과 연간-Q1~Q3의 Q4는 같은 fiscal cycle/기간/단위/문맥을 공유하고 모두 cutoff 전에 가시적인 입력만 사용한다. derived_fact에는 input record IDs와 max(input.available_at), 실제 계산한 materialized_at을 각각 저장한다. reconstructed_public은 나중 계산을 허용하고 observed_system은 materialized_at <= cutoff를 요구한다.
- [ ] TTM은 호환되는 연속 4분기를 요구한다. basic/diluted EPS와 다른 주식 수 분모는 구분한다. 단순 EPS 차감으로 미보고 Q4를 만들지 않는다. 회계 검산 tolerance는 reported precision과 compatible total scope로 정한다.
- [ ] python -m pytest tests/unit/test_financial_normalization.py tests/unit/test_financial_derivation.py -q 후 git commit -m 'feat: normalize compatible quarterly fundamentals'.

**회귀 기대값:** 최초 Q1 매출=100, 최초 Q2 YTD=250이면 최초 Q2=150. Q1이 나중에 110으로 정정되면 정정 전 query는 150을 유지한다. 정정 후에는 정정 Q1과 Q2의 호환성을 확인하고 새로운 derived revision과 availability를 만들며 기존 derived 입력을 덮어쓰지 않는다.

## Task C4: PIT 선택과 표본 검산

**Create:** src/backtest_data/fundamentals/query.py, selection.py; tests/integration/test_fundamentals_asof.py; docs/data/fundamental-validation.md.

- [ ] reconstructed_public에서 3월 최초 공시/5월 정정에 대해 2월 unavailable, 4월 최초값, 6월 정정값을 검사한다. 같은 릴리스의 observed_system은 입력 captured, 채택 decision.recorded_at, 파생 materialized_at을 모두 cutoff로 제한한다. 입력만 과거에 있고 채택/계산은 나중에 수행됐으면 unavailable인 회귀를 추가한다. 릴리스가 나중에 생성됐다는 이유만으로 유효한 과거 기록을 제외하지 않는다.
- [ ] 가시성 필터 후 동일 metric+기간+단위+scope+basis 안에서 revision과 source 우선순위를 선택한다. 미래 fact, 최신 Yahoo capture-only, 서로 다른 연결 scope로 historical gap을 메우지 않는다.
- [ ] issuer 기반 재무를 security와 연결하되 당시 identifier/listing 유효성과 기업행동 기준을 확인한다. 반환에는 원문/공시/시점/매핑/파생 evidence를 포함한다.
- [ ] 모든 대상 issuer의 추출/매핑/PIT coverage를 집계하고 특수 사례는 임시 공시 입력과 SQL numeric/period/scope를 검산한다. 정상·정정·결산기 변경·unmapped 및 annual/semiannual foreign issuer의 지원 상태를 기록한다. 전수 수집을 사례 종목으로 제한하지 않는다.
- [ ] python -m pytest tests/integration/test_fundamentals_asof.py -q --require-postgres 후 git commit -m 'feat: query point-in-time filing revisions'.

## Task C5: ETF·ETN의 상품형 값 SQL 저장

**Create:** src/backtest_data/fundamentals/products.py, adapters/us_products.py, adapters/kr_products.py; models에 fund_product_fact와 fund_holding_fact 추가; tests/unit/test_product_fundamentals.py; tests/integration/test_all_product_coverage.py. **Modify:** config/fundamental_inventory.yaml, 해당 schema의 새 migration.

- [ ] ETF를 발행사 기업 EPS로 채우는 오류, ETN을 펀드 NAV로 오인, fund series/share class 혼합, not_applicable와 unavailable 혼동, 상품 공시 revision 테스트를 작성한다.
- [ ] A4에서 승인된 KRX/상품 제공기관/SEC 상품 자료의 source-specific schema로 ETF NAV·순자산·좌수·비용·분배·기초지수, ETN 지표가치·발행사·만기·비용·분배 값을 추출한다. 상품 종류별 적용 항목 inventory를 고정하고 공개 보유 내역은 fund_holding_fact에 structured SQL로 보존한다.
- [ ] 상품/series/share class와 발행사 ID를 분리하고 공시·기준일·available/captured·단위·정정 이력을 연결한다. 과거 증거가 없는 현재 profile 값은 capture-only다. 상품 자료 파싱 입력 역시 SQL commit 후 정리한다.
- [ ] A5의 모든 ETF/ETN에 필수 작업을 생성하며 미지원 상품도 coverage 분모에 남긴다. 실제 적용 안 되는 기업 재무는 근거와 함께 not_applicable, 적용되는 상품 값의 미확보는 unavailable이다.
- [ ] python -m pytest tests/unit/test_product_fundamentals.py tests/integration/test_all_product_coverage.py -q --require-postgres 후 git commit -m 'feat: store product-specific financial values'. 기대: 상품 값과 발행사 값이 구분되고 전수 coverage가 유지됨.

## Task C6: ROE·ROA·마진·성장·FCF·밸류에이션

**Create:** src/backtest_data/fundamentals/metric_policy.py, ratios.py, valuation.py; config/financial_metric_definitions.yaml; tests/unit/test_financial_ratios.py, test_valuation.py; tests/integration/test_metric_asof.py. **Modify:** derive.py, models.py, 해당 schema의 새 migration.

선행: 재무 비율은 C3, 가격 배수는 B1/B2와 C3 및 A5 identity. 함수는 입력 기반 계산이며 release models를 import하지 않는다. D2/D3가 검증된 입력 집합과 연결한다. 정의 기준은 [재무 지표 계약](../../data/financial-metric-catalog.md) financial_metrics_v1이다.

- [ ] 계약의 숫자 회귀와 평균 잔액 누락, 범위/기간 불일치, capex/리스/D&A 중복, 0·음수 비교값, 현재 주식 수, 복수 common class 누락, ADR/통화 불일치, total-return 가격 혼입의 failing tests를 작성한다.
- [ ] ratios.py에 parent ROE/total ROA, TTM 마진, FCF와 QoQ/YoY/TTM 성장률을 구현한다. 평균 잔액은 기간 시작/종료를 사용하고 분모/손익 scope를 맞춘다. FCF/표준 EBITDA와 회사 reported/adjusted 값을 별도 정의로 둔다.
- [ ] 매출, basic EPS, diluted EPS, 영업이익의 QoQ/YoY/TTM YoY 12개 명시적 metric_id를 필수 등록한다. 각각의 비교 기간/정정/PIT/분할/0·음수·누락 회귀를 test_financial_ratios.py와 test_metric_asof.py에 추가하고 EPS TTM의 직접 보고 4분기 합계 정책을 검사한다.
- [ ] valuation.py에 raw 가격·가시적 주식 수 기반 common cap, diluted PER, common PBR, book-basis EV/EBITDA를 구현한다. EPS/주식 수의 분할/ADR 기준, 모든 common class, debt/preferred/NCI/cash, 통화 및 FX 증거를 검사한다. 누락을 0으로 대체하지 않는다.
- [ ] EPS_TTM은 4개 직접 보고 희석 분기 EPS의 호환 합계와 eps_method를 사용한다. 가중평균 분모가 크게 다른 분기들을 NI_TTM/평균 주식 수로 대체하는 회귀를 차단한다. EBITDA는 consolidated 전체 NI를 사용하며 NCI가 있을 때 parent NI를 넣는 오류를 검사한다.
- [ ] first-output-date 회귀에서 pre-start 분기/평균 잔액/자본/주식 입력으로 TTM YoY·ROE·PER를 계산한다. 그 입력이 없으면 해당 지표만 unavailable이며 output 범위는 유지한다. 경제적 기간이 과거여도 공시 available_at이 미래인 입력을 차단한다.
- [ ] definition_version/as_of/input IDs/available_at/materialized_at/unit/scope/eligibility/reason을 derived_fact에 저장한다. 과거 각 as_of에서 가시적 입력을 먼저 선택하고 현재 재무로 과거 배수를 재작성하지 않는다. C6 integration은 DB의 고정 candidate input set으로 검사하고 D2 이후 release 연결 회귀를 추가한다.
- [ ] python -m pytest tests/unit/test_financial_ratios.py tests/unit/test_valuation.py tests/integration/test_metric_asof.py -q --require-postgres 후 git commit -m 'feat: derive versioned financial and valuation metrics'. 기대: 계약 숫자/시점/상태 회귀 PASS.

## C 최종 완료 기준

E4의 별도 financial_metrics_annual_v1을 추가해 직접 보고된 연간 EPS·매출·영업이익 기반 지표를 지원한다. strict 4분기 TTM 계산을 연간 값으로 대신하지 않는다. E5 매핑 수정/수동 결정은 새 버전으로 반영하고 옛 release를 수정하지 않는다.

모든 유용 source 계정과 상품 값이 SQL에 저장되고 최초/정정 기록이 보존된다. 채택/파생은 SQL source value와 정확한 context까지 역추적된다. 문서 파일 없이 DB만으로 재무 조회/계산을 재현하는 테스트를 통과한다. 역사 재구성과 당시 replay를 구분하고 미확보 구간을 최신값으로 채우지 않는다. 전 종목의 추출/매핑/적용 지표 coverage를 공개한다.
