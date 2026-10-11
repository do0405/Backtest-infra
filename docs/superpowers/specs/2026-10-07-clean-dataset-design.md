# 미국·한국 Clean Dataset 설계

작성일: 2026-10-07 (Asia/Seoul). 상태: 대화에서 합의한 기본 구조와 VWAP 요구를 구현 계획으로 구체화한 설계.

## 1. 목표와 범위

개인 백테스트에서 가격·거래량·펀더멘탈을 동일한 SQL 조회 계약으로 사용한다. 데이터가 언제 공개되고 언제 수집됐는지, 어느 공급자·정정·계산 규칙을 사용했는지 추적한다. 수정주가, 일봉 기간/Anchored VWAP, 과거 지수 구성 이력을 지원한다.

- 시장: 한국 KOSPI·KOSDAQ·KONEX 및 미국 거래소 상장 종목 전체. 보통주·우선주·ADR·리츠·SPAC와 ETF·ETN을 포함한다. 현재 종목과 요청 기간에 거래한 신규/상장폐지 종목을 전수 관리한다. OTC·채권·옵션·선물은 현재 합의 범위 밖이며 특정 지수/스크리너/표본으로 수집 대상을 제한하지 않는다.
- 해상도: 일봉. 분봉/틱은 필요한 VWAP 일별 합계의 검증·생성 입력으로만 사용할 수 있다. 장중 전략은 현재 범위 밖이다.
- 저장: PostgreSQL이 관측값·채택 이력·공시·기업행동·릴리스의 기준 저장소다. SEC/OpenDART는 유용한 재무 숫자와 계정·문맥·공시 정보를 SQL에 전수 저장하고 최초/정정값을 보존한다. 문서/XML/HTML은 파싱용 임시 입력이며 장기 원문 파일 보관은 요구하지 않는다. 가격 공급자 원본 등 별도로 보관하는 자료만 압축 파일+DB 해시/위치로 관리한다.
- 처리: Python, SQLAlchemy, Alembic, 배치 SQL. Django 관리 화면은 후속 선택 사항이며 현재 구현 의존성으로 추가하지 않는다. DuckDB/Parquet도 측정된 분석 병목이 생기면 추가한다.
- 비용: 무료를 출발점으로 사용한다. 확보되지 않은 기간/종목/항목은 명시한다. 유료 소스는 비교 후보이며 구독·구매는 이 설계에 포함하지 않는다.
- 최초 요청 범위의 설정 기본값: 2016-01-01부터 시장별 최신 완료 세션. 실제 확보 범위는 공급자 적격성 조사 결과로 보고한다. 더 오래된 자료는 이 설정을 확장해 수집할 수 있으며 원천이 있다는 이유로 완전성을 보증하지 않는다.
- 위 날짜는 출력 범위다. 분기/TTM/성장률/평균 잔액/가시 주식 수 및 rolling/anchored VWAP에 필요한 선행 입력은 의존성에 따라 더 과거까지 수집해 SQL/릴리스에 보존한다. 입력 부족을 숨기기 위해 출력 시작일이나 anchor를 바꾸지 않는다.
- 지수 이력은 기존 합의에 따라 모집단/벤치마크 지원 데이터로 포함한다. 초기 조사 후보는 S&P 500, Nasdaq-100, KOSPI 200, KOSDAQ 150이며 실제 확보 가능한 기간을 각각 공개한다.

## 2. 현재 확인한 사실과 아직 검증하지 않은 사항

새 작업 폴더에는 기존 애플리케이션이나 Git 저장소가 없다. 이 요청에서는 문서만 작성한다. 구현 시 별도 초기화 작업에서 Git·의존성·DB 환경을 만든다.

참조 프로젝트는 `E:/side project/invest-prototype-main`, `main`, 커밋 `38f937d7f2d9fbd5fbe2ad0601f05680b13ee179`이다. 읽기만 수행했으며 기존 비추적 문서를 변경하지 않았다.

