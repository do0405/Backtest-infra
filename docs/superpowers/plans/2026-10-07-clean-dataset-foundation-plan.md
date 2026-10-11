# Clean Dataset Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 다른 도메인이 의존할 PostgreSQL·원본·identity·달력·수집 계약과 실제 공급자 적격성 증거를 만든다.

**Architecture:** 범용 catalog/ingestion만 이 단계가 소유한다. 공급자 계약은 원본을 식별하지만 가격/재무 값을 채택하지 않는다.

**Tech Stack:** Python, PostgreSQL, SQLAlchemy, Alembic, psycopg, pytest.

---

설계: [Clean Dataset 설계](../specs/2026-10-07-clean-dataset-design.md). 선행 계획 없음.

## Task A1: 저장소·패키지·실제 DB 테스트 환경

**Create:** pyproject.toml, dependency lock, .gitignore, .env.example, compose.yaml, alembic.ini, alembic/env.py, src/backtest_data/__init__.py, __main__.py, config.py, db.py, cli.py, tests/conftest.py, tests/unit/test_cli_entrypoint.py, tests/integration/test_database_bootstrap.py.

- [ ] 현재 폴더와 Git 상태를 확인하고 새 프로젝트로 git init한다. Docker compose 사용 가능 여부와 기존 로컬 PostgreSQL DSN을 확인한다. Docker가 없으면 제공된 로컬 DSN을 사용하며 설치가 필요하면 실제 선택안을 제시한다.
- [ ] 빈 DB에서 transaction rollback, upgrade/downgrade/re-upgrade를 검사하는 failing integration 테스트를 쓴다. tests/conftest.py에 --require-postgres를 구현해 필수 검사에서 DB 미설정이 실패하도록 한다.
- [ ] src layout 패키지, settings, 하나의 engine/session factory, CLI, Alembic metadata를 만든다. __main__.py는 cli.main()을 호출한다. CLI 등록은 한 파일에서 소유하며 A4가 sources probe, B1/C2/D1이 collect domain handler, D3/D4가 validate/release/query/maintenance를 연결한다. .env.example은 변수 이름과 샘플만 포함한다. BACKTEST_DATABASE_URL과 BACKTEST_RAW_ROOT를 사용한다.
- [ ] python -m pip install -e '.[dev]', python -m backtest_data --help 및 python -m pytest tests/unit/test_cli_entrypoint.py tests/integration/test_database_bootstrap.py -q --require-postgres를 실행한다. 기대: CLI entrypoint exit 0, rollback과 migration 검사 PASS. 실제 Windows 지원 버전 조합을 lock한다.
- [ ] 변경 파일만 stage하고 git commit -m 'chore: bootstrap clean dataset storage'를 실행한다.

## Task A2: 불변 원문과 수집 작업 이력

**Create:** src/backtest_data/ingestion/models.py, raw_store.py, jobs.py; alembic/versions/0001_catalog_ingestion.py; tests/unit/test_raw_store.py; tests/integration/test_ingestion_resume.py.

- [ ] archive 대상 원문 중복 저장 방지, 변경 원문 분리, 중단된 write, 파일 없는 archive 참조, retry 후 재확인, 실패 종목의 pending 유지 테스트를 작성하고 FAIL을 확인한다. 재무는 structured_sql provenance로 파일 참조 없이 성공 가능한 계약을 추가한다.
- [ ] 원문 bytes를 SHA-256로 식별하고 압축/atomic rename 후 DB 참조를 commit한다. DB에는 raw root에 상대적인 content-addressed storage key를 저장해 복원 위치가 바뀌어도 identity가 같다. 동일 bytes는 raw_object를 재사용하고 ingestion_attempt는 별도 저장한다. 파일 없는 참조는 검증 실패다.
- [ ] job identity=(source_contract_version, domain, security/issuer, requested_range, request_basis)로 멱등 계획을 만든다. 발견 watermark와 종목별 성공 checkpoint를 분리한다. provider 제한/transport/no-data/quality/structural을 구분한다.
- [ ] E2의 SQL lease/fencing과 source/account 공통 rate budget을 A3 뒤 연결한다. A4 probe에도 같은 limiter를 사용하고 만료 worker의 checkpoint 갱신을 거부한다. historical scan의 domain 연결은 B/C 이후로 미룬다.
- [ ] python -m pytest tests/unit/test_raw_store.py tests/integration/test_ingestion_resume.py -q --require-postgres를 실행한다. 기대: 중단/재시도/중복 사례 PASS, 실패 작업이 조용히 완료되지 않음.
- [ ] git commit -m 'feat: preserve raw evidence and resumable ingestion' 전 변경 파일/원문 샘플에 자격증명이 없는지 diff를 확인한다.

