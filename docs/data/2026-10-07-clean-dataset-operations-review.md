# Clean Dataset 운영 계획 점검과 개선안

작성일: 2026-10-07. 대상: 기존 설계·19개 구현 작업·재무 지표 계약. 점검 결과 E1~E7을 추가해 총 26개 작업으로 보강했다. 코드/DB/인증 API/실제 운영이 아직 없으므로 아래는 계획의 부족점과 구현 승인 조건이다. 실데이터 오류나 운영 성능을 확인했다는 보고가 아니다.

## 판단

기존 계획은 전 종목 모집단, SQL 재무 계정/정정, 원주가와 수정 기준, PIT와 릴리스 불변성의 방향을 잡았다. 그러나 장기 전수 운용에는 품질 판정의 실행 규칙, 과거 정정 재검증, 동시 작업 제어, 릴리스 저장 비용, 추출 누락 복구, 재해 복구 기준이 부족하다. 구현 착수 전에 아래 P0 항목을 공통 계약으로 넣는다.

변경 경계는 source contract -> ingestion -> validation/selection -> release/query -> maintenance다. 거래 전략·주문/포트폴리오 엔진, Django UI, 외부 알림, 유료 구독 구매는 이번 개선의 책임 밖이다. 추가 작업은 [운영 보강 계획](../superpowers/plans/2026-10-07-clean-dataset-operations-plan.md)에 정의한다.

## 발견사항과 개선

| ID | 우선순위 | 부족한 점 / 영향 | 개선 / 검증 |
|---|---|---|---|
| O01 | P0 | accepted와 coverage 상태는 있지만 언제 조회·발행을 막을지, freshness를 무엇과 비교할지 구체적이지 않음 | 데이터 계약과 기능별 query gate, latest-completed-session/공시 발견 지연/정정 미처리를 분리. stale/partial/unsupported를 임의 usable로 바꾸지 않음. E1 |
| O02 | P0 | 최근 overlap 갱신만으로는 오래된 가격/분할 수정과 SEC post-acceptance 수정·삭제를 놓칠 수 있음 | 일일 overlap + 회전 역사 검증 + event-triggered repair. 같은 공시 identity의 변경 hash에도 새 version/정정 가용 시각 적용. 정정 시각 미확보는 detection 기반이며 원공시일로 소급 금지. E2 |
| O03 | P0 | supervisor/resume가 worker lease·중복 실행·계정 단위 rate limit을 정의하지 않음 | PostgreSQL lease/fencing, source/account 공통 rate budget, Retry-After/backoff, live delta와 backfill 자원 분리. 중단 후 전수 pending 유지. E2 |
| O04 | P0 | SEC/DART 문서를 commit 후 정리하면 추출 누락을 나중에 다시 읽기 어려움 | SQL source inventory/문맥/값/원표기/precision·추출 검산을 commit gate로. 재파싱 시 hash 변경과 재수집 실패를 기록하고 최초 SQL 관측을 수정하지 않음. E4/E5 |
| O05 | P1 | 매 릴리스 전체 역사의 회원 행과 모든 역사 배수를 복제·재계산하면 저장/작업량이 누적 폭증할 수 있음 | 공유 immutable data segment, 변경 chunk만 새로 생성. 의미 입력 hash 기반 파생 재사용과 영향 범위 재계산. SQL·역사 기록은 보존. E3 |
| O06 | P0 | 정기 보고서만 수집하면 실적 발표일과 공시 보고서일 사이의 정보 가용성을 설명하지 못함 | periodic_report와 earliest_ingested_disclosure를 구분. 승인한 SEC 8-K/6-K·KR 잠정 실적 경로의 숫자를 SQL로 추출하고 GAAP/IFRS/non-GAAP와 assurance를 구분. E4 |
| O07 | P1 | 직접 보고 4분기 EPS만 요구하면 annual-only/분기 EPS 미확보 종목의 PER/성장률 coverage가 낮아질 수 있음 | strict TTM 정의 유지, 직접 보고 연간 EPS/연간 성장률은 별도 metric_id/basis로 추가. 분기 EPS를 연간 EPS 차감으로 만들지 않음. 실제 reporting frequency/미계산 이유 공개. E4 |
| O08 | P0 | backup/restore는 있지만 동일 시점 DB+파일, 다른 장치 사본, RPO/RTO와 실제 복구 시간이 명확하지 않음 | 일관 snapshot과 immutable 파일 hash manifest, 다른 장치 백업, 복원 drill. base backup+WAL 사용 시 SQL/raw 공통 복구 horizon도 검증. SEC/DART 문서 없이 SQL 재무 복원. E6 |
| O09 | P1 | 정정 이슈가 생겼을 때 재수집/매핑 수정/수동 override/재발행의 순서가 부족함 | issue ledger -> 원인 분류 -> evidence 기반 변경 -> 영향 조회 -> 새 revision/release. 근거/actor/time을 보존하고 원값 UPDATE 금지. E5 |
| O10 | P1 | 미국·한국을 같은 날짜로 묶는 운영 cut, source entitlement/정의 변경, FX/주식 수 입력 나이가 충분히 드러나지 않음 | global cutoff와 시장별 마지막 완료 session, disclosure profile과 policy 버전을 고정. 최신 FX/주식 수의 age와 estimate 여부 노출. E1/E7 |
| O11 | P1 | 전수 성능·디스크 증가·API 소요·운영 상태의 측정/경보 기준이 없음 | 전수 work denominator, 429/020/401/coverage drift/새 unmapped account/디스크·WAL·backup age/조회 p95 보고. 단계별 부하와 비용 budget 실측 후 확정. E7 |