| 참조 코드 | 가져올 개념 | 새 인프라에서 바꿀 점 |
|---|---|---|
| `data_collectors/sec_access.py`, `sec_accession_delta.py` | 공식 API 접근, 공시 discovery, 재시도 | 처리 성공과 discovery watermark를 분리하고 장기 백필 지원 |
| `data_collectors/opendart_pit_fundamentals_collector.py`, `opendart_xbrl_policy.py` | 접수번호 검증, 공시 문맥, 분기 정규화 | 모든 보고 기간·정정 버전 보존, 접수번호별 원본 검증 |
| `utils/market_data_contract.py` | 가격 기준 분리, OHLCV 구조 검증 | 진짜 as-traded 기준 확인, 거래대금·VWAP 분모 추가, 누락 필드 임의 보충 제거 |
| `screeners/growth_leader_overlay/pit_fundamentals.py` | 가용 시점, provenance, 시점 조회 | 종목 티커 대신 발행사/증권 ID, 단위·문맥·출처를 보존한 버전 조회 |
| `utils/metadata_history.py` | 관측 시점과 원본 identity | 이미 수집한 현재 스냅샷을 과거 구성 증거로 사용하지 않음 |

현재 참조 파이프라인은 가격 최신 756봉, 재무 최신 8분기를 유지하고 이전 revision을 압축한다. 수집기에도 8분기 제한이 있다. 이 보관 정책과 최신값 선택을 그대로 복사하지 않는다. KIS 기간별 시세 요청은 수정주가 옵션 `FID_ORG_ADJ_PRC=0`을 사용하고 정규화 함수는 거래대금을 보존하지 않으며 기업행동 필드를 기본값으로 채운다. 새 어댑터는 엔드포인트별 기준을 명시적으로 검증한다.

공급자 문서는 확인했지만 인증된 실제 샘플 응답, 계정별 권한, 전 종목/전 기간 커버리지, 거래대금과 VWAP 분모의 일치는 아직 검증하지 않았다. 0단계의 필수 산출물로 해결한다. 기존 프로젝트를 실행하거나 수집 결과를 새 데이터셋으로 인증하지 않았다.

## 3. 파이프라인과 책임

```text
가격 어댑터 -> 공급자 원본 보존 -> 정규화된 관측 -> 검증/선택 결정
재무 어댑터 -> 임시 문서 파싱 -> SQL 계정/문맥/공시 값 -> 검증/선택 결정
                                              |
                                              v
                           버전이 고정된 데이터 릴리스 -> PIT 조회 -> 백테스트
```

가격 원본 보관은 공급자 응답의 증거를 소유한다. 재무 추출 계층은 공시별 source value와 필요한 account/context/provenance를 SQL에 보존한다. 어댑터는 수집/파싱을 소유하되 canonical 채택을 결정하지 않는다. 검증 계층은 단위·시장·세션·문맥·기업행동을 확인한다. 선택 계층은 공급자별 evidence와 채택 이유를 기록한다. 조회 계층은 릴리스와 시점을 강제한다. 전략·주문·포트폴리오 회계는 별도 백테스트 엔진의 책임이다.

`raw` 원본은 공급자 응답 그대로라는 의미다. `as_traded` 가격 기준과 혼동하지 않는다. 정규화했다고 검증이 끝난 것으로 간주하지 않는다.

## 4. 저장 및 identity 계약

- 발행사 `issuer_id`, 증권 `security_id`, 상장 `listing_id`를 구분한다. 발행사는 여러 주식 종류를 발행할 수 있다. CIK/DART corp_code는 발행사 식별자이며 증권 ID의 대체물이 아니다.
- 티커·ISIN·거래소 코드 매핑에 유효 기간, 관측 시각, 원문을 연결한다. 재사용 티커·기업합병으로 서로 다른 증권을 이어 붙이지 않는다.
- 공급자 계약에는 endpoint, 계약 버전, upstream/정제 경로, venue/feed, session 범위, 통화·단위, 조정 상태, 타임스탬프 정의를 기록한다.
- `raw_object`는 SHA-256, 압축 전 원문 hash, root에 상대적인 content-addressed storage key, byte count를 갖는다. 절대 운영 경로를 identity에 넣지 않아 다른 raw root로 복원할 수 있어야 한다. `ingestion_attempt`는 동일 원문 재확인을 포함해 모든 수집 시도를 가리킨다. 같은 바이트는 중복 저장하지 않지만 변경된 원문·관측·정정은 덮어쓰지 않는다.
- 영구 보관 대상으로 정한 공급자 원문과 모든 SQL 관측·선택 결정·릴리스는 append-only다. SEC/OpenDART의 임시 파일은 구조화 SQL commit 성공 후 정리하고 실패 시 재수집/재파싱 작업을 pending으로 유지한다. 운영 체크포인트와 조회용 current pointer는 갱신할 수 있다. 역사 SQL fact를 삭제하거나 최근 8분기로 압축하지 않는다.
- 재무 provenance는 filing/source URL, 문서 content hash, 계정 concept/id/name, 원표기 value, scale/decimals/unit, period/context/dimensions, parser/mapping version을 SQL로 보존한다. 문서 hash는 당시 입력의 identity이며 문서 파일 영구 보관을 의미하지 않는다. 필수 evidence는 SQL source fact와 공시 메타데이터다. 문서 전체 재파싱은 upstream에서 다시 받을 수 있을 때만 가능하며 SQL 조회/기존 계산의 재현은 저장한 fact로 수행한다.
- PostgreSQL `NUMERIC`/Python Decimal로 가격·금액·수량을 처리하고 reported unit/precision을 보존한다. 날짜 코드의 선행 0을 유지한다. UTC timestamp와 원천 timezone/precision을 함께 기록한다.
- 파일 원문과 DB 참조는 일관되게 백업·복원한다. 파일 생성 후 DB transaction을 commit하고, DB가 가리키는 파일이 없으면 릴리스 발행을 실패시킨다.

