# 구현 상태 — 2026-10-08 KST

## 2026-10-10 보완 — 실행 중인 작업과 배포 대기

일봉 기본 정책 `release_query_v8_daily`는 SEC/DART 공시번호와 공개일을 연결한다. 원본 아카이브 전수 대조를 필수 조건에서 제외했고, 정정은 자신의 공개일 다음 세션부터 사용한다. 기존 물리 시각·원자료·v5-v7 봉인은 보존한다. [일봉 정책](pragmatic-daily-policy.md)을 참고한다.

공급자 계약에서 원주가/수정주가와 분할·배당 범위를 구분한다. Alpaca 분할 수정주가의 실제 응답과 SQL 커밋을 확인했으며 기존 원주가 수집과 별도 전수 워커가 이어 실행 중이다. 기업행동 완전성은 새 정책의 필수 게이트가 아니다. 전 기간 완료·세 소스 합의·정확 VWAP·과거 상장 이력 완성을 뜻하지 않는다.

원격 수집과 비공개 델타·로컬 SQL 동기화 진입점, 페이지/공시 자식 작업 재개, KRX·상품·수정주가의 별도 갱신 창을 구현했다. 운영 SQL의 일부 원자료를 이동/재처리해 시각 보존을 확인했다. 원격 저장소/클라우드가 아직 정해지지 않아 원격 실행은 비활성이다. 배포에는 저장소·비공개 저장공간·계정 소유권과 기존 원자료 ID 인계 검증이 필요하다. [원격 구현](remote-pipeline.md)을 참고한다.

활성 Docker/PostgreSQL 데이터는 E에 유지한다. 이전 때 만든 중복 콜드 VHD 두 개만 삭제해 약83.5GiB를 회수했다. 기존 수집을 중단하거나 재시작하지 않았다. 공개일 보완·수정주가·Docker 상태를 기존 2시간 감시에 추가했다.

아래 표는 초기 구현 기록이며 현재 공급자 키·승인 상태와 혼용하지 않는다.

이 문서는 초기 계획과 실제 실행을 구분한다. 26개 작업 전체 완료를 선언하지 않는다. 전수 대상은 유지하고 수집 예산만 제한했다.

| 영역 | 실제 구현/검증 | 남은 일 |
|---|---|---|
| A 기반 | Python package/CLI, PostgreSQL 18, 별도 개발·테스트 DB, Alembic, 공유 키 경로, SQL provenance, 전수 master/issuer discovery, 전수 코드·국문/영문명 별칭 SQL 및 정정/철회, 작업 중복 재사용 | 상품 분류, 과거 상장/상폐·티커 재사용, 과거 identity, 계약/라이선스 qualification, 거래소 calendar 실제 적재 |
| B 가격 | 한국·미국 KIS 원주가 실제 수집, 공식 미국 공급자 마스터 매핑, 원응답 보존, Decimal 정규화·검증, 정정/되돌림 관측, 순수 분할/배당·VWAP 계산 엔진, Alpaca 무료 과거 SIP 연결·재개·repair 검증 | Alpaca 키 설정과 별도 대조 수집, independent reconciliation, 전체 기업행동 history/coverage, exact VWAP 입력, 전 기간 backfill |
| C 재무 | SEC/OpenDART 원본 구조화 숫자 실제 SQL 적재, custom/nil/차원/단위/precision, 추출 inventory, 원본·replacement·alias, CIK mismatch 격리, 실제 표준계정 SQL 매핑 v1/v2 및 계산 실행·미산출 사유, CFS/OFS receipt 검증 | 연결/별도 및 fiscal lineage 인증, 분기/YTD SQL 파생 연결, 주식/ADR 기준·다중 class/FX 입력, 상품별 펀더멘탈, earliest disclosure, 완전한 public PIT evidence |
| D 릴리스 | immutable SQL triggers, full target 고정, 원본/정정/미매핑 source 계정 및 별칭 전체 이력 closure, 공유 segment, catalog snapshot, 명시적 cutoff/history mode 조회 | 정상 production profile/활성화, source qualification과 calendar/freshness의 실제 연결, 전수 규모 검증 |
| E 운영 | lease/heartbeat/fencing, 공통 rate budget/Retry-After, 재개·repair, source inventory gate, SQL+raw snapshot 백업 및 임시 DB 복원 기능, 상태 CLI, SQL 시도 횟수 기반 재개 공정성, 공급자 제한 격리, 계획/실행 예산 분리 | 회전 historical correction/PAC withdrawal scanner, live 우선 budget, dependency repair/derived SQL 재사용, remediation workflow, 권한 분리, 독립 백업·WAL/PITR·전수 capacity 측정 |
| 지수 | 계획과 strict unavailable 경계 유지 | 공식 과거 baseline/구성종목 변경·발표/효력 시각·지수 값/방법론 공급자 qualification 및 수집 |

