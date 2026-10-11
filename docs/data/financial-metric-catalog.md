# 재무 파생 지표 계산 계약

작성일: 2026-10-07. 정의 버전: financial_metrics_v1 및 별도 financial_metrics_annual_v1. 사용자 요청에 따라 아래 지표를 초기 확정 계산 범위에 포함한다. 이 문서는 계획상의 계산 계약이며 실제 값 수집/계산이 완료됐다는 의미가 아니다.

## 적용 범위와 저장

기업형 지표다. ETF/ETN의 상품 자체에는 적용하지 않고 not_applicable을 기록한다. 금융업/리츠 등에는 지표별 applicability policy를 사용하고 산업 특화 지표로 조용히 대체하지 않는다. 공시상 구조와 호환되는 계산만 제공한다.

모든 값은 SQL derived_fact에 metric_id/definition_version, entity/period, value/unit, as_of, release/입력 집합, available_at, materialized_at, input_record_ids, scope/currency/share_basis, eligibility/status/reason을 함께 저장한다. source 값과 회사가 발표한 조정 지표는 별도 record로 보존한다. 금융 source 파일 영구 보관은 요구하지 않는다.

E3의 재사용 구현에서는 계산 당시 evaluation_as_of와 조회 binding의 requested_as_of/release를 분리할 수 있다. 정의·입력·기간·주당/통화 기준·disclosure/history_mode가 같은 의미 계산만 공유하고 최초 materialized_at은 보존한다. cutoff에 따라 선택 입력/주당 기준이 달라지면 다른 signature다. 요청 cutoff의 입력 가시성과 observed_system gate는 재사용 시에도 적용한다.

비율은 Decimal 0.2처럼 저장하고 표시할 때 20%로 변환한다. PER/PBR/EV-EBITDA는 multiple, FCF/EBITDA/시가총액/EV는 currency_amount다.

## 확정 지표와 기본 정의

| metric_id / 지표 | 기본 계산 | 핵심 입력 조건 |
|---|---|---|
| roe_parent_ttm / ROE | 지배주주 귀속 TTM 순이익 ÷ 평균 지배주주 자본 | 손익·자본의 연결/귀속 범위 동일 |
| roa_total_ttm / ROA | 연결 전체 TTM 순이익 ÷ 평균 연결 총자산 | 지배주주 순이익을 전체 자산과 혼용하지 않음 |
| gross_margin_ttm / 매출총이익률 | TTM 매출총이익 ÷ TTM 매출 | 동일 기간/매출 정의 |
| operating_margin_ttm / 영업이익률 | TTM 영업이익 ÷ TTM 매출 | 동일 연결/회계 범위 |
| net_margin_ttm / 순이익률 | 연결 전체 TTM 순이익 ÷ 연결 TTM 매출 | 전체 범위끼리 비교 |
| ebitda_margin_ttm / EBITDA 마진 | 표준 재구성 EBITDA_TTM ÷ TTM 매출 | EBITDA 정의와 모든 입력 확인 |
| fcf_cfo_less_capex_q, _ttm / FCF | 영업현금흐름 − 현금 capex | capex 지출을 양의 사용액으로 정규화 |
| fcf_margin_ttm / FCF 마진 | FCF_TTM ÷ TTM 매출 | 동일 기간/연결 범위 |
| growth_<metric>_qoq, _yoy, _ttm_yoy / 성장률 | 현재값 ÷ 비교값 − 1 | 비교값 > 0, 호환 fiscal 기간/단위/범위 |
| per_diluted_ttm / PER | 당시 원주가 ÷ 검증된 희석 EPS_TTM | 동일 주당/분할/통화 기준, EPS > 0 |
| pbr_common / PBR | 당시 전체 보통주 시가총액 ÷ 최신 가시 보통주 장부자본 | 모든 보통주 종류의 가격/주식 수 확인, 자본 > 0 |
| ev_ebitda_ttm / EV/EBITDA | 당시 EV ÷ 표준 재구성 EBITDA_TTM | EV 구성 항목 전체와 EBITDA > 0 |

성장률 대상은 매출, 영업이익, 순이익, basic/diluted EPS, 영업현금흐름, FCF다. QoQ는 이전 실제 fiscal quarter, YoY는 이전 fiscal year의 대응 quarter, TTM YoY는 직전 동기 TTM과 비교한다. 재무제표 잔액을 분기/TTM 흐름처럼 합산하지 않는다.