주요 테이블군: `issuer`, `security`, `listing_history`, `identifier_history`, `source_contract`, `raw_object`, `ingestion_attempt`, `exchange_session`, `price_observation`, `corporate_action_observation`, `vwap_daily_stat`, `filing`, `financial_source_document`, `source_financial_fact`, `fundamental_fact`, `derived_fact`, `fund_product_fact`, `validation_issue`, `selection_decision`, `index_definition`, `index_membership_event`, `index_weight_observation`, `index_level_observation`, `dataset_release`, `release_member`, `coverage_report`, `collection_checkpoint`, `target_universe_manifest`, `collection_work_item`.

## 5. 시점 및 정정 계약

세 종류의 시간을 구분한다: 경제적 기간/효력 시점, 공개·이용 가능 시점, 수집 시점.

- `published_at`: 근거가 있는 공개 시각. 날짜만 알면 timestamp 정밀도를 꾸며내지 않는다.
- `available_at`: 소스와 공개 증거에 따른 사용 가능 시각. 날짜만 있으면 시장별 다음 거래 세션 개장부터 사용할 수 있도록 보수적 규칙을 적용한다. timezone·휴장일·DST를 거래 달력으로 처리한다.
- `captured_at`: 우리 시스템이 해당 버전을 실제 수집한 UTC 시각.
- `extracted_at` / `source_fact_first_seen_at`: 해당 SQL source fact를 실제 추출/처음 저장한 시각. 문서의 earlier captured_at과 분리한다. 나중 재파싱한 새 fact는 문서 수신 시각으로 소급하지 않는다.
- `availability_precision`: `timestamp_verified`, `date_only`, `capture_only`, `unknown`.
- `history_mode=reconstructed_public`: 과거 공개자료를 현재 수집해 재구성한다. 신뢰할 수 있는 공시별 공개/정정 증거가 있어야 과거 cutoff에 사용할 수 있다. 과거 가격은 최신 공급자 archive를 재구성한 것임을 표시하며, 알려지지 않은 과거 공급자 정정 시각까지 재현한다고 주장하지 않는다.
- `history_mode=observed_system`: 당시 저장되고 채택된 계산 입력을 재현한다. 입력의 `available_at <= as_of`, `captured_at <= as_of`뿐 아니라 채택 결정의 `recorded_at <= as_of`, 파생 결과의 `materialized_at <= as_of`도 요구한다. 당시 채택/계산 기록이 없으면 unavailable이며 오늘의 결정을 과거 결정으로 소급하지 않는다. 나중에 만든 release가 과거 기록을 묶을 수 있으므로 release 발행일 자체를 cutoff 이전으로 강제하지 않는다.
- observed_system의 재무 SQL source 입력은 extracted_at/source_fact_first_seen_at도 cutoff 이전이어야 한다. 이전 문서 수신 이력이 있어도 그때 SQL에 없던 새 계정은 당시 replay에서 제외한다.
- 모든 파생 결과는 입력 가용 시각과 별도로 `materialized_at`을 기록한다. reconstructed_public에서는 나중에 계산할 수 있지만 공개 입력의 시점과 고정된 release 규칙을 지킨다. observed_system에서는 당시 만들어진 파생 기록만 사용한다.
- 현재 재무 API 응답에 과거 공시일을 단순 부착하지 않는다. 원공시의 정정 전 버전을 확보하지 못하면 해당 과거 버전은 unavailable이다.
- 같은 기간이라도 정정 공시를 별도 record로 보존한다. 가시적인 revision 중 최신을 고르되 metric, period start/end, unit/currency, scope, share basis, provenance를 분리한 후 선택한다.
- 같은 accession/receipt/URL의 content hash 변경이나 철회는 별도 content revision/withdrawal event다. 새 hash에 원공시 published_at/available_at을 자동 재사용하지 않는다. 공식 정정 공개 시각 근거가 없으면 detection/capture 기반 보수적 availability와 correction_time_unknown을 사용한다. reconstructed_public에서도 정정 내용을 원공시일 이후 전체 과거에 역삽입하지 않는다. 옛 SQL 버전과 release는 유지한다.
- API의 ticker-mapping `asof`와 데이터셋의 PIT cutoff는 별개다.

