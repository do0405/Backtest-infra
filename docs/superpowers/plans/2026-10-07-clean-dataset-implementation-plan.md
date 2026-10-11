# Clean Dataset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 한국·미국 전 종목(ETF·ETN 포함)의 가격·거래량과 유용한 펀더멘탈 값을 SQL로 저장하고 정정/PIT·수정가격·기간/Anchored VWAP·과거 모집단을 제공한다.

**Architecture:** PostgreSQL을 기준 저장소로 사용한다. SEC/OpenDART는 문서의 계정 값·문맥·공시 정보를 추출해 SQL에 저장하고, 원문은 임시 파싱 입력으로 다룬다. Python 수집·검증·선택을 분리하며 릴리스/as_of를 고정한다. Django는 필수 의존성이 아니다.

**Tech Stack:** Python, PostgreSQL, SQLAlchemy, Alembic, psycopg, pytest, 배치 SQL. 실제 버전은 Windows smoke test 후 lock한다.

---

## 문서와 실행 범위

- 설계: [Clean Dataset 설계](../specs/2026-10-07-clean-dataset-design.md)
- [A. 공통 기반과 공급자 적격성](2026-10-07-clean-dataset-foundation-plan.md)
- [B. 가격·거래량·기업행동·VWAP](2026-10-07-clean-dataset-prices-plan.md)
- [C. 펀더멘탈 PIT](2026-10-07-clean-dataset-fundamentals-plan.md)
- [D. 과거 모집단·릴리스·운영](2026-10-07-clean-dataset-release-plan.md)
- [E. 품질·정정·복구·전수 운영 보강](2026-10-07-clean-dataset-operations-plan.md)
- [운영 부족점과 개선 검토](../../data/2026-10-07-clean-dataset-operations-review.md)

이 요청의 산출물은 계획 문서다. 아래 체크박스는 구현 시 수행할 작업이며 현재 완료했다는 표시가 아니다. 현재 폴더는 애플리케이션/Git 저장소가 없는 상태다. 실행 시 A부터 초기화하고 B/C는 공통 계약을 고정한 다음 구현한다. D의 release 발행은 B/C의 테스트와 공급자 적격성에 의존한다.

## 확정 결정

1. 한국 KOSPI·KOSDAQ·KONEX, 미국 거래소 상장 전 종목과 ETF·ETN의 일봉. 무료 출발/정확도 우선.
2. 가격 원본/재무 SQL source fact -> 공급자별 관측 -> 검증/선택 -> immutable release -> PIT 조회. SEC/OpenDART 장기 문서 archive는 만들지 않는다.
3. PostgreSQL + Python + SQLAlchemy/Alembic. DB와 raw 파일을 함께 백업한다.
4. raw/as-traded/split-adjusted/total-return를 구분하고 원본을 덮어쓰지 않는다.
5. 정확 VWAP는 동일 집합의 가격×수량 합계와 대상 거래량으로 계산한다.
6. 역사 재구성과 당시 시스템 replay를 구분한다.
7. 지수 구성 이력은 과거 모집단 지원용으로 포함한다.
8. 참조 프로젝트에서 수집/검증 개념을 가져오되 live 스크리닝·대시보드와 rolling retention은 가져오지 않는다.
9. ROE·ROA·마진·성장률·FCF·PER·PBR·EV/EBITDA를 초기 파생 범위에 포함한다. [정의/예외/PIT 계약](../../data/financial-metric-catalog.md)을 버전으로 고정하고 SQL에 계산 근거를 보존한다.
10. 출력 시작일과 계산 입력 시작일을 분리한다. 첫 날짜에 필요한 과거 분기·평균 잔액·주식 수/VWAP 선행 입력을 수집하며 없으면 해당 지표를 미계산으로 공개한다.
11. 매출·EPS·영업이익 성장률을 각각 QoQ·YoY·TTM YoY로 필수 제공한다. EPS basic/diluted를 분리하며 정의와 명시적 SQL metric_id는 지표 계약을 따른다.
12. 전수 운영 전 품질 profile, lease/fencing·공통 quota, 과거 정정 재검증, SQL 추출 commit gate, 불변 segment 공유, 독립 백업·복원 검사를 통과한다. annual-only 기업은 별도 연간 지표를 제공하되 strict TTM 정의를 바꾸지 않는다.

## 실행 순서와 완료 기준