## Task A3: 발행사·증권·상장·출처·거래 달력

**Create:** src/backtest_data/catalog/models.py, identity.py, sources.py, calendar.py; config/source_contracts.yaml, validation_samples.csv, universe_policy.yaml; tests/unit/test_identity_calendar.py; tests/integration/test_catalog_constraints.py.

- [ ] 티커 재사용, CIK 하나/증권 여러 개, 선행 0, 유효 기간 겹침, source timezone/DST, KR 휴장/예외, 미국 조기폐장 테스트를 쓰고 FAIL을 확인한다.
- [ ] issuer/security/listing/identifier를 분리한다. mapping은 경제적 valid_from/to와 공개/수집 시점을 가지며 version history를 보존한다. 근거 없는 mapping은 자동 연결하지 않는다.
- [ ] exchange_session에는 market/date/open/close/timezone/calendar_version과 원문/override 근거를 저장한다. market grid를 기준으로 결측과 휴장을 구분한다. 공식 예외로 calendar 버전을 갱신한다.
- [ ] source_contract는 endpoint별 원/수정주가, feed/venue/session, currency/unit, quantity unit, timestamp precision, upstream/정제 경로를 가진다. defaults를 암묵적으로 선택하지 않는다. schema/constraint를 A2 migration에 반영하되 이미 적용된 migration이면 새 revision을 만든다.
- [ ] python -m pytest tests/unit/test_identity_calendar.py tests/integration/test_catalog_constraints.py -q --require-postgres 실행 후 git commit -m 'feat: define temporal security and source identities'. 기대: mapping/달력 계약 PASS.

## Task A4: 문서 비교에서 실제 공급자 승인으로

**Create:** src/backtest_data/catalog/qualification.py, ingestion/probe.py; tests/unit/test_source_qualification.py; artifacts/source-qualification/manifest.json(운영 산출물); docs/data/source-qualification.md.

- [ ] 문서만 확인된 source, 권한 거부, 기간 제한, latest-only 재무, VWAP 분모 미확인, 동일 wrapper 두 개를 독립으로 오인한 사례를 fixture로 검사한다.
- [ ] cli sources probe는 source_contract의 allowlisted endpoint/명시적 파라미터로 응답을 받는 최소 HTTP probe다. 가격 archive는 raw_store, SEC/OpenDART는 임시 응답의 hash/필드/공시 메타데이터를 ingestion_attempt에 저장한다. 재무 문서 archive를 생성하지 않는다. B/C 어댑터를 선행 import하지 않는다. 결과에는 pass/partial/fail, entitlement, observed_bounds, supported_basis, vwap_capability, extraction_inventory, checked_at을 넣고 도메인 검증 후 최종 승인 증거를 추가한다.
- [ ] US 후보 Alpaca SIP(raw)와 비교 후보, KR KRX/KIS(raw), SEC accession, DART receipt를 실제 샘플로 검사한다. 계정이 없으면 requires_credentials를 기록하고 해당 source의 실데이터 승인 작업을 pending으로 둔다. fixtures로 API 승인을 대신하지 않는다.
- [ ] 전 종목 마스터 및 ETF·ETN 상품 정보 source의 형식/종목 지원 범위도 승인한다. 특정 source가 KONEX/우선주/ETF/ETN을 지원하지 않으면 해당 종목을 마스터에서 제거하지 않고 보완 source 또는 미확보 작업을 기록한다.
- [ ] VWAP은 matched 합계가 있으면 지원한다. 없으면 체결 endpoint의 필드/권한/취소·정정 범위를 별도 조사한다. 그마저 안 되면 정확 VWAP unavailable로 확정하고 일반 가격 domain은 독립 승인한다.
- [ ] python -m pytest tests/unit/test_source_qualification.py -q를 실행하고 docs/data/source-qualification.md에 선택/기각/보완 이유를 기록한다. git commit -m 'feat: qualify dataset sources with evidence'.

