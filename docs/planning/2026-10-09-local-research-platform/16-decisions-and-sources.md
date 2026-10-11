# 기술 결정과 공식 출처

기준일은 2026-10-09 KST다. 비용·무료량·서비스 접근·라이선스는 배포와 의존성 lock 시 다시 확인한다. 아래 공식 설명과 이 프로젝트의 설계 판단을 구분한다.

## 주요 결정

| ADR | 결정 | 이유·대안·재검토 조건 |
|---|---|---|
| ADR01 | 기존 PostgreSQL 기준 이력 유지 | 감사·정정·release·동시성 기반 존재. SQLite 전환은 기존 계약을 크게 바꿈 |
| ADR02 | FastAPI + React 로컬 웹앱 후보 | 차트·대량표·async job·layout 제어. Streamlit/Dash는 단기 분석 UI 대안이지만 별도 frontend 목표와 유지 경로 비교 필요 |
| ADR03 | Lightweight Charts 기본 chart adapter | 사용자 원하는 금융 차트와 local data 연동. Advanced Charts와 전체 TradingView 기능은 별도 제품 |
| ADR04 | framework qualification 후 기본 engine 고정 | Zipline-reloaded 우선·LEAN local 다음, vectorbt는 선택 연구 도구; paid CLI/PRO 자동 이용 없음 |
| ADR05 | public minimal collector + private R2 우선 | 기존 원격 설계 계승. 공개 범위 미허용이면 private Actions/Cloud Run 비교 |
| ADR06 | 원격 transient SQL + portable subset | 현재 SQL collector 재사용, 전체 DB cloud mirror 피함. state subset round-trip 검증 필수 |
| ADR07 | lifecycle·tradability·product history 필수 | 현재 universe의 과거 사용과 회사/상품 혼합 방지 |
| ADR08 | 검증/탐색 분리·domain clock 분리 | 가격은 세션 cutoff·수신 감사용, 재무는 공개/정정 PIT; 미래 정보 유입 금지 |
| ADR09 | 하루5시간·worker1·dynamic RAM | 31.1GiB 전체보다 당시 가용5.3GiB를 고려, PC 상시 수집 지양 |
| ADR10 | API job queue는 PostgreSQL | 초기 Redis/Celery 의존 불필요. job 규모·다중 host 요구 생기면 재검토 |
| ADR11 | 분석 cache는 재생성 가능 | SQL이 기준, Parquet/DuckDB는 실제 성능 개선 시 도입 |
| ADR12 | Python 확장 플랫폼·기본 rule regime | 초기 전략 스타일은 예시, 고급 수학은 interface만·이번 연결 제외 |

## 공식 조건과 설계 적용

### GitHub 실행과 보관