모든 공개 조회는 `as_of`, `release_id`, `history_mode`를 명시한다. 연구 목적에는 `reconstructed_public`을 사용하되 요청과 응답에 모드를 명시한다. DB 스냅샷만으로 과거 공개 가능성을 증명하지 않는다.

QueryContext에 required_profile/research_option을 포함하고 release의 고정 quality/query 버전을 bind한다. 재무 조회는 periodic_report 또는 earliest_ingested_disclosure와 고정 disclosure_policy_version을 명시한다. 응답의 고정 data payload/coverage와 오늘 collector health 등의 live operational envelope를 분리하고 재현성 hash에는 data payload만 포함한다.

## 6. 가격·거래량·기업행동

원주가 OHLCV, 거래대금, 원천의 VWAP/거래조건을 공급자별로 보존한다. 조정 상태가 불명확한 값은 원주가로 인증하지 않는다. 분할·배당 데이터가 없다는 이유로 0 또는 조정계수 1을 삽입하지 않는다.

가격 출력은 `as_traded`, `split_adjusted`, `total_return_index`로 구분한다. 총수익은 배당 재투자 정책이 명시된 별도 계열이며 일반 체결가격처럼 사용하지 않는다. 동일 종목·날짜에서 source 간 비교 전에 venue/feed, 정규장/시간외, 경매·거래조건, 통화·단위, 조정 기준을 맞춘다.

기업행동에는 발표/가용 시점, ex/effective date, payment date, 비율·금액·수취 증권, 원공시 및 revision을 기록한다. 분할·병합은 기계적으로 조정 가능하나 권리락·분사·합병은 별도 이벤트 유형과 검증된 처리 규칙을 사용한다. 미지원 이벤트가 있는 구간의 해당 파생 계열은 unavailable이며 raw 가격을 삭제하지 않는다.

확인되지 않은 급등락을 자동 삭제하거나 평균·다수결로 수정하지 않는다. `yfinance repair`는 원본과 별도의 후보일 뿐이다. 공급자에서 정정이 들어오면 새 revision 및 릴리스를 생성한다. 거래 정지·상장폐지는 공식 lifecycle 근거로 판단하며 가격 누락만으로 확정하지 않는다. 결측 OHLCV/거래량/거래대금을 forward-fill하지 않는다.

## 7. 일봉 기간/Anchored VWAP

지원 정의: 거래일별 VWAP, 최근 N개 거래 세션의 VWAP, anchor session부터 as_of까지의 VWAP. 정규장과 거래조건을 source contract에서 고정한다.

정확 계산의 입력은 동일 거래 집합의 `sum_price_volume`과 `eligible_volume`이다. `sum(sum_price_volume) / sum(eligible_volume)`으로 집계한다. 공급자의 VWAP와 일반 volume만으로 정확한 거래대금을 역산하지 않는다. 공급자 내 계산 분모가 다르면 분리하거나 체결에서 일별 충분 통계를 만든다.

세 방법을 구분한다: `matched_daily_statistics`, `trade_aggregated`, `bar_proxy`. 직접 제공된 일별 VWAP는 `provider_reported`로 보존하되 정확한 기간 VWAP 가능 여부는 별도 feature eligibility다. 기본 정확 VWAP 조회는 앞의 두 방법만 허용한다. `bar_proxy`는 사용자가 명시적으로 근사값을 요청했을 때만 반환하며 정확 조회의 fallback이 아니다.

분할 조정계수 a에 대해 P'=aP, V'=V/a다. 순수 분할의 거래대금은 보존된다. 조회 cutoff까지 공개되고 효력이 발생한 기업행동으로 as_of 주식 수 기준을 맞춘다. 현금배당 총수익 보정과 거래 VWAP은 분리한다.