**실제 샘플 명령:** python -m backtest_data sources probe --source alpaca --market us --start 2016-01-01 --samples config/validation_samples.csv. 각 source를 명시해 실행한다. 기대: evidence manifest 경로와 계약별 승인/미지원 범위를 출력하며 비승인 source는 전체 수집에 사용할 수 없음.

## Task A5: 전 종목 마스터와 필수 작업 전수 생성

**Create:** src/backtest_data/catalog/universe.py, historical_universe.py, adapters/us_directory.py, adapters/krx_directory.py; ingestion/work_plan.py; tests/unit/test_full_universe.py; tests/integration/test_universe_work_plan.py; config/fundamental_inventory.yaml; docs/data/universe-coverage.md; alembic/versions/0001b_universe_workplan.py. **Modify:** catalog/models.py, ingestion/models.py.

- [ ] 전체 목록 중 unsupported/상폐/우선주/ETF/ETN이 누락되거나 첫 N개만 작업 생성되는 회귀를 쓴다. 동일 발행사의 여러 증권과 신규 편입/상품 종료도 검사한다.
- [ ] 한국 KOSPI/KOSDAQ/KONEX 및 ETF/ETN 공식 마스터와 미국 Nasdaq-listed/other-exchange-listed를 수집해 상품 유형을 보존한다. provider 자산 목록과 대조하고 unmatched/test issue/중복 listing을 근거와 함께 분류한다. 현재 전체 목록과 역사 전체 목록의 완전성을 별도 판단한다.
- [ ] historical_universe.py에서 승인 source의 요청 기간 상장/상폐·상품 종료·티커 이력을 최초 작업 계획 전에 discovery한다. 현재 디렉터리와 합쳐 target_universe_manifest에 모든 대상 ID/상품 종류/source/기간/lifecycle 근거와 미확보 범위를 고정한다. current/historical completeness를 분리하고 historical 미확보를 현재 목록으로 대체하지 않는다. D1 코드를 기다리지 않고 공통 discovery를 소유한다.
- [ ] 종목×가격/volume/VWAP/기업형 또는 상품형 재무×유효 요청 기간의 작업을 전수 생성한다. 주식 종류는 security별로 보존하고 동일 issuer 공시 요청은 공유해 중복 호출을 줄인다. 유효 listing 이전/이후와 근거 있는 not_applicable만 요구 집합에서 제외한다. failure/unsupported를 분모에서 빼지 않는다.
- [ ] requested_output_start와 dependency_input_range를 분리해 warmup_required 작업을 생성한다. declarative inventory에서 TTM/TTM YoY의 4/8분기와 평균 잔액/주식 snapshot, VWAP의 N세션/anchor를 지정하고 C2/B 수집기가 discovery 후 실제 범위를 확장한다. A5는 아직 없는 C6 계산 모듈을 import하지 않는다. lifecycle 이전 공시라도 상장 후 지표 입력으로 필요하면 issuer 작업에서 제외하지 않는다.
- [ ] python -m pytest tests/unit/test_full_universe.py tests/integration/test_universe_work_plan.py -q --require-postgres 실행 후 git commit -m 'feat: plan every listed security and product'. 기대: manifest의 모든 대상에 필수 work item이 있고 source 미지원 종목도 추적됨.

## A 완료 산출물

archive 파일/재무 SQL evidence를 구분한 공통 저장 구조, 거래 달력, 실제 적격성 matrix, 양 시장 전 종목+ETF/ETN target manifest와 전수 작업 계획. B/C/D는 모든 대상을 유지하면서 source별 승인 범위를 사용한다. 인증 부재는 외부 의존성으로 보고하고 독립적인 unit/schema 작업은 계속한다.
