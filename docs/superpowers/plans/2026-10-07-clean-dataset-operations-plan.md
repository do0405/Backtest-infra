# Clean Dataset Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전 종목 데이터셋을 장애·정정·대규모 이력·추출 누락 상황에서도 명시적 품질 기준과 복구 절차로 운용한다.

**Architecture:** 기존 ingestion/selection/release CLI를 보강한다. PostgreSQL에서 작업 lease/rate budget/품질/공유 segment/issue ledger를 관리하며 별도 runtime이나 외부 메시지 시스템을 만들지 않는다. SEC/OpenDART의 SQL 중심 저장과 문서 임시 입력 정책을 유지한다.

**Tech Stack:** 기존 Python/PostgreSQL/SQLAlchemy/Alembic/pytest, PostgreSQL backup 도구, 배치 SQL. 새 workflow broker는 필수로 추가하지 않는다.

---

근거: [운영 점검](../../data/2026-10-07-clean-dataset-operations-review.md), [설계](../specs/2026-10-07-clean-dataset-design.md). E는 기존 A~D를 대체하지 않고 같은 구현을 보강한다. task file 책임이 겹치면 아래 변경을 원래 해당 task에 포함한다.

의존 단계: A3 뒤 E1 계약 함수와 E2 lease/rate budget -> A4 probe/A5 전체 계획 -> B/C 뒤 E2 historical scan 및 E4 -> D2/D3와 E1 query 통합/E3/E5 -> E6/E7 -> D4 production 활성화. E1/E2의 통합 테스트는 해당 domain/query가 구현된 단계에서 실행한다. 없는 releases.query/domain adapter를 공통 기반에서 import하지 않는다.

## Task E1: 품질 계약·freshness·query profile

**Create:** src/backtest_data/quality/models.py, contracts.py, freshness.py, query_gate.py; config/data_quality_contracts.yaml; tests/unit/test_quality_contracts.py; tests/integration/test_query_quality_gate.py. **Modify:** catalog/sources.py, releases/coverage.py, releases/query.py, cli.py; 새 Alembic revision.

- [ ] row-valid지만 stale/source drift인 데이터, partial release에서 exact_vwap 요청, annual-only issuer의 unsupported metric, US/KR 미래 session 혼합, unknown=0 문제의 failing tests를 만든다.
- [ ] source qualification, row validation, semantic mapping, feature capability, freshness를 분리하고 quality_contract_version/profile/필수 metric/허용 evidence grade/시간 목표를 고정한다. 전수 target denominator를 유지한다.
- [ ] global_as_of와 각 시장 eligible completed session을 반환한다. price latest-required-session, 공시 discovery last-success, pending amendments, shares/FX snapshot age를 따로 검사한다. close+60분/discovery 60분은 초기 내부 목표로 두고 A4 측정·quota로 확정한다.
- [ ] history freshness는 요청 cutoff의 expected session/입력 snapshot 나이와 고정 evidence로 판정하고 오늘의 collector lag 때문에 과거 정상 조회를 거부하지 않는다. 현재 운영 health는 별도 필드다. 당시 시스템 health를 재현하려면 실제 status 이력이 있어야 한다.
- [ ] D3 QueryContext의 required_profile/quality_contract_version에 gate를 연결한다. 고정 data payload와 live operational envelope를 분리하고 오늘 health를 payload 재현성 hash에 넣지 않는다. query cache key에 profile/version/research option을 포함한다.
- [ ] strict query는 구조/PIT/필수 profile 실패를 오류로 반환한다. explicit research override는 coverage/freshness의 partial/stale 반환만 허용하고 flags/missing/age를 포함한다. 구조/PIT/미지원 rule version 오류는 우회할 수 없다. accepted를 무오류 보증으로 표시하지 않는다.
- [ ] python -m pytest tests/unit/test_quality_contracts.py tests/integration/test_query_quality_gate.py -q --require-postgres 후 git commit -m 'feat: enforce versioned data quality profiles'.

## Task E2: lease·rate budget·정정 재검증

**Create:** src/backtest_data/ingestion/leases.py, rate_limits.py, revision_scan.py; src/backtest_data/ops/schedule_policy.py; tests/integration/test_worker_lease.py, test_source_rate_budget.py, test_revision_discovery.py. **Modify:** ingestion/jobs.py, supervisor.py, catalog/qualification.py.