anchor에는 `anchor_session`, `anchor_known_at`, `anchor_rule`를 기록한다. 미래 봉을 보고 확정한 저점을 과거부터 알려진 anchor로 사용하지 않는다. 당일 완성 일봉 통계는 소스 지연을 반영한 available_at 이후에만 쓴다. N기간은 거래 달력의 N세션이며 결측 행을 압축해 N개 관측으로 대체하지 않는다. no-trade 증거가 있는 0거래량 세션은 0으로 집계할 수 있으나 결측을 0으로 바꾸지 않는다. 분모 0이면 null과 이유를 반환한다.

일반 지수에는 체결이 없으므로 VWAP를 만들지 않는다. ETF VWAP는 별도 증권의 거래 통계다.

## 8. 펀더멘탈

기업형 펀더멘탈은 유용한 재무 숫자를 전수 SQL화한다. 재무상태표·손익/포괄손익·현금흐름·자본변동표의 모든 보고 계정과 EPS/주식 수/배당/부문/주석의 구조화 숫자를 source_financial_fact에 보존한다. 매출·영업이익·순이익·현금흐름·자산/부채/자본·현금·차입금·capex 등은 우선 canonical 매핑하되 이 목록에 없거나 custom/unmapped인 계정을 삭제하지 않는다. 식별번호·문서 페이지 숫자 등 비재무 문서 숫자는 추출 대상이 아니다. 소스별 추출 inventory와 매핑 coverage를 따로 보고한다.

ETF는 기업형 손익/EPS와 별개로 NAV·순자산·좌수·비용·분배·기초지수 및 공개된 구성/보유 정보를 fund_product_fact에 저장한다. ETN은 지표가치·추종지수·발행사·만기·수수료·분배 등 해당 상품 정의에 맞는 값을 저장한다. 상품에 적용되지 않는 기업형 항목은 not_applicable로 표시하고, 적용되지만 확보하지 못한 값은 unavailable로 구분한다. 발행사의 기업 재무를 상품 자체 재무로 잘못 붙이지 않는다. 외국 발행사의 annual/semiannual 공시를 미보고 quarterly로 바꾸지 않는다.

- 공시별 long format fact를 보존한다. US-GAAP/K-IFRS의 의미 차이를 이름만 같다고 합치지 않는다. 연결·별도, 단위, 기간, 전체/지배주주, 주식 수 기준을 각각 필드로 둔다.
- SEC submissions/companyfacts와 accession별 XBRL/iXBRL 입력을 연결해 계정과 문맥을 SQL로 추출한다. companyfacts 표준 태그와 source XBRL의 custom/부문 계정을 구분한다. 공시 원문 장기 archive를 요구하지 않는다. current ticker map만으로 과거 발행사 모집단을 만들지 않는다.
- OpenDART 공시 목록에서 정정 포함 discovery를 하고 receipt별 XBRL/보고서에서 유용한 계정 값을 SQL로 추출한다. 연도/보고서 코드 기반 API가 지정한 원공시 버전을 반환했는지 접수번호로 검증한다. 최초/정정별 source fact를 별도 저장하며 원정정 이력 확보가 안 되면 latest-only로 분류한다.
- 누적/YTD, standalone quarter, annual, instant를 구분한다. Q2/Q3/Q4 및 TTM 파생은 호환되는 입력이 모두 가시적일 때만 만들며 available_at은 사용한 입력의 최댓값이다.
- basic/diluted EPS는 구분하고 annual EPS에서 YTD EPS를 무조건 빼서 Q4 EPS를 만들지 않는다. EPS와 주식 수의 분모가 호환되는 근거가 없으면 파생하지 않는다.
- 회계기간 변경, 52/53주 결산, 통화 변경, 부호·단위 변경, taxonomy 변경을 진단한다. 원본 분기 결측은 0/인접 분기로 채우지 않는다.
- Yahoo/브로커의 공시별 역사 증거 없는 최신 재무는 capture-only다. 역사적 PIT 공백을 채우는 fallback으로 사용하지 않는다.

확정 추가 계산 범위는 ROE·ROA·매출총/영업/순이익/EBITDA/FCF 마진, QoQ/YoY/TTM 성장률, FCF, PER, PBR, EV/EBITDA다. 정의와 분모/기간/자본화/음수/통화/PIT 정책은 [재무 지표 계산 계약](../../data/financial-metric-catalog.md)에 고정한다. 모든 파생값을 SQL에 입력 ID·정의 버전·시점·상태와 함께 보존한다. 기업형 지표를 ETF/ETN 상품 자체에 적용하지 않는다.