| 단계 | 작업 | 완료 기준 |
|---|---|---|
| A1 | 개발·테스트 환경, SQL 마이그레이션 | 깨끗한 PostgreSQL에 upgrade/downgrade/re-upgrade 성공 |
| A2 | 원본·수집 작업·체크포인트 | 중복 수집, 재시작, 파일-DB 무결성 테스트 성공 |
| A3 | identity·출처·거래 달력 | 티커 재사용/이력/단위/휴장·DST 계약 통과 |
| A4 | 실제 공급자 표본·권한·기간·기준 조사 | 계약별 pass/partial/fail과 증거 hash, 지원 범위 확정 |
| A5 | 전 종목 마스터와 작업 집합 | 현재/과거 전체 대상, ETF·ETN을 등록하고 종목별 필수 작업 전수 생성 |
| B1 | 전 종목 가격·거래량 관측 | 단위/세션/수정 기준 판별, 모든 대상의 확보/미확보를 집계 |
| B2 | 기업행동·조정 가격 | 분할/배당/기준/가시성 테스트 통과 |
| B3 | 기간·Anchored VWAP | matched 분모/결측/anchor 테스트 통과 |
| B4 | 가격 검증·선택 | 상충 원천/기업행동/PIT 선택 근거 보존 |
| C1 | 재무 SQL schema | 원공시 receipt/accession, 계정·문맥·숫자 보존 계약 |
| C2 | 공시 discovery·계정 SQL 추출 | 최초/정정·custom/주석 값과 실패 checkpoint 연결 |
| C3 | canonical·분기·TTM | 호환 기간/단위/범위와 파생 가용 시각 검증 |
| C4 | 재무 PIT 선택·검산 | 최초/정정 및 당시 채택·계산 시각 회귀 통과 |
| C5 | ETF·ETN 상품형 펀더멘탈 | NAV/지표가치·규모·비용·분배 등 해당 상품 값을 SQL화 |
| C6 | 수익성·성장·현금흐름·가격 배수 | 평균 잔액, 주당 기준, 시가총액 구성, 비정상 분모와 PIT 회귀 통과 |
| D1 | 과거 모집단·지수 | baseline+이벤트를 증명한 구간만 membership 조회 |
| D2 | 통합 데이터 릴리스 | 입력 불변성·미래정보 차단·coverage·복원 테스트 통과 |
| D3 | SQL 조회·통합 검증 | 고정 release/cutoff/profile과 버전별 재현성 |
| D4 | 전체 백필/증분/정정/복원 | 종목별 실패 격리와 resume, 운영 완료와 품질 완료 분리 |
| E1/E2 | 품질·freshness·작업 소유·정정 발견 | query gate, stale worker 거부, 공통 quota와 과거 검증 |
| E3/E5 | segment 공유·재계산·이슈 처리 | 같은 이력 재사용, 새 결정 이력과 기존 release 불변 |
| E4 | 추출 commit gate·공시 profile·연간 지표 | SQL inventory 검증, 최초 실적 공시 범위와 TTM/연간 구분 |
| E6/E7 | 복구·전수 상태·용량 | 독립 사본 복원 및 실제 전수 성능/비용 측정 |

완료 기준 기반 계획이다. 구현 기간과 전 시장 다운로드 시간은 표본 측정 전 확정하지 않는다. API 권한/기간/역사 버전이 확보되지 않으면 해당 계약을 지원하지 않는 결과도 정상 조사 산출물이다. 전체 수집은 source별 승인 범위만 진행한다.

E는 기존 task의 단계별 보강이다. A3 뒤 E1의 계약 함수와 E2의 lease/rate budget을 먼저 만들고 A4 probe에 사용한다. E2 historical scan은 B/C 어댑터가 생긴 뒤 연결한다. E1 query 통합과 E3/E5는 D2/D3 단계에서 완성한다. E6/E7 검증 뒤 D4 production을 활성화한다. 서로 아직 없는 CLI/모듈을 선행 import하는 순환 의존성을 만들지 않는다.

## 공급자 우선순위와 필요한 증거

| 도메인 | 1차 후보 | 비교/보완 후보 | 승인에 필요한 확인 |
|---|---|---|---|
| US 가격/volume | Alpaca 과거 SIP, raw 기준 | Massive, Yahoo 별도 경로 | 실제 권한/2016 이후 요청/상폐·티커/세션/정정/가격 기준 |
| US 정확 VWAP | matched 일별 통계 또는 SIP 체결 집계 | 계약 검증된 공급자 통계 | 일반 volume과 VWAP 분모 관계, 조건/취소/정정 포함 범위 |
| KR 가격/volume/대금 | KRX 확인 가능한 원천 | KIS raw 요청, Yahoo 비교용 | 날짜별 대금/volume 단위·거래 범위, 엔드포인트 조정 옵션 |
| US 재무 | SEC 공시 이력+accession 원문 | companyfacts/iXBRL | 원정정 버전, concept/context, 시점 근거 |
| KR 재무 | OpenDART receipt 원문/XBRL | 전체 재무 API 보완 | 원공시 receipt 대응, 정정 전 버전, 연결/별도 |
| ETF·ETN 펀더멘탈 | KRX/상품 제공기관·관련 SEC 공시 | 승인된 상품 정보 API | 상품/발행사 구분, NAV/지표가치·규모·비용·분배의 시점 |
| 지수 | 지수 제공기관/KRX 자료 | 제공기관 변경 공지/검증된 이력 | 역사 baseline, 발표/효력, 비중·방법론·수익률 종류 |