## 필수 성장률

사용자가 지정한 필수 성장률은 매출·주당순이익(EPS)·영업이익이며 각각 QoQ·YoY·TTM YoY를 제공한다. EPS는 basic과 diluted를 별도 계열로 저장한다.

| 필수 계열 | QoQ ID | YoY ID | TTM YoY ID |
|---|---|---|---|
| 매출 | growth_revenue_qoq | growth_revenue_yoy | growth_revenue_ttm_yoy |
| 기본 EPS | growth_eps_basic_qoq | growth_eps_basic_yoy | growth_eps_basic_ttm_yoy |
| 희석 EPS | growth_eps_diluted_qoq | growth_eps_diluted_yoy | growth_eps_diluted_ttm_yoy |
| 영업이익 | growth_operating_income_qoq | growth_operating_income_yoy | growth_operating_income_ttm_yoy |

EPS TTM 성장률에도 호환되는 직접 보고 분기 EPS 4개 합계 정책을 적용하고 basic/diluted를 섞지 않는다. 현재/비교 값 모두 동일 as_of에서 가시적인 revision과 동일 분할/ADR/통화 기준을 사용한다. 기존 순이익·영업현금흐름·FCF 성장률 범위도 유지한다.

## 평균 잔액·회계기간

평균 자본/자산은 해당 TTM 시작 직전 잔액과 TTM 종료 잔액의 산술평균이다. 둘 다 query cutoff에서 가시적인 revision을 사용한다. 동일 범위와 fiscal cycle이 필요하며 시작 잔액이 없으면 마지막 잔액으로 대신하지 않는다. 결산기 변경/52·53주 차이는 period lineage에 표시하고 호환 여부를 판정한다.

기본 ROE는 parent 범위다. common ROE 또는 total ROE를 추가하면 별도 metric_id와 정의로 구분한다. 순이익이 음수인 ROE/ROA/마진/FCF는 유효한 음수로 보존하되 분모 자본/자산/매출 <= 0인 경우 status=nonpositive_denominator로 null을 반환한다.

## FCF와 EBITDA

PER의 EPS_TTM v1은 호환되는 직접 보고 희석 분기 EPS 4개의 합이다. 각 값은 기록된 reported share basis에서 as_of의 분할/ADR 기준으로 환산한다. 분기별 가중평균 희석 주식 수가 달라도 보고 EPS를 합산하며 TTM 순이익/평균 희석 주식 수로 대체하지 않는다. 직접 보고 분기 EPS가 없으면 unavailable이며 annual EPS−YTD EPS나 annual EPS fallback을 쓰지 않는다. eps_method=sum_reported_diluted_quarters와 입력 precision을 저장한다.

FCF v1은 현금흐름표의 CFO에서 유·무형자산 취득의 현금 capex를 뺀다. 기업 인수, 금융자산 투자, 모든 투자현금흐름을 capex로 합치지 않는다. 총액 계정과 세부 계정을 동시에 더하지 않는다. source sign policy로 지출 방향을 정규화하고 abs()로 부호를 무조건 지우지 않는다. capex 미확보를 0으로 가정하지 않는다. FCF v1을 FCFF/FCFE라고 부르지 않는다.

표준 재구성 EBITDA v1은 동일 consolidated scope/기간의 전체 순이익(비지배지분 귀속 이익 포함) + 법인세 비용 + 순이자 비용(이자비용−이자수익) + 감가상각/상각이다. 지배주주 순이익을 대신 넣지 않는다. 영업이익+감가상각 계열은 별도 정의로 구분한다. 순이자·D&A 중복 또는 손상차손의 임의 가산을 금지한다. 회사 reported EBITDA/Adjusted EBITDA는 공시 정의·reconciliation과 함께 별도 저장하고 표준값의 자동 fallback으로 사용하지 않는다. 필요한 입력이 미확보면 해당 파생은 unavailable이다.

## 시가총액·PER·PBR·EV

주당 지표에는 당시 실제 거래가격을 사용한다. 배당 포함 total-return 가격을 사용하지 않는다. EPS/주식 수는 cutoff에서 알려진 분할·ADR 비율과 가격 기준에 맞춘다. 과거에 현재 shares outstanding을 넣지 않으며 가중평균 희석 주식 수를 시가총액 수량으로 대신하지 않는다.