필수 성장 계열은 매출, basic/diluted EPS, 영업이익의 QoQ·YoY·TTM YoY다. EPS는 순이익 성장률과 다른 지표이며 기본/희석과 분할 기준을 구분한다. SQL에는 각 계열별 명시적 metric_id와 비교 입력/기간/시점/상태를 보존한다.

## 9. 지수·과거 모집단

공통 A5 단계가 현재 목록과 요청 기간의 역사 lifecycle discovery를 함께 소유한다. 최초 production manifest를 만들기 전에 승인 source의 역사 discovery를 실행하고 결과 및 미확보 범위를 고정한다. 이후 지수 단계는 이를 보완할 뿐 최초 역사 discovery의 선행 의존성이 아니다.

최초 실행 전에 한국·미국 전체 종목 마스터를 target_universe_manifest로 고정하고 종목×도메인×요청 기간 작업을 전수 생성한다. 한국 세 시장 및 ETF/ETN 마스터와 미국 Nasdaq-listed/other-exchange-listed 등 공식 디렉터리·승인 provider 자산 목록을 대조한다. 목록에 있지만 수집되지 않은 종목도 작업/coverage 분모에서 제거하지 않는다. 신규 상장과 확인된 종료 이력을 증분 반영한다. 공식 목록과 provider 지원 종목의 차이는 별도 이슈다.

현재 상장 종목과 현재 지수 구성만으로 과거 universe를 인증하지 않는다. 요청 기간의 상장폐지/상품 종료와 listing/identifier 이력을 전수 discovery하고 공식 지수 구성 baseline+편입/편출 이벤트를 보존한다. 역사 마스터가 불완전하면 그 범위는 미완료이며 현재 생존 종목만으로 전체 완료를 선언하지 않는다. 지수 자료가 가격/재무 전 종목 수집 집합을 제한하지 않는다.

구성/비중은 공개 시각과 효력 시점을 구분한다. 발표됐으나 효력이 오지 않은 종목은 현재 membership에 넣지 않는다. index weights는 official/reconstructed/proxy를 구분한다. 가격지수·gross/net total return, 통화, 방법론 버전, live/backtested 상태를 별도로 보존한다. ETF holdings를 공식 지수 구성으로 인증하지 않는다.

## 10. 검증·채택·릴리스

관측의 검증 결과와 selection decision을 별도 append-only 기록으로 남긴다. 상태는 `provisional`, `accepted`, `quarantined`, `rejected`이며 없는 데이터는 coverage의 `unavailable`로 표현한다. accepted는 정해진 검사를 통과했다는 뜻이며 무오류 보증이 아니다.

출처 계약·원문 무결성·시장/종목/단위 확인·구조 검사·해당 anomaly 해결을 통과해야 accepted가 된다. 검증 증거 등급은 `contract_verified`, `independent_crosschecked`, `official_evidence_checked`로 노출한다. 모든 accepted가 교차검증됐다고 표시하지 않는다. 여러 wrapper가 동일 데이터 서비스를 사용하면 독립 검증으로 세지 않는다. 공유된 SIP 원천 위에 서로 독립된 수집/정제 경로가 있는 경우 그 관계를 그대로 기록한다.

- 가격: finite/범위·비음수 수량, 중복 세션, 시장 세션, 거래 범위, 기업행동 전후 기준, source 변경.
- 거래량: 주/lot 구분, 누적/구간 구분, 거래대금과 같은 집합 여부, 분할 변환의 P×V 보존.
- 재무: 기간/문맥/통화/단위, 공시 identity, reported precision에 맞는 회계 검산, 정정 가시성, 파생 lineage.
- 지수: baseline·변경 이벤트 연속성, 발표/효력 구분, 확인된 전체 비중 자료의 합계, 지수 수익률 종류.

`dataset_release`는 요청 범위에서 과거 cutoff에 필요한 최초/정정/이전 채택 revision 전체와 decision 이력, 파생 parent closure, 원문 hash, schema/normalizer/adjustment/calendar/query rule 버전, 코드 revision, coverage manifest를 고정한다. 현재 최신 선택 record만 묶는 snapshot이 아니다. 한 release 안에서도 4월 cutoff는 최초 공시, 6월 cutoff는 5월 정정 공시를 조회할 수 있어야 한다. 발행은 원자적으로 수행한다. 무결성·미래정보 차단 검사 실패는 발행 차단이다. 명시된 coverage 공백은 부분 릴리스에 기록하고 기능별 eligible mask를 제공한다. 정확 VWAP 누락 때문에 검증된 원주가를 삭제하지 않는다. 전략 소비자는 feature 요구에 따라 부족한 데이터의 처리 규칙을 명시해야 하며 dataset universe 자체를 조용히 현재 생존/완전 종목으로 축소하지 않는다.