계산 함수 테스트 성공과 실제 지표 coverage를 구분한다. 실제 `fundamental_fact`, `derived_fact`, `vwap_daily_stat`가 아직 없으면 해당 값이 생성됐다고 주장하지 않는다. KIS 거래대금/일반 거래량의 나눗셈을 exact VWAP으로 채택하지 않는다.

현재 master는 미국 13,251개 및 한국 4,507개(KOSPI 2,575/KOSDAQ 1,825/KONEX 107), 합계 17,758개이다. 이는 현재 공급자 directory의 전체 항목이다. 보통주만의 수, ETF/ETN의 완전한 분류, 과거 전체 상장/상폐 포함 여부를 인증하는 수가 아니다. 이전 master도 SQL에 보존한다.

수집의 `succeeded`는 해당 API 단계가 완료됐다는 뜻이다. source coverage/semantic mapping/PIT/필수 profile 검증이 별개이며, 현재 전수 clean coverage와 정상 활성화는 false다. `incremental`은 기존 명시 작업 큐 재개이며 자동 날짜 증분·정정 스캔은 미구현이다.

실측 행 수·테스트 수·릴리스·백업은 다음 CLI 실행 결과와 로컬 `artifacts` 기록을 기준으로 확인한다. 이력 파일에는 키·토큰·DSN을 넣지 않는다.

## 최초 실행 검증

- 전체 테스트: **282 passed**, 실제 PostgreSQL `backtest_test` 사용. Python compilation과 Git whitespace 검사 통과. Source key·DB password·SEC 연락 정보가 Git 대상 파일에 들어가지 않았음을 확인했다.
- 실제 SQL: 공시 108개, 구조화 source financial fact **94,459행**, KIS 가격 **8,587행**, 가격 검증 실패 이력 68개. Source 계정은 원본/정정·파서 버전별 숫자와 nil을 포함한 감사 행 수이며, 표준 재무 지표의 완성 행 수가 아니다.
- 최종 전체 target manifest: `b31ee12c-40aa-484a-9807-ed8fafc1ec0a` — 17,758개, 2016-01-01~2026-10-07 요청 기간. 현재 master 전체이며 과거 상장/상폐 universe는 미검증이다.
- 최종 sealed release: `ef8778b8-2e4c-45d6-ae1c-09ed4cba5b1b`, `release_query_v3`, `research_partial`. Master 원응답 5개, 가격/거부 행 원응답, 원계정 SQL·decisions·검증 실패 이력을 정확 membership으로 고정한다. Production pointer는 활성화하지 않았다.
- 실제 고정 조회: v2 단일 QA entity에서 source account 1,825행, 가격 493행 확인. 요청 지표 4개는 mapping 미완료에 따라 명시적 unavailable이며 strict partial query는 차단됐다. 최종 v3 전수 현재 observed 조회에서도 42,588개 source account가 보이고 2016 public cutoff에는 0개가 보여 capture-only 값의 소급을 차단했다.
- Snapshot backup과 새 무작위 테스트 DB 복원은 실제로 검증했다. 최종 v3 봉인을 포함한 snapshot은 28개 모델 테이블/97개 raw object를 복원했다. Dump 약 92 MB, 백업·검증 약 8.3초, 복원·행 수 확인 약 13.9초였다. 로컬 사본이며 독립 재해 복구·WAL/PITR·전수 RPO/RTO는 미완료다.