market_cap_common_pit는 보통주 종류별 원주가×가시적 유통 주식 수를 합산한다. 동일 증권의 복수 listing은 중복 계산하지 않는다. 주식 수는 최근 공개된 snapshot과 검증된 이후 기업행동으로 구하며 reported_share_based_estimate 여부와 기준일을 노출한다. 모든 common class 가격/수량을 확보하지 못하면 capitalization_coverage_incomplete를 기록하고 issuer PBR/EV를 인증하지 않는다. 단일 보통주 검증 사례의 PBR은 가격/BVPS와 동일해야 한다.

common_equity는 지배주주 자본에서 equity-classified preferred capital을 구분한 값이다. 우선주가 없는 것으로 확인된 경우와 우선주 항목이 미확보인 경우를 구분한다.

EV v1 = common 시가총액 + 이자부 부채(이자부 리스 포함) + 우선주 장부자본 + 비지배지분 장부금액 − 현금및현금성자산. short/long/current/noncurrent와 리스의 총액/세부 중복을 제거한다. preferred/NCI는 book_basis임을 표시하며 시장가치 EV와 혼동하지 않는다. 현금 이외 금융자산을 자동 차감하지 않는다. 구성 요소가 없으면 근거 있는 not_applicable/zero인지 missing인지 판별한다.

같은 통화가 원칙이다. 필요한 FX는 별도로 검증된 가용 시각/정의 버전/입력 ID가 있을 때만 사용하고, FX가 없으면 currency_mismatch로 미계산한다. ADR/복수 share class의 범위를 추측해 합치지 않는다. EBITDA <= 0 또는 EPS <= 0이면 배수는 null과 이유를 저장한다. 정의상 가능한 음의 EV 배수는 별도 flag를 붙이고 데이터 오류로 자동 삭제하지 않는다.

## 성장률의 0·음수 비교값

비교값 <= 0이면 통상 성장률 %를 만들지 않는다. 값은 null로 두고 loss_to_profit, loss_narrowed, loss_widened, from_zero, profit_to_loss 등 상태와 절대 증감을 함께 저장한다. 비교값 > 0에서 현재값이 음수인 성장률은 계산하되 profit_to_loss를 표시한다. 결측/비교 기간 불일치/단위 불일치는 별도 이유다. EPS 성장률의 두 시점도 같은 분할/주당 기준으로 환산한다.

## 보고 빈도와 별도 연간 지표

annual/semiannual issuer와 분기 EPS 미확보 종목도 source 계정 수집 대상에 남긴다. reporting_frequency, fiscal 기간, 회계 기준, 실제 공개/수집 시각과 미계산 이유를 노출한다. financial_metrics_v1의 직접 보고 4분기 EPS_TTM 정책은 유지한다.

financial_metrics_annual_v1은 아래 직접 연간 보고 값 기반 지표를 별도 제공한다. 성장률은 호환되는 직전 fiscal year와 비교하며 짧아진 결산 전환 기간을 정상 1년으로 비교하지 않는다. EPS 양쪽 값은 요청 cutoff의 호환 분할/ADR 기준으로 환산한다. 매출/영업이익은 해당 연간 consolidated scope, EPS는 해당 보통주 귀속 basic/diluted 보고 기준을 유지한다.

| metric_id | 정의 |
|---|---|
| per_diluted_fy | raw 가격 / 가시적인 최근 직접 보고 diluted annual EPS. EPS > 0 및 호환 주당 기준 필요 |
| growth_eps_basic_fy_yoy | current basic annual EPS / previous basic annual EPS − 1 |
| growth_eps_diluted_fy_yoy | current diluted annual EPS / previous diluted annual EPS − 1 |
| growth_revenue_fy_yoy | current annual revenue / previous annual revenue − 1 |
| growth_operating_income_fy_yoy | current annual operating income / previous annual operating income − 1 |

성장률 previous <= 0과 결측은 위 성장률 예외 정책을 따른다. direct_annual_report/annual basis, fiscal end, 공시 profile/assurance, snapshot age를 반환하고 별도 definition version/hash를 release에 고정한다. TTM metric 요청에 연간 값을 조용히 반환하지 않는다. fixture에서 quarterly 미확보 + annual EPS=3 + price=60이면 per_diluted_fy=20이지만 per_diluted_ttm은 unavailable이어야 한다.

## PIT·릴리스·재계산