재무 release의 필수 증거는 SQL source_financial_fact와 공시/문서 메타데이터다. canonical 지표의 입력이 아닌 custom/unmapped source account도 독립 release member로 고정하며 source 값·context·revision·시점 evidence를 보존한다. source extraction 검증과 canonical 매핑 적격성을 구분해 미매핑 계정을 삭제/누락하지 않는다. SEC/OpenDART 문서 파일 부재는 발행/복원 실패 사유가 아니다. 실제 archive하는 가격 원문에 대해서만 raw 파일 존재 검사를 한다. DB 백업에는 전체 source/canonical/derived fact와 정정/채택 이력을 포함한다.

완료 판정의 분모는 target_universe_manifest 전체다. current/historical universe 완전성, 작업 terminal 비율, 가격/volume/재무/정확 VWAP 확보율을 각각 표시한다. 적용 가능한 required 항목의 unavailable/failed/pending을 단순히 완료로 세지 않는다. not_applicable은 상품 유형 근거가 있을 때만 허용한다. partial release는 중간 산출물이며 전 종목 데이터셋 완료로 표현하지 않는다.

재수집/재정정 후에도 기존 release를 다시 조회한 값과 hash가 동일해야 한다. 자료가 바뀌면 새 release만 발행한다. query rule dispatcher는 release에 고정된 버전을 사용하며 미지원 버전은 명시적으로 거부한다. 오늘의 규칙으로 옛 release를 조용히 재해석하지 않는다.

## 11. 단계별 승인 기준

1. 소스 조사: 미국/한국별 실제 샘플, 권한/기간/종목/세션 커버리지, 수정 기준, VWAP 충분 통계 확보 여부를 evidence matrix로 만든다. 막힌 공급자는 지원 범위와 대안을 기록한다.
2. 공통 기반: 원문-DB 연결, identity, 달력, append-only, 수집 resume, 검증 상태, 백업/복원을 테스트한다.
3. 전 종목 마스터/작업 계획: 한국·미국 현재/과거 대상과 ETF/ETN을 전수 등록하고 모든 종목의 필수 작업을 생성한다. 특수 사례 fixture는 내부 검증 수단이며 수집 대상이나 완료 기준을 줄이지 않는다.
4. 가격·거래량·재무: 전 종목 가격/수량/기업행동과 해당 상품의 유용한 재무 계정/값을 SQL화한다. 최초/정정 전후 PIT, 기간/단위/문맥/YTD, 정확 VWAP 적격성을 검증한다.
5. 모집단·릴리스: 확보된 지수 이력과 lifecycle을 연결하고 immutable release의 시점 조회를 검증한다.
6. 전수 운영: 처음부터 전 종목을 대상으로 승인된 source contract의 시장별 백필→증분/정정/품질 보수→새 release 발행을 수행한다. 내부 처리 배치 크기는 자원 조절이며 전체 대상 축소가 아니다. 운영 완료율과 품질/feature 확보율을 별도 보고한다.

## 운영 보강 계약

[운영 점검](../../data/2026-10-07-clean-dataset-operations-review.md)과 [E 구현 계획](../plans/2026-10-07-clean-dataset-operations-plan.md)은 위 설계에 다음 계약을 추가한다.