위 숫자는 최초 실행의 고정 기록이다. 이어서 실제 source 계정을 표준 이름으로 연결했으며 최초 매핑 1,792건과 v2 이력 1,792건을 SQL에 보존했다. 미검증 fiscal/회계/주식 기준을 임의로 채우지 않았다. 실제 재무비율·VWAP은 여전히 미산출이며 아래 후속 실측과 구분한다.

## 무료 소스와 후속 실행

- 무료 출발 조건을 유지한다. 미국은 Alpaca Basic의 과거 SIP(2016년 이후, 종료 15분 이상 과거)를 우선 후보로 연결했다. `feed=sip`, `adjustment=raw`, `asof=-`, New York 날짜·DST와 완료된 날짜를 고정한다. 실제 응답 capture와 페이지 요청 anchor는 별개다. 현재 로컬 키는 없으며 미국 13,251개 가격 작업은 pending 상태로 보존됐다. 무료 계정 접속·상폐·ETN·전체 기간 coverage를 인증한 상태는 아니다.
- 한국은 KIS 실제 수집을 유지한다. KRX 공식 일별 서비스를 다음 기준·대조 소스로 추천하지만 연결·이용 신청·실제 적재는 미완료다. 가격/기업행동/지수 이력 전체를 한 무료 공급자만으로 완성할 수 있다고 주장하지 않는다.
- 정규화는 모든 SQL source 행을 대상으로 수행한다. 기본 실행은 scan cutoff보다 늦게 도착한 입력을 보류하고 실제 행 계산 완료 시각을 기록한다. 큰 document inventory를 매 batch마다 가져오지 않도록 metadata 읽기를 분리했다. `fundamental_mapping_v1` 해시는 보존하고 HTTPS IFRS 지원은 v2로 추가했다.
- OpenDART 전체 계정 API를 CFS/OFS 각각 수집하고 접수번호·기업·연도·보고서 코드 일치를 요구한다. `frmtrm_q_amount`도 보존한다. 확인되지 않은 실제 fiscal 날짜는 unknown이며 달력 분기를 추정하지 않는다.
- 후속 릴리스 전수 검증에서 JSON v2의 서로 다른 응답 행이 같은 source signature로 합쳐지는 문제를 발견해 봉인을 차단했다. 원 bytes를 저장하지 않아도 공식 API를 재조회하여 기존 13개 document hash와 정확히 같은 자료임을 모두 확인했다. 기존 SQL·시각을 수정하지 않고 누락 node **100개**만 새 수집/추출 시각으로 복구했다. 재실행은 복구/추가 행 0개였다. JSON v3는 행 식별자를 보강하고 SQL gate는 실제 source node 수까지 대조하며 실패한 삽입을 savepoint로 되돌린다. 문맥을 찾지 못한 숫자는 명시적 unknown evidence로 보존하고 격리하며 완전 추출로 채택하지 않는다.

미지원/모호한 계정과 부족한 지표 입력은 전수 coverage 분모에 유지한다.

## 후속 최종 실측