API 문서 읽기와 실제 권한 테스트는 다른 증거다. 공급자에서 기본 feed/asof/adjustment를 자동 선택하도록 두지 않는다. 현재 API 인증 샘플은 미검증이다.

## 디렉터리 책임

~~~text
src/backtest_data/
  cli.py                     하나의 collect/validate/release/query/maintenance CLI
  config.py, db.py            설정과 DB transaction
  catalog/                   종목·발행사·출처 계약·거래 달력
  ingestion/                 원본 파일, 작업/재시도/워터마크
  quality/                   품질 profile·freshness·조회 gate·이슈/수동 결정
  ops/                       schedule policy·상태·용량·소스 변경 감지
  prices/                    공급자 관측, 가격 검증, 기업행동, VWAP
  fundamentals/              재무 계정 SQL 추출, 회계 문맥, 정규화/파생/PIT, ETF·ETN 값
  indices/                   구성 baseline/변경/비중/지수 값
  releases/                  결정·릴리스·coverage·조회·복원
config/                      계약/매핑/표본 및 모집단 정책
alembic/versions/             단계별 스키마
tests/unit/, integration/    네트워크 없는 회귀와 실제 PostgreSQL 검사
tests/fixtures/              합성 사례와 허용된 최소 공급자 응답
data/raw/                    보존하기로 한 가격 등 공급자 원본
data/staging/                SEC/OpenDART 임시 파싱 입력(commit 후 정리)
artifacts/source-qualification/ 공급자 승인 근거
~~~

## 검증 원칙

- 테스트는 실제로 틀릴 수 있는 시점/기업행동/파싱/조회/복원 계약에 집중한다.
- 공급자 테스트 기본값은 네트워크 없는 fixture다. 실제 API 조사 명령을 명시적으로 실행해 증거를 보존한다.
- PostgreSQL integration 테스트는 SQLite로 대체하지 않는다. 필수 DB 검사에서 DSN이 없으면 skip이 아니라 실패다.
- 각 작은 변경은 failing test -> 최소 구현 -> targeted test -> commit 순서로 진행한다. lock/config/docs-only 변경에 무의미한 테스트를 만들지 않는다.
- Git identity는 사용자 기존 설정을 사용한다. 글로벌 설정을 임의 변경하지 않는다. Git을 사용할 수 없으면 그 이유와 코드 content hash를 기록한다.
- 내부 테스트 사례에는 정상, 분할/병합, 배당, 티커 변경, 상폐, 거래 정지/누락, 재무 정정, fiscal 변경, ETF/ETN, 지수 편출입을 포함한다. 이 사례들이 production 대상 종목 목록을 제한하지 않는다.
- correctness gate는 무결성/시점/조정/lineage 위반을 0건 요구한다. coverage 공백은 따로 공개하고 기능별 eligible mask를 만든다.

## 최종 구현 검증 명령

아래 CLI와 pytest 옵션은 각 하위 계획에서 구현하는 인터페이스다. 현재 존재한다고 가정하지 않는다.

~~~powershell
python -m pytest tests/unit -q
python -m pytest tests/integration -q --require-postgres
python -m backtest_data validate --scope all --release-id <실제발행UUID>
python -m backtest_data query smoke --release-id <실제발행UUID> --market both
python -m backtest_data maintenance verify-backup --backup-manifest <실제백업manifest경로>
~~~

실행에서는 실제 발행 UUID/manifest를 명령 출력에서 받아 사용한다. smoke 결과에는 요청/실제 기간, 가용성 모드, 누락 종목/세션/항목, 정확 VWAP 가능 범위, source 증거 등급을 표시한다.

## 전달 대상과 전수 완료 기준

첫 수집 대상과 최종 전달 대상 모두 한국·미국 전 종목 및 ETF·ETN이다. 먼저 전체 마스터/종목별 작업 집합을 생성하며 현재 목록과 요청 기간의 종료 종목 이력을 함께 조사한다. source 권한/요청 속도 때문에 처리 배치를 나눠도 전체 대상은 유지한다.

target_universe_manifest를 분모로 가격, volume, 기업형/상품형 펀더멘탈, 정확 VWAP의 성공/미확보/실패/not_applicable을 전수 공개한다. 모든 작업이 terminal인 것과 모든 필수 값이 확보된 것은 별개다. 적용 가능한 항목의 누락이 남거나 역사 마스터가 미완료면 전체 완료라고 선언하지 않는다. 부분 릴리스는 중간 결과다. 펀더멘탈은 전체 유용 source 계정과 canonical 매핑을 모두 SQL로 제공하며 정정 전후 기록을 보존한다.