1. **품질과 조회:** source 승인, row validation, 매핑, feature 가능 여부, freshness를 별도 상태로 관리한다. core_price_volume/fundamentals_sql/exact_vwap/valuation/index_membership profile과 quality_contract_version을 고정한다. strict 조회는 필수 profile 실패를 거부한다. 연구용 partial/stale 옵션도 무결성/PIT/미지원 규칙 버전 오류를 우회하지 못한다. freshness는 요청 cutoff에 필요한 각 시장 완료 session과 source discovery 상태로 판정하며 오늘의 상태로 역사 조회를 무효화하지 않는다.
2. **작업과 정정:** SQL lease/heartbeat/fencing과 계정 단위 공통 quota를 사용한다. 최근 overlap, 회전 역사 검증, 이벤트 기반 repair를 함께 수행하고 last_verified와 미검증 구간을 공개한다. 수정·삭제 발견은 새 관측/결정/coverage 이력이며 옛 release를 변경하지 않는다.
3. **재무 SQL 추출:** 임시 문서의 numeric/context inventory와 SQL 행·단위·기간·precision·원표기·예외를 비교해 commit한 뒤 문서를 정리한다. 파서 자체의 누락은 개수 일치만으로 증명되지 않으므로 지원 포맷별 fixture/독립 계정 inventory 검산을 한다. 미지원 주석·비구조화 숫자와 추출 실패를 따로 공개한다. 새로 추출한 fact의 SQL 최초 관측/추출 시각은 실제 시각이며 이전 문서 수신 시각으로 소급하지 않는다. observed_system은 이 시각도 cutoff로 제한한다.
4. **공시 범위와 계산:** periodic_report와 earliest_ingested_disclosure는 승인한 공시 경로의 서로 다른 수집 profile이다. accounting_basis/assurance를 보존하고 non-GAAP/잠정 자료를 정기 GAAP/IFRS 자료로 조용히 대체하지 않는다. 직접 보고된 연간 EPS/PER·매출/EPS/영업이익 연간 성장률은 별도 financial_metrics_annual_v1이며 strict 4분기 TTM을 대체하지 않는다.
5. **규모와 불변성:** release_member의 정확 record/decision 집합과 revision closure는 그대로 유지하고 불변 data_segment/segment_member/release_segment로 공유한다. derivation은 정의와 모든 의미 입력/basis의 hash로 재사용한다. requested_as_of는 조회 binding에 남기되 값/주당 기준이 cutoff에 따라 바뀌면 입력 signature도 달라져야 한다. first materialized_at을 보존하고 관측 모드의 시점 gate를 지킨다. 영향 graph로 바뀐 기간/window만 재계산한다.
6. **복구와 운영:** exported DB snapshot과 그 snapshot의 가격 archive 참조/hash를 하나의 backup manifest로 묶고 독립 장치/서비스에 사본을 둔다. WAL/PITR를 사용하면 SQL과 raw 파일을 함께 복원할 수 있는 공통 horizon을 확인한다. SQL 재무 복원에는 SEC/OpenDART 문서 파일을 요구하지 않는다. 전수 복원으로 RPO/RTO, query p95, 요청 수·디스크/WAL·backup 증가를 측정한 뒤 운영 budget을 고정한다.
7. **이슈와 변경:** identity/매핑/소스 기준 변경·수동 선택은 actor/reason/시간/근거/영향 범위와 새 버전으로 처리한다. 구조 오류가 생기면 기존 release 유지+stale 상태를 노출한다. credentials는 provenance/log에 넣지 않고 읽기/수집/발행/마이그레이션 권한을 분리한다.

이 보강은 같은 PostgreSQL/Python CLI에 연결한다. 독립 workflow broker나 Django 의존성을 추가하지 않는다. source 권한·역사 범위·실측 성능은 아직 승인 전이며 partial/unsupported를 전수 완료로 해석하지 않는다.

## 12. 근거 문서

문서 확인일 2026-10-07. 문서상 범위는 0단계의 실제 API 테스트를 대체하지 않는다.

- [SEC 공식 API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces): 공시 이력과 XBRL, companyfacts의 표준 concept 범위.
- [OpenDART 공시 검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001): 정정 포함 검색, 날짜 단위 접수일.
- [OpenDART 전체 재무제표](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020): 응답 접수번호, 연결/별도, 문서상 2015년 이후 정보.
- [Alpaca API 범위](https://docs.alpaca.markets/us/docs/about-market-data-api), [FAQ](https://docs.alpaca.markets/us/docs/market-data-faq): 문서상 2016년 이후, 과거 SIP 권한 조건, VWAP 분모와 일반 volume의 차이.
- [Massive 플랜](https://massive.com/pricing): 무료/유료별 접근 범위. 계약·실제 권한 검증 후 사용.
- [KIS 공식 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_functions.py): 엔드포인트별 원/수정주가 옵션.
- [yfinance repair](https://github.com/ranaroussi/yfinance/wiki/Price-repair): 보정 후보의 한계.
- [KRX](https://data.krx.co.kr/contents/MMC/MAIN/main/index.cmd): 한국 주식/지수 원천 조사 시작점.
- [Nasdaq 공식 종목 디렉터리 정의](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs): Nasdaq/다른 거래소 상장 목록 대조 시작점. 현재 디렉터리가 과거 전체 마스터를 증명하지는 않는다.