| 항목 | 결과 |
|---|---|
| 대상 | 미국 13,251 + 한국 4,507 = **17,758개**, 대상 축소 없음 |
| 실제 SQL 공시 숫자 | **152,954행** — 최초 94,459행에서 증가; 재파싱/원본·정정·nil·미매핑 감사 이력 포함 |
| 실제 KIS 가격 | **14,972행** — 최초 8,587행에서 증가; 전체 기간 완료 수가 아님 |
| 표준 계정 | 원 source 기준 **1,792건**, 매핑 v1/v2 이력 **3,584행**. 이름·단위 연결이며 fiscal/회계/주식 기준 인증을 뜻하지 않음 |
| 실제 재무 계산 | 11 issuer의 등록 지표 462건 평가, 성공 **0건**, 입력 부족/모호함으로 unavailable. `derived_fact=0`, `vwap_daily_stat=0` |
| 전수 테스트 | **384 passed**, 별도 실제 PostgreSQL `backtest_test`. 소스 compilation/whitespace/로컬 인증정보 Git 유출 검사 통과 |
| 독립 코드 검토 | 추가 Alpaca/정규화/수집 및 node 복구·격리 보존까지 SPEC/QUALITY 승인 |
| 새 릴리스 | `217ce6f0-1641-460d-af55-e99e64439b14`, `release_query_v4`, `research_partial`; 전수 대상·원행/정정·매핑·decisions 고정. Production pointer 미활성화 |
| 실제 고정 조회 | 현재 observed source account **101,083건**, canonical **3,584행** 모두 qualification gap 노출. 2016 public cutoff source account **0건**으로 현재 값 소급 차단 |
| 전 릴리스 검증 | 봉인 4개, integrity error **0개**. 불완전 추출 gap은 해당 버전마다 보존하며 production 인증으로 승격하지 않음 |
| snapshot 백업·복원 | dump 약 **121 MB**, raw **171개**, 백업 약 **10.7초**. 새 임시 테스트 DB 복원 **20.5초**, 모델 28개/공시 수치 152,954/가격 14,972/파일 참조 일치 확인 |

당시 백업: `data/backups/20261008T021540Z-72a88437/manifest.json`. 같은 장치의 로컬 검증 사본이며 독립 재해 복구·WAL/PITR 보증은 아니다. 기존 봉인 버전과 원본 SQL은 유지한다. 별칭 확장 이후 현재 query reader는 v5 정책을 요구하며 다른 규칙 버전을 임의로 v5로 해석하지 않는다.

다음 입력·구현은 Alpaca 무료 키 설정 후 미국 실제 수집, KRX 연결 및 가격 대조, 검증된 기업행동/정확 VWAP sufficient statistics, 과거 상폐·티커/인덱스 구성·발표 이력, 실제 fiscal·주식 기준 및 공개 시각 증거다. 전수 백필·정상 품질 profile은 여전히 미완료다.

## 종목 코드·이름 SQL 매핑 확장 — 2026-10-08

- 기존 `security.id`를 유지하고 append-only `identifier_history`에 전수 **75,037행**을 등록했다. 미국 **47,854행**, 한국 **27,183행**, 대상 **17,758개 모두**에 별칭 assertion이 있다. 실제 재등록은 삽입 **0행**, 철회 **0행**이며 이번 실제 원천 연결 qualification gap은 0건이었다. 이것은 현재 관측 자료의 별칭 등록 coverage이며 과거 identity 인증이 아니다.
- `005930`, `A005930`, `삼성전자`, `Samsung Electronics`가 기존 삼성전자 보통주 ID `3664afb1-e648-5bee-9480-404e3fbd0401`로 연결된다. 삼성전자우는 `af1989dc-5180-5807-a241-366de94097f0`로 유지하며 BRK.A/BRK.B도 별개다. 이후 사용자 요청에 따라 `Samung Electronics` 오타 별칭은 활성 매핑에서 철회하고 재등록을 막았다. `5930`, 미등록 오타, 현재 별칭의 2016 경제적 유효일 조회는 unresolved다.
- 코드 선행 0·클래스 구두점을 보존하고 Unicode/case/공백을 버전 정책으로 정규화한다. namespace/시장/거래소·유효 구간·공개/수집/등록 cutoff를 적용한다. 모호한 회사명은 후보를 반환하고 가격·재무 조회는 unresolved/ambiguous를 전체 target으로 확대하지 않는다. 발행사 연결이 없는 재무 조회도 차단한다.
- 정정·철회·복구는 새 assertion으로 추가한다. 대상 밖 증권으로 정정한 전체 이력과 해당 증권/발행사 catalog도 봉인해 이전 연결이 되살아나지 않게 했다. SEC/DART source issuer 충돌, primary code 변경, 이름 필드 소실은 기존 source 별칭을 철회하며 earlier cutoff와 기존 릴리스는 보존한다.
- 새 릴리스 **`35a0571d-2c17-4711-9874-9fed42129de4`**, `release_query_v5`, `research_partial`. 별칭 정책·구현 hash·전체 alias membership과 catalog를 봉인했다. 실제 고정 조회에서도 다섯 삼성전자 표기와 보통주/우선주 분리를 확인했다. `security_identity`를 포함한 production 품질 profile은 여전히 false다.
- 최종 전체 **430 tests passed** — 별도 실제 PostgreSQL, migration downgrade/upgrade replay, 두 세션 동시 재실행, source qualification 전환, Unicode 긴 이름과 UTF-8 CLI 출력, frozen isolation/대상 외 정정 이력 포함. 독립 통합 및 catalog 검토 승인, compilation/whitespace 검사 통과.
- 기존 버전까지 봉인 **5개**의 실제 integrity error **0건**. 새 별칭을 포함한 snapshot 백업은 `data/backups/20261008T030745Z-d9974a14/manifest.json`, dump **141,480,215 bytes**, raw **171개**, 백업·checksum 검증 **12.34초**다. 새 임시 PostgreSQL DB로 **24.88초**에 복원해 모델 **28개** 전체 행 수(별칭 75,037 포함)와 raw 참조가 일치했다. 같은 장치의 로컬 사본이며 독립 재해 복구·WAL/PITR 인증은 아니다.