- [ ] worker 중단/lease 만료 후 stale commit, 두 market worker의 합산 limit, provider 429/020, auth 만료, 오래된 가격 correction/PAC 삭제를 검사한다.
- [ ] PostgreSQL atomic lease/heartbeat/fencing token으로 작업을 소유한다. TTL/heartbeat/HTTP timeout은 설정으로 분리하고 오래된 worker는 checkpoint/activation을 갱신할 수 없다. 동일 재확인은 ingestion attempt만 추가하고 기존 first_seen/captured 값을 덮어쓰지 않는다.
- [ ] source/account 공통 quota와 endpoint budget, Retry-After, exponential backoff+jitter, circuit 상태를 구현한다. SEC 공식 최대 10회/초보다 낮은 초기 budget을 사용하고 DART/KIS/가격 source는 실제 계약·응답으로 제한한다. quota로 종목을 전수 계획에서 삭제하지 않는다.
- [ ] 승인한 공통 quota 안에서 live discovery/최신 증분에 예약 budget과 우선순위를 두고 backfill/repair를 공정하게 처리한다. 전수 백필이 공시 discovery를 굶기는 fixture와 장기 저우선 작업의 starvation 회귀를 검사한다. work 집합을 삭제해 backlog를 줄이지 않는다.
- [ ] 일일 recent overlap, 회전 historical verification, event-triggered repair를 계획한다. 검증 cycle 초기 목표 90일과 segment별 last_verified를 기록하며 실제 용량으로 조정한다. 발견/검증을 하지 않은 과거 기간을 verified로 표시하지 않는다. 분할/기준/스키마 변경은 관련 전체 history 검사를 유발하고 source PAC/취소도 발견한다.
- [ ] 같은 accession/receipt/URL의 다른 hash와 withdrawal을 새 content revision/event로 저장한다. 공식 정정 공개 시각이 없으면 detected/captured 시각 기반 보수적 available_at 및 correction_time_unknown을 사용하고 원공시일을 자동 재사용하지 않는다. 삭제는 과거 값을 제거하지 않으며 새 release의 event/selection/coverage에 반영한다. original=100@3월, replacement=110을 5월 발견/정정 시각 미확보 fixture에서 reconstructed_public 4월 cutoff에 110이 들어가지 않도록 검사한다.
- [ ] python -m pytest tests/integration/test_worker_lease.py tests/integration/test_source_rate_budget.py tests/integration/test_revision_discovery.py -q --require-postgres 후 git commit -m 'feat: recover and reconcile bounded collection workers'.

## Task E3: 공유 segment와 영향 범위 재계산

**Create:** src/backtest_data/releases/segments.py; src/backtest_data/fundamentals/dependency_graph.py; tests/integration/test_shared_release_segments.py, test_incremental_derivation.py. **Modify:** releases/models.py, build.py, query.py, fundamentals/derive.py; 새 Alembic revision.

- [ ] 같은 역사 구간을 100개 release에서 복제, 미매핑 source 계정 누락, correction chunk 변경, 가짜 max-ID membership, 현재 rule로 과거 재계산하는 회귀를 만든다.
- [ ] data_segment/segment_member/release_segment로 domain/market/partition의 명시적 record/decision ID 집합과 content hash를 공유한다. release_member 의미 계약은 유지한다. max(id)나 mutable latest view로 정확 membership을 대체하지 않는다. 원본 SQL fact는 삭제/압축하지 않는다.
- [ ] derived identity=(metric definition hash, input IDs/hash, period, history_mode/disclosure_profile/basis/scope와 기타 계산 context hash)로 동일 의미 계산을 재사용한다. 요청 as_of/release는 query binding으로 분리해 공시 사이의 같은 비율을 매일 복제하지 않는다. 가격/FX/분할·ADR 기준/window/anchor가 cutoff에 따라 달라지면 signature도 달라진다. first materialized_at을 보존하고 query cache는 release/cutoff/profile/version을 포함한다. 재사용 결과의 실제 입력 가시성/당시 materialization도 cutoff로 검증한다.
- [ ] dependency graph로 바뀐 input의 가시 구간과 해당 metric/window/anchor만 재계산한다. source correction/definition change는 새 record와 새 chunk를 만들며 옛 release는 그대로다. 판별할 수 없는 영향은 명시적인 확대 repair로 처리한다.
- [ ] python -m pytest tests/integration/test_shared_release_segments.py tests/integration/test_incremental_derivation.py -q --require-postgres 후 git commit -m 'feat: share immutable history and incremental derivations'. 기대: 무변경 history segment/derived row를 재사용하고 바뀐 구간만 추가됨.

## Task E4: SQL 추출 commit gate와 실적 공시 범위

**Create:** src/backtest_data/fundamentals/extraction_inventory.py, disclosure_profiles.py, adapters/earnings_disclosures.py; tests/unit/test_extraction_inventory.py; tests/integration/test_disclosure_profiles.py, test_annual_eps_metrics.py. **Modify:** fundamentals/adapters/sec.py, opendart.py, xbrl.py, metric_policy.py, availability.py, selection.py, query.py, releases/query.py, ingestion checkpoint/CLI; config/fundamental_inventory.yaml 및 새 Alembic revision.

