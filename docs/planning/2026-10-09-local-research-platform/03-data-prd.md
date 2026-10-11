# 전종목 가격 펀더멘털 데이터 PRD

전체 한국·미국 대상과 10년 이상의 목표 기간을 유지한다. 전수 수집 목표, 실제 보유, 검증 적격성을 각각 측정한다. 기존 [clean dataset 설계](../../superpowers/specs/2026-10-07-clean-dataset-design.md)의 immutable 관측·Decimal·공개/수집 시각·봉인 계약을 계승한다.

## 데이터 영역과 우선순위

| ID | 영역 | 필수 내용 | 초기 공급자·현재 간극 |
|---|---|---|---|
| D01 | 역사 universe | 증권·상장·상품·티커 변경·상폐·재상장·효력/발표 시각 | 현재 마스터·별칭 재사용, 역사 소스 qualification 신규 |
| D02 | 거래 달력 | 시장·세션·개장/폐장·휴장·조기종료·timezone | 기존 exchange_session 모델, 실제 버전 적재 필요 |
| D03 | 원주가 일봉 | OHLCV·통화·session·feed/venue·단위·source hash | KIS KR/US, Alpaca SIP 후보, KRX 대조 후보 |
| D04 | 기업행동 | 분할·병합·현금배당·권리·합병·청산·상폐 대가 | 계산 엔진 일부 존재, 이력 수집·coverage 미완료 |
| D05 | 거래가능 이력 | 공식 정지·재개·상하한·거래중단·불확실성 | 공급자 0거래와 별도 공식 근거 필요 |
| D06 | 재무 원계정 | filing·concept·context·기간·scope·단위·precision·정정 | SEC/OpenDART 실제 SQL, 표준화 적격성 보강 |
| D07 | 필수 표준 재무 | 매출·영업이익·순이익·자본·자산·CFO·CAPEX·주식수·EPS | fiscal lineage·연결/별도·매핑·입력 closure |
| D08 | 재무 개선 특징 | 매출/이익 QoQ·YoY·TTM YoY, 마진 변화, 흑자전환 | 직접 연간과 분기 구분, 공개 cutoff·정정 버전 필수 |
| D09 | 밸류에이션 | 시총·PER·PBR·EV 계열 | 주식수·주식 class·ADR·분할·FX 기준 일치 후 |
| D10 | ETF·ETN | 유형·발행사·기초·레버리지·만기·상폐·NAV·괴리 | 회사 PER를 대신 적용하지 않음, 상품별 source |
| D11 | benchmark·FX | ETF/지수 값·분배금·FX 시각·통화 | 공식 지수·ETF proxy를 분리, 합산은 적격 FX 후 |
| D12 | coverage와 증거 | 요청 분모·상장 전·missing·unsupported·검증·PIT 수준 | 릴리스·시장·상품·필드·기간별 SQL report |

## 종목 이력과 생존편향

현재 17,758개 마스터를 역사 전체 universe로 사용하지 않는다. `listing_history`·상폐 사건·상품 상태를 별도 구축하고 해당 시점에 실제 존재했던 증권과 거래 가능성으로 전략 universe를 만든다. 상폐된 종목을 오늘 찾을 수 없다는 이유로 빼지 않는다.

상폐 이후 현금 정산·합병 대가·청산 대가·장외 이전 가격이 없으면 `unresolved_terminal_event`로 남긴다. 마지막 종가를 무한 보유하거나 임의 0원 처리하는 기본값은 금지한다. 편향 연구에서 별도 가정을 비교할 경우 가정·민감도·제외 종목을 결과에 표시하고 검증된 실제 정산 성과로 인증하지 않는다.

ETF·ETN·우선주·ADR은 증권 단위의 별개 identity다. 동일 발행사의 보통주·우선주를 합치지 않는다. current ticker 매핑을 과거에 소급하지 않는다. 원천 code 재사용과 합병은 유효기간·관측기간으로 분리한다.

## 재무 개선 모멘텀의 첫 입력

매출 YoY, 영업이익 YoY, 영업마진 전년 동기 대비 변화, CFO TTM 변화, 흑자전환 flag는 재무 개선 연구의 예시 feature다. 제품은 새 feature/전략을 추가하는 확장 계약을 제공한다. 비교 기준이 양수이고 같은 회계 범위·통화·fiscal 기간인 수치만 비율을 만들고, 적자→흑자는 별도 사건으로 표시한다.

한국 누적 YTD의 독립 분기 계산은 실제 fiscal interval과 compatible scope일 때만 Q2=H1−Q1, Q3=9M−H1, Q4=FY−9M을 적용한다. 다른 정정 버전·통화·회계 기준을 섞지 않는다. 미국 52/53주·비12월 결산도 동일한 period 계약으로 처리한다.