실측 근거는 로컬 `artifacts/security-alias-{seed,replay,live-probe,frozen-probe,release}.json`에 있다. [별칭 사용·시점 계약](security-aliases.md). 과거 티커 재사용·명칭 변경·상장/상폐의 공식 유효 이력과 정기 directory 재수집 운영은 후속 작업이다. 현재 별칭을 과거로 소급하지 않는다.

## 후속 요청: 오타 철회와 데이터 수집 현황 — 2026-10-08 14:06 KST

사용자가 제거를 요청한 `Samung Electronics`는 기존 SQL을 덮어쓰지 않고 withdrawal 1행으로 철회했다. 자동 별칭 등록에서도 제외했고, 이후 재실행 시 되살아나지 않는 테스트를 추가했다. 기존 봉인과 이전 cutoff의 감사 이력은 유지한다. 현재 대상은 계속 17,758개다.

철회 이력을 포함한 새 연구용 릴리스는 `852662e7-baab-4a94-9676-60bfbc2cacc2`, `release_query_v5`다. alias history는 75,038행이며 원래 acceptance와 새 withdrawal을 모두 포함한다. 전체 테스트 **431 passed**를 확인했다.

| 데이터 | 실제 보유 상태 | 완료 여부 |
|---|---|---|
| 현재 종목 목록·별칭 | 미국 13,251 + 한국 4,507 대상 등록 | 현재 directory 범위 완료, 상품 분류·과거 lifecycle은 미검증 |
| 한국 가격·거래량 | 75개 증권, 14,972 observations | 일부 수집, 전수·전 기간 완료 아님 |
| 미국 가격·거래량 | 0행, Alpaca 키 미설정 | 어댑터·13,251개 대기 작업은 있으나 실제 수집 미시작 |
| 재무 원계정 SQL | 한국 20·미국 11개 발행사, 합계 152,954행 | 추출·정정/재파싱 이력 포함, 전수 수집·PIT 적격성 미완료 |
| 표준 재무·계산 지표 | 매핑 이력 3,584행, derived 결과 0행 | 실제 ROE/성장률/밸류에이션 등 미산출 |
| 기업행동·수정주가 | 실제 기업행동 관측 0행 | 검증된 분할·배당 등의 이력과 대조 공급자 필요 |
| 정확 VWAP | 적격 daily statistics 0행 | 대상·거래범위가 맞는 입력 데이터 필요 |
| ETF/ETN 상품별 펀더멘탈 | fund_product_fact 0행 | NAV·보유종목 등 상품별 입력 미수집 |
| 지수 값·과거 구성, 상장/상폐 이력 | 계획과 unavailable 경계 | 수집 공급자와 과거 유효 이력 미구축 |