requested_output_start와 dependency_input_start를 분리한다. 첫 output as_of에서 가시적인 마지막 fiscal period를 발견한 뒤 TTM/TTM YoY에는 필요한 4/8개 호환 분기, ROE/ROA에는 TTM 시작 직전 잔액, PER/PBR/EV에는 그 전에 공개된 EPS/자본/주식 수/부채/현금 snapshot을 선행 수집한다. 공시 지연·결산기에 따라 더 오래된 기간이 필요하므로 output_start−24개월로 단순 잘라 충분하다고 가정하지 않는다. 선행 입력도 SQL/release lineage에 보존한다. 자료가 없으면 opening metric을 unavailable로 표시하고 요청 출력 시작일을 뒤로 옮기지 않는다.

재무 비율은 공시/정정이 가시적이 되는 시점, 일별 배수는 완료 가격이 가시적이 되는 시점에 맞춘다. 각 역사 as_of마다 그때 가시적인 재무/주식 수/기업행동/가격 입력을 먼저 선택한 후 계산한다. 최신 정정 재무로 전체 과거 배수를 덮어 계산하지 않는다.

공시 정정, 가격 정정, 주식 수/기업행동 변경, 승인 FX 또는 정의 변경은 새 derived revision/새 release를 만든다. financial_metrics_v1 정의와 전체 input closure를 release에 고정한다. observed_system은 당시 materialized/decision 기록을 요구하며 reconstructed_public은 나중 계산을 허용하되 역사 입력 가시성을 지킨다.

## 필수 회귀 사례

- ROE: NI_parent_TTM=20, equity endpoints=80/120이면 0.2. ROA: NI_total_TTM=24, assets endpoints=200/280이면 0.1.
- 마진: 매출=100, gross/operating/net=40/15/10이면 0.4/0.15/0.1.
- FCF: CFO=120, capex=30이면 90. capex null이면 unavailable.
- EBITDA: NI_total=50, NI_parent=40, tax=10, net interest=5, D&A=20이면 85이며 75가 아니다. NCI 귀속 손익과 NCI 장부자본을 구분한다. 누락/중복 D&A는 미계산.
- PER: raw 가격=60, 호환 EPS_TTM=3이면 20. EPS<=0은 null.
- EPS_TTM: 각 quarter 순이익=10, 희석 가중평균 주식 수=10/20/40/80, 직접 보고 EPS=1/0.5/0.25/0.125이면 합계=1.875다. NI_TTM/평균 주식 수=40/37.5로 바꾸지 않는다. 직접 보고 한 분기가 없으면 unavailable이다.
- 단일 클래스: 가격=60, 유통주식=10, common equity=300이면 시가총액=600, PBR=2.
- EV: common cap=600, debt=100, preferred=20, NCI=10, cash=50이면 680. EBITDA=85이면 배수=8.
- 성장: 100->120이면 0.2. -10->5는 null+loss_to_profit.
- 필수 성장률 회귀: 매출 100->120은 0.2, basic EPS 2->3은 0.5, diluted EPS 2->2.5는 0.25, 영업이익 10->15는 0.5다. 네 계열의 QoQ/YoY/TTM YoY 비교 기간 선택, 분할 환산, 정정 시점, 0·음수·누락 입력을 각각 검사한다.
- 미래 공시/현재 주식 수/total-return 가격/미지원 FX/누락 share class/다른 release 입력을 차단한다.
- 첫 output as_of가 2016년 초이고 최신 가시 분기가 2015Q3인 합성 사례에서 필요한 2013Q4~2015Q3 흐름과 평균 잔액/snapshot을 선행 입력으로 수집한다. 미래 2015Q4 공시를 쓰지 않고 warm-up 없는 해당 지표만 unavailable로 둔다.

## 근거와 정의 선택

[CFA 재무비율 목록](https://www.cfainstitute.org/sites/default/files/-/media/documents/support/programs/cfa/cfa_program_level_ii_financial_ratio_list.pdf)을 비교 기준으로 사용한다. 위 parent/common 범위와 EV book-basis는 이 데이터셋의 명시적 v1 선택이다.

[SEC 비GAAP 설명](https://www.sec.gov/rules-regulations/staff-guidance/corporation-finance-interpretations/non-gaap-financial-measures)은 FCF의 정의 차이와 EBITDA/조정 EBITDA 구분을 설명한다. 본 계약은 이 데이터셋의 저장/계산 정의를 버전으로 고정한다.