공개 저장소의 표준 제공 실행기는 실행 요금이 무료다. 비공개 GitHub Free는 월2,000분과 artifact500MB가 포함된다. larger runner·저장·cache의 조건은 별도로 관리한다. 다른 저장소의 계정 사용량을 차감한다. [공식 요금](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

예약은 지연·누락될 수 있고 공개 repo는 활동이 없으면 60일 후 예약이 비활성화될 수 있다. 따라서 calendar-aware catch-up과 상태 감시가 필요하다. 공식 문서는 UTC 기본 예약과 선택 timezone을 설명한다. [예약 조건](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

artifact/log 기본 보관은90일이며 조직·repo 정책으로 달라질 수 있다. artifact를 장기 이력·유일한 checkpoint로 쓰지 않는다. [보관 조건](https://docs.github.com/en/organizations/managing-organization-settings/configuring-the-retention-period-for-github-actions-artifacts-and-logs-in-your-organization).

### R2 Standard

월 무료량은10GB-month, Class A100만회, Class B1,000만회이며 직접 egress는 무료다. Standard 무료량이고 Infrequent Access와 다르다. 10GB-month는 매월10GB씩 누적하는 저장 공간이 아니다. 내부8GB hold·safety margin은 설계 판단이며 강제 과금 상한이 아니다. [R2 요금](https://developers.cloudflare.com/r2/pricing/).

R2는 강한 일관성을 설명하고 PutObject 조건부 연산을 지원한다. 따라서 versioned manifest와 ETag CAS를 후보 commit/owner 방식으로 사용한다. 실제 SDK·실패·재시도·fencing은 실증해야 한다. [일관성](https://developers.cloudflare.com/r2/reference/consistency/), [S3 호환표](https://developers.cloudflare.com/r2/api/s3/api/).

### Google의 역할

Cloud Run Jobs 무료량은 us-central1 가격 기준 월240,000vCPU초·450,000GiB초다. Scheduler는 결제 계정 전체 예약 작업3개가 무료다. 컨테이너 보관·빌드·network·기타 서비스는 별도다. [Cloud Run 가격](https://cloud.google.com/run/pricing), [Scheduler 가격](https://cloud.google.com/scheduler/pricing).

가정상 총2vCPU·2GiB를30일 동안 하루1시간 실행하면216,000vCPU초·216,000GiB초라 이 두 실행 무료량 안이다. 총 리소스에는 collector·transient SQL 등을 합산한다. 실행비 외 항목까지0원이라는 보장은 아니다.

일반 Google 계정은 Gmail·Drive·Photos 공유15GB를 제공한다. Drive는 비공개 변경분 전달의 대안이며 전체 SQL 장기 저장의 필수 서비스가 아니다. [Google 저장량](https://support.google.com/googleone/answer/9312312?hl=en).

Apps Script는 consumer 계정 URL Fetch20,000/day, trigger runtime90min/day, script6min/execution 조건을 설명한다. 작은 metadata 확인 후보이며 현재 SQL 기반 전수 수집기의 무변경 실행 장소가 아니다. [할당량](https://developers.google.com/apps-script/guides/services/quotas).

GOOGLEFINANCE 역사값은 Sheets API·Apps Script로 내려받거나 접근할 수 없다고 공식 도움말이 명시한다. 따라서 자동 전수 clean SQL 수집의 주 소스로 설계하지 않는다. Google 실행 서비스와 Google Finance 데이터 소스를 구분한다. [GOOGLEFINANCE 도움말](https://support.google.com/docs/answer/3093281?hl=en).

### 가격·재무 공급자

Alpaca Basic 공식 안내는 무료 US 주식·ETF, 역사2016년 이후, historical200calls/min, 최신15분 제한을 설명한다. 무료 실시간 IEX와 과거 SIP를 구분하고 `feed=sip`와 최근 종료 제한을 명시한다. 계정·상품·상폐·전기간 coverage는 실제 실증 대상이다. [plan](https://docs.alpaca.markets/us/docs/about-market-data-api), [FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).

KIS 공식 API 저장소는 국내/해외 시장 데이터 구현 근거로 참조한다. 예제에 매매 API가 있어도 이 앱의 수집 adapter에는 데이터 조회 경로만 사용한다. 실제 API access·endpoint history·volume 단위·계정 호출 조건은 source qualification으로 확인한다. [공식 저장소](https://github.com/koreainvestment/open-trading-api).

KRX 공식 일별 서비스는 한국 가격 대조 후보다. 신청·인증키·서비스 범위·이용 조건과 실제 수집은 완료 상태로 가정하지 않는다. [서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd).

SEC data API는 인증키 없이 submissions·XBRL JSON을 제공하고 nightly bulk를 안내한다. companyfacts는 표준 taxonomy·entity-level 범위를 갖기 때문에 현재 source 계정 전체·custom/context 증거를 자동 대체하지 않는다. bulk 사용도 기존 provenance·정정·시점 계약을 검증해야 한다. [EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

OpenDART 전체 재무제표 API는 보고서·사업연도·연결/별도 기준과 반환 항목의 근거다. 현재 repo의 JSON/XBRL 수집·접수번호 대조를 계승한다. 공시 대상 기간을 실제 공개 시각으로 쓰지 않는다. [공식 가이드](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019017).

### 차트와 분석 라이선스

Lightweight Charts는 별도의 금융 차트 라이브러리이며 사용자가 원하는 화면의 후보다. 공식 문서에 NOTICE와 TradingView 링크 등 attribution이 안내돼 있으므로 앱에 보존한다. 자체 datafeed·지표·marker·상태 UI를 구현한다. [문서](https://tradingview.github.io/lightweight-charts/docs), [차트 제품 비교](https://www.tradingview.com/free-charting-libraries/).

vectorbt 현재 LICENSE.md는 Apache2.0+Commons Clause 조건을 포함한다. 단순 Apache/MIT와 같은 조건으로 분류하지 않는다. 무료 개인 로컬 연구 후보로 검토하고, 유료 서비스/상용화 시 사용 조건을 별도 검토한다. 무료 패키지와 PRO 기능을 혼동하지 않는다. [라이선스](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md), [공식 기능·설치](https://vectorbt.dev/).

## 결정 상태와 외부 의존

| 항목 | 현재 상태 | 필요한 결정·증거 |
|---|---|---|
| 코드 공개 범위 | 최소 수집 코드 공개 허용 | API 키·토큰·계정 인증·전략·데이터 비공개 검사 |
| 불완전 역사 탐색 연구 | 사용자 허용·등급 분리 | supplier-defined 가격 계열 탐색, 재무 PIT·가격 세션 cutoff·품질 gap 보존 |
| 최장 PC 미접속 | 최대30일 확정 | 30일 보관·복구·용량 실측 인수 |
| 유료 예외 상한 | 자동0원, 월합계10,000원 이하 제안만 | 데이터/서비스 전체 비용 근거와 사용자 이용 확인 후 결정 |
| 역사 상폐·정지·기업행동 공급자 | qualification 미완료 | market/product/period별 coverage·권리·사용 조건 |
| 전략 선택·validation | 모두 research, 사용자 최종 selected | selection·data quality·성과 validation 독립, 자동 승격 없음 |
| 현재 보유 원장 | 기본 수동 import | actual journal 입력·stale 허용기간 |
| PostgreSQL 호스트 volume 위치 | 확인 전 | Docker/WSL 위치·용량 증가·backup reserve |

미확정은 문서 작성 실패가 아니라 구현 전에 적용할 기본안과 외부 의존의 경계다. 실제 배포·비용·공개는 관련 선택이 준비된 범위에서 수행한다.

## 저장소 근거

[README](../../../README.md), [현재 구현 상태](../../data/implementation-status.md), [무료 원격 갱신 설계](../../data/free-remote-update-local-sql.md), [기존 clean 설계](../../superpowers/specs/2026-10-07-clean-dataset-design.md), [KIS 수집·복구 계획](../../plans/2026-10-09-kis-us-kr-repair-resume.md)을 기준으로 현재 기능과 신규 기능을 구분했다. 진행 중인 데이터 수치는 00의 고정 스냅샷 날짜를 따른다.

## 초기 버전 문서 검증 기록

2026-10-09 초기 v0.1 검증에서 당시17개 파일·PRD7개·질문80개·인수30개·요구78개·내부링크33개·JSON예시3개를 확인했다. 당시 원격 소유권 원자성·legacy 인계·달력/기업행동 실제 적재 작업을 보강하고 재검토 승인을 받았다. 이 수치와 승인은 아래 v0.2 개정의 새 검증 결과를 대신하지 않는다.

이는 문서의 구조·일관성 검증이며 신규 앱 runtime·실제 공급자 coverage·전략 수익 검증은 아니다. 현재 구현과 SQL·기존 수집 프로세스·인증정보는 이번 문서 작성으로 변경하지 않았다.

## 구현 품질과 비용에 관한 개정

v0.2는 사용자와 DEC-01–16을 고정하고 17 구현 품질 계약, 18 프레임워크 비교를 추가했다. Python 확장·가격 세션/재무 PIT 분리·연구/사용자 선택/validation 분리·source 자동 전환 금지·자동 유료0원/월1만원 이하 제안만·고급 수학 interface만을 관련 PRD/계약/작업에 반영했다. 금지 대체·미연결 완료·비용/범위 변조 인수12개를 추가한 당시 계획은 T01–T42다.

프레임워크 근거와 실제 qualification의 구분은 [18 비교](18-framework-evaluation.md)에 있다. 실행하지 않은 앱/engine test나 시장 성과를 완료로 보고하지 않는다.

## 버전 0.2 문서 검증 기록

2026-10-09 개정 후 19개 Markdown·PRD 7개·원 질문 80개·사용자 정책 DEC 16개·인수 T01–T42·추적된 요구 98개를 확인했다. 내부 파일 링크 42개는 실제 경로가 존재하고 JSON 예시 3개는 파싱된다. 문서 fence·ID 누락/중복·미연결 요구·정책 원장의 pending 여부와 폐기한 승인/자동 승격 표현을 검사해 PASS를 확인했다.

독립 읽기 전용 검토는 19개 문서를 읽고 Approved로 판정했다. 비차단 의견인 기존 승인/승격 표현과 탐색용 입력의 미래 기업행동 제한을 정리했다. 가격 세션 reader와 기존 봉인 계약 보존, 재무 공개 버전 PIT, Python 확장, 사용자 선택과 validation 분리, source 고정, 무료 엔진 qualification·비용 통제의 문서 정합성을 검토한 결과다.

이 기록은 기획·명세의 검증이다. 신규 앱 구현·엔진 설치/성능/회계 실행·실제 원격 수집·10년 전수 coverage·전략 수익은 이번 개정에서 검증하지 않았다. 해당 증거는 14의 구현 Task와 15의 인수에서 확보한다. 기존 SQL·수집기·인증정보·sealed release는 이 문서 개정의 변경 대상이 아니다.

## 버전 0.3 Invest Prototype 검토 개정

19 문서를 추가하고 기존 가격756봉/재무8분기 정책의 오용·행 오류의 전체 실패 확대·source completion과 coverage 혼동·기업행동 기본값·현재 모집단 소급·state 초기화·과거 성공 표시의 재사용을 사례와 인수에 연결했다. T43–T48의 회귀 시나리오와 사용자 DEC-17의 연구용 거래가능 가정 T49를 추가한 현재 인수 계획은 T01–T49다. 읽기 전용 근거와 현재/과거 상태의 한계를 19에 명시한다.

DEC-17은 유효한 raw OHLCV·양수 거래량의 체결 세션에서 공식 거래가능 이력의 unknown을 연구 가정으로 분리하는 선택이다. 실제 관측·품질 인증은 바꾸지 않고, confirmed halt/invalid price/action·미래 거래량의 decision/sizing 유입을 차단한다. 기존 확정 정책은 재질문하지 않았다. runtime·전수 coverage·실제 데이터 수정은 수행하지 않았다.

2026-10-09 v0.3 문서 검증에서 20개 Markdown·PRD7개·원 질문80개·확정 정책17개·추적 요구98개·인수49개·내부 파일 링크64개·JSON예시3개를 확인해 PASS였다. 링크 존재·fence·ID 누락/중복·미연결 요구·미확정 정책 잔존·DEC-17 반영·구현 요청문의 최신 범위를 검사했다. 이 검증은 문서 정합성이며 신규 runtime나 Invest Prototype 회귀 테스트 실행 통과가 아니다. v0.2의 독립 검토 승인은 당시 버전의 기록이고 이번 v0.3의 새 독립 승인으로 재사용하지 않는다.

## 버전 0.4 단계 연결·확장 개정

[20 연결 계약](20-pipeline-and-extension-contracts.md)을 추가해 같은 평가 문맥·Python 전략 진입 풀·보유 관리·레짐 연결 위치·명시 목표 범위·정의 bundle·추가 데이터/방법의 시점·단위·비용 경계를 보강했다. 초기 구현은 기존 DTO·registry·함수를 재사용하며 범용 workflow·초기 고급 모델을 추가하지 않는다. 인수 T50–T56을 추가한 현재 계획은 T01–T56이다.

사용자는 판단별 검사에 구현 복잡도 억제를 요청했고(DEC-18), 전략별 풀을 진입 판단 풀로 설명했다(DEC-19). 원격의 무료 운영 선호를 확인했으며 계산 위치의 정확한 의미·초기 거래 범위·시장 간 자금·현재 수량 출력은 DEC-20–23의 질문 원장으로 구분한다. 새 답변 전까지 기존 기본안을 사용자 확정으로 바꾸지 않는다. 전체 runtime·시장 coverage·새 수학 방법의 성능을 검증한 개정은 아니다.

2026-10-09 v0.4 문서 검증에서 21개 Markdown·PRD7개·원 질문80개·결정 원장23항목(답변 반영19·pending4)·추적 요구98개·인수56개·내부 파일 링크69개·JSON예시3개를 확인해 PASS였다. pending은 DEC-20–23만이며 답변을 받았다고 표시하지 않았다. 구조/링크/ID/요구 연결·최신 구현 요청문을 검사한 결과이고 신규 runtime 검증·독립 명세 승인 기록은 아니다.