SQL 확인 시 만료되지 않은 수집 lease는 **0개**였고 작업 큐에는 pending/retry가 남았다. 현재 전체 데이터가 상시 자동으로 수집되는 상태가 아니다. KIS/OpenDART/SEC 설정은 있지만 미국 가격 키, 기업행동·VWAP·지수 등의 추가 공급자/입력 연결이 필요하다. 다음 우선순위는 미국 가격 연결, 가능한 한국 가격·SEC/DART 전수 백필, 기업행동과 가격 대조, PIT·fiscal/share 기준 인증 및 VWAP·지수 이력 확보다.

현황 근거: `artifacts/current-data-status.json`, `artifacts/current-data-coverage.json`. SQL 원계정 행 수와 현재 종목 등록 수를 완전한 clean dataset coverage로 해석하지 않는다.

## KIS 미국 연결·재무 재개·한국 가격 복구 — 2026-10-09

현재 대상과 출력 기간은 17,758개(KR4,507/US13,251), 2016-01-01~2026-10-07이다. 공식 KIS NAS/NYS/AMS 마스터를 보존해 미국 12,283개를 정확히 매핑하고 968개는 미매핑으로 남겼다. KIS 미국 원주가·거래량 수집을 실제로 시작했으며 한국과 미국은 인증·호출 예산을 공유한다. 짧은 페이지도 명시적 경계 또는 빈 응답까지 계속 확인한다. 현재 마스터를 과거 종목 정체성으로 소급하지 않는다.

2026-10-09 12:48 KST SQL 점검에서 미국 가격 91,275행/965종목, 한국 가격 7,469,739행/4,446종목을 확인했다. 재무 원계정은 한국 2,839,303행/1,639발행사, 미국 476,082행/171발행사다. 이는 진행 중인 관측 수이며 해당 종목의 전 기간 완료를 뜻하지 않는다. 최신 수치는 `artifacts/collection-watch/latest-sql.json`을 따른다.

한국 과거 가격 검증 거부 259,810건은 모두 명시적 거래량·거래대금 0과 동일 양수 OHLC를 가진 공급자 보고였다. 이를 `supplier_reported_unverified` 등급으로 구분했고 실제 거래정지·무거래 상태나 exact VWAP는 인증하지 않는다. immutable 아카이브 복구는 원래 received attempt의 hash·정확한 요청·수집 시각과 SQL 검증 실패의 증권 정체성을 확인한다. 검증 결정과 replay receipt는 새 행으로 추가한다. 기존 관측과 실패 이력은 수정하지 않는다. 복구를 모두 검증한 작업만 보존한 날짜 커서에서 재개한다.

OpenDART가 공지한 2026-10-08 20시~10-11 18시 KST 점검 중에는 XBRL 단계의 800을 별도 보류하고 다른 JSON·SEC 수집을 계속한다. JSON 작업은 XBRL 저장 전에도 발견한 공시의 정확한 기업·접수번호·제목으로 계획한다. 과거 공시 요청에 최신 정정 접수번호가 반환되면 채택하지 않는다. 공급자 800/900·호출 제한은 재시도 상태로 보존하며 clean coverage로 간주하지 않는다.

수집은 앱 세션에서 분리한 Windows WMI wrapper와 도메인별 파일 잠금으로 실행한다. 초기 프로세스 신원·자식 PID·종료 코드는 `artifacts/kr-full-workers.json`이 가리키는 실행별 파일에 남긴다. 재무 코드 교체를 위한 운영자 종료는 기존 실행의 `operator-stop.json`에 기록했다. 앱 업데이트 자체를 재현한 내구성 검증은 아직 하지 않았다. 기존 2시간 자동감시는 한국·미국과 아카이브 복구를 읽기 전용으로 확인하며 의미 있는 변화만 알린다.

재실행 가능한 아카이브 복구 명령은 다음과 같다. 같은 작업을 중복 실행하지 않으며 완료 receipt를 검증해야 수집 작업이 requeue된다.