- [ ] 숫자/단위/context 누락, nil과 0 혼동, 중복 같은 계정/different dimensions, parser bug, 정기 report 이전 실적 발표, non-GAAP EPS 혼합, annual-only issuer를 검사한다.
- [ ] 임시 문서에서 기대 numeric/context inventory를 생성하고 SQL rows/count/hash/단위/기간/예외와 대조한다. 구조화 값·원표기·precision·차원·추출 버전을 모두 commit한 후만 문서를 정리한다. 불완전 inventory는 pending/quarantined이며 extraction_complete=false다.
- [ ] 같은 파서의 자체 count 일치만으로 추출 완전성을 인증하지 않는다. 승인한 포맷별 독립 numeric node/계정 inventory fixture와 재무표/주석 검산을 사용하고 미지원 비구조화 숫자·누락 포맷은 extraction coverage에 남긴다. useful inventory 버전/범위를 고정한다.
- [ ] 재파싱은 upstream 재수집+content hash 비교로 수행한다. 저장된 source facts로 재매핑 가능한 경우 문서를 요구하지 않는다. 새로 추출한 값의 실제 extracted_at/first_seen을 기록해 과거 observed_system 입력으로 소급하지 않는다.
- [ ] 재수집 hash가 다르면 E2의 content revision/availability 계약을 적용한다. 같은 filing identity의 원공시일을 새 값의 공개 시각으로 추정하지 않으며 PAC/receipt replacement와 정상 amendment를 구분한다.
- [ ] periodic_report와 earliest_ingested_disclosure를 명시적 query profile로 분리한다. 승인 source 안의 SEC 실적 공시/외국 issuer 공시/KR 잠정 실적을 숫자·기간·회계 기준 검증 후 SQL화한다. accounting_basis와 assurance를 보존하고 전 세계 최초 공개 시각을 확보했다고 주장하지 않는다.
- [ ] D3 QueryContext의 disclosure_profile/disclosure_policy_version을 source selection과 재무 query/cache/derived signature에 전달한다. 최초 실적 공시 이후 정기 보고 전 cutoff에서 두 profile의 서로 다른 결과를 검사하고 global policy fallback을 거부한다.
- [ ] 기존 strict 4분기 EPS_TTM과 growth를 유지하고 직접 연간 EPS의 per_diluted_fy, growth_eps_basic_fy_yoy, growth_eps_diluted_fy_yoy, growth_revenue_fy_yoy, growth_operating_income_fy_yoy를 별도 financial_metrics_annual_v1로 등록한다. missing quarterly를 연간 차감으로 만들지 않으며 TTM 요청을 annual 결과로 조용히 채우지 않는다.
- [ ] python -m pytest tests/unit/test_extraction_inventory.py tests/integration/test_disclosure_profiles.py tests/integration/test_annual_eps_metrics.py -q --require-postgres 후 git commit -m 'feat: validate structured financial extraction and disclosure scope'.

## Task E5: 이슈·매핑 변경·수동 결정의 복구 흐름

**Create:** src/backtest_data/quality/remediation.py, overrides.py; tests/integration/test_remediation_lineage.py. **Modify:** selection_decision models/CLI, source validation, release build.

- [ ] 매핑 실패를 provider 장애로 재시도, 잘못된 계정 덮어쓰기, override를 과거 decision으로 backdate, 기존 sealed release 변경을 차단한다.
- [ ] issue마다 source/record IDs, rule, severity, affected feature/기간, first/last seen, owner/상태, evidence, 원인 classification을 저장한다. transient/API/schema/identity/semantic/coverage 문제를 구분한다.
- [ ] 매핑 수정은 새 mapping version과 canonical/derived revision이다. 수동 선택은 actor/reason/recorded_at/유효 범위/evidence를 저장한다. 경제적 적용 기간과 실제 결정 시각을 구분한다. SQL 원값을 UPDATE하지 않는다.
- [ ] 재수집 -> 추출/매핑 확인 -> 영향 graph -> validation -> 새 release -> 활성화/이전 pointer 복귀의 runbook을 만든다. 구조 오류는 기존 좋은 release 유지+stale 상태를 노출하며 새 날짜 성공으로 가장하지 않는다.
- [ ] python -m pytest tests/integration/test_remediation_lineage.py -q --require-postgres 후 git commit -m 'feat: trace dataset issue remediation'.

## Task E6: 일관 백업·독립 사본·복원 목표

**Create:** src/backtest_data/releases/backup_snapshot.py; tests/integration/test_consistent_backup.py, test_recovery_drill.py; config/recovery_policy.yaml; docs/data/recovery-runbook.md. **Modify:** releases/backup.py, cli.py, deployment config.