## 우선 적용할 운영 계약

- release profile: core_price_volume, fundamentals_sql, exact_vwap, valuation, index_membership. 모든 profile은 전 종목 마스터를 유지한다. 한 feature가 미확보라고 해당 종목을 전체 대상에서 빼지 않는다.
- source 계약 승인, row validation, semantic mapping, feature 계산 가능, freshness, 역사 membership 완전성은 서로 다른 상태다.
- 운영 완료와 source extraction 완료, canonical mapping 완료, feature별 coverage 완료는 별도 보고한다. unsupported_required는 not_applicable로 재분류해 완료율을 높이지 않는다.
- UTC global_as_of에 대해 US/KR의 eligible 마지막 session을 각각 사용한다. 이미 끝난 시장만 새로 갱신됐다는 이유로 다른 시장의 미래 session을 섞지 않는다.
- consumer는 release_id/as_of/history_mode와 required_profile을 명시한다. 부분/오래된 자료를 허용하려면 explicit research option과 결과 flag가 필요하다.

초기 운영 목표는 거래일 price close 후 60분 이내 source 확인, 공시 discovery 최대 60분 간격, 명시적 close/delay 정책이다. 이 값은 source의 공개 가능 시각을 만들어내는 규칙이 아니라 내부 지연 목표다. A4 측정·쿼터와 서비스 접근 시간에 맞춰 버전으로 확정한다. 달력의 휴장/DST를 사용하고 운영 알림은 우선 로컬 status/issue 기록으로 제공한다.

## 원문을 장기 보관하지 않는 재무의 복구 범위

SQL에 저장된 숫자·문맥의 조회/매핑 변경/파생 재계산은 DB로 재현한다. 저장하지 못한 새로운 문서 필드의 복구는 upstream 재수집 가능 여부에 의존한다. 따라서 문서 제거 전에 numeric/context inventory, source 값 합계·기간·단위, 저장 row count/hash와 예외 목록을 검증한다.

문서 전체 archive를 추가하지 않는다. 재수집 문서 hash가 달라지면 새 document metadata/source facts를 저장한다. hash가 같아도 새로 발견한 fact는 실제 추출 시각을 기록하며 당시 시스템이 알았던 것으로 소급하지 않는다. inventory 불완전은 extraction_complete=false로 남는다.

파서와 같은 경로의 count/hash 일치만으로 원문 전체의 추출 완전성을 증명할 수는 없다. 지원 포맷별 독립 계정/node 검산과 fixture를 사용하고, 미지원 비구조화 주석 숫자는 coverage에 명시한다. 문서 미보관으로 인해 재수집 불가능한 미추출 필드는 복구할 수 없다는 범위도 운영 계약에 남긴다.

## 전체 시장 운용의 외부 의존성

미국 SIP/상폐·티커 이력, 한국 KONEX/상품 이력, 정확 VWAP 충분 통계/체결 권한, 전체 재무/주석·상품 profile의 과거 범위는 실제 API로 아직 검증하지 않았다. 현재 공식 디렉터리는 역사 전체 목록의 증명이 아니다. 필요 데이터가 무료로 부족하면 미완료 원인과 보완 후보를 구체적으로 보고한다. 이 계획은 과거 전체의 완전성이나 모든 metric의 전 종목 계산 가능성을 미리 인증하지 않는다.

## 구현 순서

A1~A5와 E1/E2 공통 계약 -> B/C 및 E4 추출 품질 -> D2/D3와 E3/E5 -> E6/E7 복원·부하·운영 승인 -> D4 전수 production 활성화.

source probe와 전체 manifest는 초기부터 전수 대상 계획을 유지한다. 내부 fixtures/부하 subset은 수집 대상 제한이 아니다. 영구적인 전수 운영을 시작하기 전에는 lease/rate limit/immutable segment/SQL 추출 commit gate/복원/필수 profile 검사를 통과한다.

## 근거

- [SEC 접근 정책](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data): 현재 rate limit, index 갱신과 post-acceptance 수정/삭제가 존재한다. 문서 확인일 2026-10-07.
- [SEC quarterly disclosure 변경](https://www.sec.gov/newsroom/press-releases/2020-290): 분기 표 disclosure 규칙이 변경됐으며 직접 분기 자료를 모든 issuer에 가정하지 않는다.
- [SEC 실적 발표 공시 예시](https://www.sec.gov/Archives/edgar/data/932696/000093269626000067/0000932696-26-000067-index.htm): 정기 보고서와 별도 실적 공시 경로 조사 근거.
- [PostgreSQL pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html), [지속 WAL 보관/PITR](https://www.postgresql.org/docs/18/continuous-archiving.html): 일관 백업과 특정 시점 복구 방법. 내부 RPO/RTO는 별도 목표/실측이다.