```powershell
.venv/Scripts/python.exe -m backtest_data maintenance replay-kis-prices --audit-path artifacts/kr-price-validation-audit.json --progress-path artifacts/kr-price-recovery-progress.json --budget-seconds 60 --requeue-recovered
```

KRX 대조 연결, Alpaca 추가 연결, 기업행동·검증된 수정주가·정확 VWAP·지수 이력·상폐 역사·상품 정보·완전한 PIT/재무비율 적격성은 계속 미완성이다. 전체 데이터가 100만 evidence 행 제한을 넘으므로 전수 릴리스 빌드는 기존과 같이 실패하며, 스트리밍 봉인은 별도 구현이 필요하다.

최종 회귀 검증은 실제 격리 PostgreSQL에서 **659개 테스트 통과(34.06초)**다. 독립 검토에서 발견한 미국 미매핑 재시도, 공급자 마스터 원본 closure/hash, 공유 KIS 제한 처리와 한국 복구의 증권 정체성·복수 원본 이벤트 검증을 수정했다. 복구의 첫 60초 운영 실행에서 **4,317행**이 추가됐고, 실제 SQL 표본 100행의 원래 capture 보존·0거래 공급자 등급·현재 검증 시각을 대조했다. 남은 복구는 동일 체크포인트를 앱 세션에서 분리한 worker로 이어 실행하며 전체 검증 후에만 한국 커서를 재큐한다. 프로세스와 복구 경로는 최신 메타데이터의 `auxiliary.price_recovery`를 따른다.

2026-10-09 13:00 KST 인계 SQL 점검은 한국 가격 **7,481,181행/4,456종목**, 미국 가격 **212,495행/2,244종목**, 한국 원계정 **2,859,311행/1,653발행사**, 미국 원계정 **533,793행/188발행사**다. 계속 실행 중이며 이 점검 이후에도 증가한다. 한국 복구·가격 수집·재무 수집의 실제 자식 프로세스와 SQL 진전을 확인하고 추가 코드 작업을 멈췄다. 2시간 감시는 읽기 전용으로 계속하며 정상 진행마다 알림을 보내지 않는다. [OpenDART 점검 공지](https://opendart.fss.or.kr/)에서 XBRL 등 특정 경로의 중지와 다른 API 사용 가능 범위를 확인했다.

## 보류 작업의 전체 기간 재조회 — 2026-10-09 15:18 KST

사용자가 커서 복원 대신 원래 기간 전체를 재조회하도록 요청했다. 한국 커서 없는 51개와 재큐 당시 미국 보류 18개만 2016-01-01~2026-10-07의 시작부터 다시 확인하도록 reset/requeue했다. 미국 보류 작업은 초기 알림의 4개에서 증가한 상태였다. 이미 저장한 관측·검증 실패·시도 횟수와 원래 요청은 유지하고 이전 커서·거부 집계는 `prior_traversal`에 보존한다. 새 조회의 커서·페이지 토큰·요청 anchor와 집계만 초기화한다. 미매핑 968개·다른 종목·실행 중인 작업은 변경하지 않았다.

`request_repair(..., job_ids=[...])`로 명시적인 범위를 제한하며 빈 목록도 전체 수리로 확대하지 않는다. 별도 수집기를 시작하지 않고 신원을 확인한 기존 가격 collector에 맡긴다. PostgreSQL 회귀 테스트 20개를 통과했고, 실제 SQL에서 한국 51개가 재조회 후 `succeeded`, 미국18개가 `retry`인 것을 확인했다. 원래 기간과 이전 체크포인트 보존도 모두 일치했다. 이것은 수집 작업 처리 상태이며 전 기간 clean/PIT coverage 인증이 아니다.

원래 작업 ID·요청·체크포인트·시도 횟수와 재조회 범위는 `artifacts/full-period-requery-20261009.json`에 기록했다. 기존 2시간 읽기 전용 자동감시가 해당 작업 상태도 확인한다. 같은 입력 부족이 재발하면 미완성으로 유지하고 자동으로 반복 재시작하지 않는다.