재무 특징은 보고서 대상 분기 종료일이 아니라 공개·사용 가능 시점부터 시장에 나타난다. 이후 정정한 재무를 최초 공개 당시의 값으로 사용하지 않는다. 전체 문서 파일의 영구 보관 정책은 변경하지 않고 기존 SQL 계정·context·hash·공시 메타데이터를 보존한다.

DEC-10에 따라 과거 재무 PIT는 탐색에서도 필수다. 당시 공개 버전·수신/공개 정책을 확인하지 못하면 해당 시점의 재무 feature·rank·전략은 unavailable/blocked이며, 현재 재무를 과거 분기 말에 넣지 않는다. 원계정 조회·현재 cutoff의 적격 재무 조회는 별도 제공할 수 있다.

## 가격 검증과 조정

원 OHLC는 양수, high≥max(open,close,low), low≤min(open,close,high), volume·notional은 보고 여부와 비음수 조건을 검증한다. source scope가 다르면 거래량 차이를 단순 오류로 판정하지 않는다. 중복·정정·되돌림·동일값 재확인을 구분한다.

split-adjusted 특징과 raw 체결 가격을 구분한다. 총수익 계열에 반영한 배당을 회계 원장의 현금배당에 중복 반영하지 않는다. 기업행동 이력이 불완전하면 조정 계열이 검증됐다고 표시하지 않는다. VWAP는 적격 거래대금·수량 또는 검증 체결 합계가 있을 때만 exact 표기를 허용한다.

공급자 0거래+동일 OHLC는 `supplier_reported_unverified`로 보존한다. 공식 거래정지·실제 체결 가능성은 별도 상태다. 결측 거래량을 0으로 채우거나, 가격 gap을 forward-fill한 값으로 매수·매도 체결하지 않는다.

잘못된 가격 행은 원관측과 실패 근거를 보존하고 해당 feature 구간·종목의 적격성에서 격리한다. 영향 없는 종목·구간은 계속 제공하되 결측을 삭제해 rolling 세션 간격을 당기지 않는다. 시장 identity·schema·hash 실패는 전체 해당 dataset 무결성 차단이며 행 오류로 낮추지 않는다. 최근 조회/cache 창은 10년 이상 SQL 원본·재무 버전의 보관 상한이 아니다. 수집 미보고 action의 0/1 기본값은 검증된 무사건 증거로 쓰지 않는다. [19 사례](19-invest-prototype-lessons.md)의 T43/T44로 추가 인수한다.

## 데이터 품질 단계

| 단계 | 뜻 | 허용 용도 |
|---|---|---|
| collected | 원응답·SQL 증거 수신 | 상태·원계정 감사 |
| normalized | 구조·단위·numeric 정규화 | 검증 대상 |
| structurally_valid | 필드·context 구조 통과 | 제한된 차트·부분 연구 |
| qualified_current | 현재 입력의 freshness·상품·거래가능·출처 적격 | 현재 특징의 입력 후보 |
| qualified_historical | 과거 identity·공개 시각·기업행동·universe 적격 | strict 역사 연구 후보 |
| release_approved | 명시 scope·profile와 입력 closure 고정 | 해당 scope·profile의 분석 |

가격 분석의 시간 적격성은 `historical_price_session_v1`로 별도 판정한다. 2018년 세션을 2026년에 받은 사실은 2018년 가격 특징을 막는 조건이 아니다. 실제 세션·원천 기준·frozen revision·구조·identity와 해당 cutoff까지의 값만 사용했는지를 검증한다. 재무는 해당 결정 시점에 공개된 버전을 요구한다. 수신 시각을 없애거나 과거 시각으로 바꾸지는 않는다.

이 단계는 새로운 앱 표시 어휘다. 기존 query profile 이름을 임의로 교체하지 않는다. release의 전체 production 조건과 개별 앱 계산의 적격성을 분리한다.

source는 dataset별 고정이며 장애를 다른 공급자로 자동 전환하지 않는다. 대조 소스는 독립 evidence이고 원 source를 조용히 덮지 않는다. 새 source 채택은 별도 qualification·명시 config·dataset hash를 가진다. DEC-11은 조정 기준이 확인된 공급자 시계열의 탐색을 허용하며, 임의 split/dividend 추정이나 검증 총수익 표기를 허용하지 않는다.

## 인수 조건

등록 전체 분모에 unsupported가 남고, 상장 전은 missing과 구분되어야 한다. 과거 ticker 재사용·상폐·정지·배당·분할·회계 변경 fixture를 통과해야 한다. 가격 관측과 원계정 수가 증가했다는 사실만으로 clean coverage 완료를 선언하지 않는다.

[데이터 계약](10-data-contracts-spec.md)이 정의한 필드·시각·원인 코드를 구현하고, [인수 시나리오](15-acceptance-and-traceability.md)의 T01–T10을 통과해야 한다.