- [ ] 수집 중 DB/file 시점 불일치, primary 장치 손실, 누락 archive 파일, 문서 없는 재무, mid-publish crash를 복원 fixture로 검사한다.
- [ ] pg_dump의 동일 exported snapshot과 그 snapshot의 immutable file references를 backup manifest에 결합한다. SQL financial source rows가 유일한 재무 복구 입력이며 SEC/DART 문서 files는 포함하지 않는다. roles/global config의 복구도 계획한다.
- [ ] 다른 failure domain의 사본을 설정한다. 동일 디스크의 다른 폴더는 장치 재해 복구 사본으로 세지 않는다. 초기 목표 RPO 24시간/RTO 8시간을 기준으로 전수 크기 drill에서 실측하며 미충족은 공개한다.
- [ ] 목표가 더 짧아지거나 dump/restore 시간이 초과하면 PostgreSQL base backup+연속 WAL/PITR를 선택한다. PG crash recovery를 backup과 혼동하지 않는다. retained release references와 필요한 base/WAL chain을 retention 정책에서 보호한다. 운영 DB는 fsync/durability를 약화해 bulk load를 빠르게 하지 않는다.
- [ ] WAL 이후 SQL이 새 가격 파일을 참조할 수 있으므로 raw object를 독립 사본에 복제한 시각/검증 hash와 SQL 복구 시점을 대조한다. 가능한 공통 recovery horizon까지 복원하거나 이후 파일 미확보 release를 차단한다. SQL만의 RPO를 전체 dataset RPO로 주장하지 않는다. 파일 복제 지연/손실 fixture에서 실제 공통 horizon과 manifest/PIT 조회 무결성을 검증한다.
- [ ] python -m pytest tests/integration/test_consistent_backup.py tests/integration/test_recovery_drill.py -q --require-postgres 후 git commit -m 'feat: verify consistent recoverable dataset backups'.

## Task E7: 전수 상태·용량·소스 변경 운영 보고

**Create:** src/backtest_data/ops/metrics.py, capacity.py, source_health.py; config/operating_budgets.yaml; tests/integration/test_operating_status.py; scripts/benchmark_dataset_queries.py; docs/data/operations-status.md. **Modify:** cli.py, query/ingestion roles/로그 설정.

- [ ] 일부 실패를 total complete로 표시, 새 source schema/unknown unit 급증, shares/FX age 누락, disk/WAL/backup 경고 소실, 읽기 role의 immutable row 변경을 검사한다.
- [ ] 전수 target denominator와 market/security_type/domain/feature별 completeness, ingestion lag, pending age, schema drift, new unmapped account, 429/020/auth 오류, verifier age, SQL query p95, disk/WAL/backup age를 SQL/로컬 JSON status로 출력한다. required profile 위반/새로운 actionable 변화만 로컬 issue/alert를 생성한다.
- [ ] source 계약의 개인 보관/변환/내보내기 조건과 entitlement/expiry/기준 변경을 확인한다. 재자격화가 필요한 변경은 해당 source의 자동 승격을 중지한다. credentials는 환경/secret 경로로 읽고 로그/SQL provenance/백업 문서에 넣지 않는다. read/ingest/publish/migration 권한을 분리한다.
- [ ] 승인된 source 전체 목록/기간을 기준으로 요청 수·재시도·SQL rows·index/segment/WAL/backup bytes·runtime/RSS를 예측하고 실측한다. batch COPY/streaming read/index/partition 계획을 검사한다. query plan과 실제 성능으로 budgets를 고정하고 범위를 줄여 수치를 맞추지 않는다.
- [ ] python -m pytest tests/integration/test_operating_status.py -q --require-postgres 및 python -m backtest_data status --market both, python scripts/benchmark_dataset_queries.py --market both 실행 후 git commit -m 'feat: report full-universe dataset operations'. 실제 운영 시작/외부 알림/구독 구매는 이 계획 작성에서 수행하지 않는다.

## 구현 연결과 production 승인

- A3 뒤 A4/A5: E1/E2의 계약 함수·lease·quota·work 상태를 먼저 연결한다. E2 historical scan은 B/C 이후다.
- C2/C3/C6: E4 inventory/disclosure policy와 E5 remediation을 적용한다.
- D2/D3: E3 segments/derived reuse와 E1 strict profiles를 적용한다.
- D4: E6 복원과 E7 상태/용량 검증 후 production 전수 supervisor를 활성화한다.

PIT/immutable membership/lease 위반은 publish 차단이다. SQL 추출 gate 실패 공시는 채택하지 않고 quarantine/coverage gap으로 남긴다. quality profile의 필수 coverage/freshness 실패는 정상 complete activation을 막으며 명시적 partial/stale release만 허용한다. strict query는 필수 profile을 요구하고 연구 옵션도 무결성/PIT/미지원 rule 오류를 우회하지 않는다. 전체 source proof가 없으면 전 종목·전 기간 완전성을 선언하지 않는다.
