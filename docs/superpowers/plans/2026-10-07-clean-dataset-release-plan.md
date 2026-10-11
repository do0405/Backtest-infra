# Historical Universe Dataset Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 과거 모집단과 세 도메인을 연결하고 재현 가능한 릴리스·SQL 조회·백필/증분 운영을 제공한다.

**Architecture:** 전체 대상 manifest와 record/decision/provenance dependencies를 고정한 immutable release를 발행한다. 재무 provenance는 SQL source fact이며 가격 archive만 파일을 참조한다. coverage는 전체 대상 기준이며 current pointer는 백테스트 authority가 아니다.

**Tech Stack:** PostgreSQL, SQLAlchemy, Alembic, Python CLI, SQL/Python query facade, pytest.

---

설계: [Clean Dataset 설계](../specs/2026-10-07-clean-dataset-design.md). 선행: A. price/fundamental 통합 발행은 B/C 의존. 지수는 확보된 source 범위만 지원한다.

## Task D1: 과거 종목 lifecycle과 지수 이력

**Create:** src/backtest_data/indices/models.py, adapters/official_files.py, membership.py, validation.py; config/index_sources.yaml; tests/unit/test_index_membership.py; tests/integration/test_historical_universe.py; alembic/versions/0004_indices_releases.py. **Reuse/Modify:** A5 소유 src/backtest_data/catalog/historical_universe.py.

- [ ] current-only 구성, baseline 누락, 이벤트 gap, 발표/효력 차이, ETF proxy, 비중 적용일 혼동, 상폐 종목 누락을 검사한다.
- [ ] 공식 machine-readable baseline/export와 변경 파일을 source-specific schema version으로 수집한다. 지원되지 않는 문서 포맷은 원문을 보존하되 membership completeness를 인증하지 않는다. 첫 지원 source는 A4 조사 결과에서 기계적으로 읽을 수 있는 자료로 한정한다.
- [ ] membership query는 cutoff 이전에 알려진 이벤트로 유효 기간을 구성한다. announcement-only를 effective membership에 넣지 않는다. baseline+continuity가 없는 시기는 unavailable이다.
- [ ] index_definition/levels에 PR/GTR/NTR, currency, methodology version, live/backtested 구분을 둔다. 비중은 official/reconstructed/proxy를 구분한다. A5에서 이미 실행한 historical_universe discovery를 새 지수/lifecycle 근거로 보완해 새 manifest/증분 작업을 만든다. D1이 최초 과거 종목 discovery를 소유하지 않는다. 지수 구성으로 전수 대상을 제한하지 않는다.
- [ ] python -m pytest tests/unit/test_index_membership.py tests/integration/test_historical_universe.py -q --require-postgres 후 git commit -m 'feat: retain historical universe evidence'.

## Task D2: 선택 결정과 불변 릴리스

**Create:** src/backtest_data/releases/models.py, decisions.py, build.py, coverage.py; tests/integration/test_release_atomicity.py, test_release_reproducibility.py.

- [ ] 미채택 record 포함, 누락 domain provenance, 미래 입력, 발행 중 중단, concurrent activation, 정정 후 기존 release 변경 tests를 작성한다. 재무 SQL source fact만 있고 문서 파일은 없는 정상 발행도 검사한다.
- [ ] selection_decision을 공통 테이블로 저장하되 도메인별 규칙은 각 domain에 둔다. release_member는 최초/정정/이전 채택 revision 전체와 decision 이력, derived IDs 및 dependency closure를 고정한다. source_financial_fact/fund_product_fact도 canonical 연결과 무관한 독립 member다. custom/unmapped source 값과 context/revision/visibility를 고정하고 source extraction validation을 canonical 매핑과 구분한다. 최신 record만 넣지 않는다.
- [ ] E3의 불변 data_segment/segment_member/release_segment로 동일 record/decision membership을 물리적으로 공유한다. 논리 membership과 모든 revision은 유지하며 max-ID/current view로 대체하지 않는다. metric/input hash가 같은 계산을 재사용하고 영향 구간만 재계산한다.
- [ ] C6의 versioned ratios/valuation 함수를 pre-seal candidate input set에 연결한다. 역사 as_of별 가시적인 가격·재무·주식 수·기업행동/FX와 definition bundle로 계산한 SQL derived records를 parent closure와 함께 고정한다. 이미 sealed된 release에 새 파생을 삽입하지 않는다.
- [ ] manifest에 financial_metric_definition_version과 해당 YAML content hash를 고정한다. 새 정의는 새 release/derived revision을 만들고 옛 release의 financial_metrics_v1을 오늘의 식으로 재해석하지 않는다.
- [ ] metric family별 definition version/hash bundle을 고정한다. financial_metrics_v1과 별도 financial_metrics_annual_v1을 구분하고 query 응답에 annual/TTM basis를 명시한다.
- [ ] requested output 범위 이전의 warm-up SQL fact/잔액/주식 수 및 가격/VWAP 입력도 release member/dependency closure에 포함한다. manifest에 output/input 범위를 따로 표시하고 opening-date coverage와 누락 이유를 테스트한다.
- [ ] manifest에 전체 target universe ID/hash, code revision/content hash, schema/normalizer/source/adjustment/calendar/query versions, 요청/실제 기간, 증거 등급, missing/eligible masks를 저장한다. 가격 archive 파일 및 재무 SQL source fact/공시 metadata/parent/decision 무결성을 domain별로 검사한다. SEC/OpenDART 문서 파일을 요구하지 않는다.
- [ ] 릴리스 sealing을 transaction으로 처리한다. activation은 compare-and-set으로 갱신하고 실패면 기존 current pointer를 유지한다. 불완전 coverage는 partial release로 공개할 수 있지만 오류를 PASS로 바꾸지 않는다.
- [ ] E1의 고정 품질 profile과 E5 이슈/승격 정책을 적용한다. 무결성/PIT 위반은 항상 차단하며 coverage/freshness 공백은 partial/stale 상태로 노출한다. strict query는 요청한 필수 profile을 만족하지 않으면 거부한다.
- [ ] python -m pytest tests/integration/test_release_atomicity.py tests/integration/test_release_reproducibility.py -q --require-postgres 후 git commit -m 'feat: seal auditable dataset releases'.

## Task D3: SQL 조회와 end-to-end 검증

**Create:** src/backtest_data/releases/query.py, query_context.py, sql_views.py, query_versions.py; tests/integration/test_dataset_query.py, test_temporal_leakage.py, test_cli_workflow.py; docs/data/query-contract.md. **Modify:** src/backtest_data/cli.py.

- [ ] release/as_of 미지정, future/capture-only 입력, 다른 source의 volume와 VWAP 혼합, universe 결측 숨김, corrupt lineage를 검사한다. R2의 최초/정정 값을 앞/뒤 cutoff로 조회하고 현재 rule bundle 변경 후 R1 동일성/명시적 미지원 오류를 검사한다. canonical과 연결되지 않은 custom account의 R1/R2 정정과 context도 동일한 cutoff/membership 회귀로 검사한다.
- [ ] QueryContext는 release_id/as_of/history_mode/required_profile/research_option을 명시하고 release에 고정된 quality_contract_version/query_version을 bind한다. 재무 조회는 disclosure_profile 및 고정 disclosure_policy_version도 필수다. 다른 도메인은 disclosure_profile=not_applicable이다. 전역 설정이나 오늘의 default로 profile을 선택하지 않으며 release와 version 불일치는 거부한다.
- [ ] 공개 함수는 get_prices(security_ids,start,end,context,basis), get_fundamentals(issuer_ids,context), get_product_fundamentals(security_ids,context), get_source_accounts(issuer_ids,context), get_vwap(security_ids,context,window_or_anchor), get_universe(index_id,session,context)로 고정한다. 모든 context는 위 QueryContext다. 미매핑 source 계정도 SQL로 조회할 수 있다.
- [ ] get_source_accounts는 release에 고정된 SQL source rows만 사용하며 available/captured/source SQL first_seen/extracted/decision 시점을 history_mode에 맞게 제한한다. 기본 출력은 all_visible revisions와 mapping 상태이며 latest_visible 선택은 explicit 옵션과 frozen query rule로 제공한다. 현재 전역 source table의 새 정정값을 옛 release에 끼워 넣지 않는다. 이전 문서의 재파싱에서 새로 발견한 fact를 당시 observed_system에 소급하는 회귀를 차단한다.
- [ ] get_financial_metrics(entity_ids,entity_type,context,metric_ids)를 제공한다. financial_metrics_v1/financial_metrics_annual_v1 및 source/price/FX input IDs를 반환하며 null/reason과 not_applicable을 유지한다. 테스트에서 정정 전후 ROE/PER/EV, 분할/음수/누락 class와 옛 release 정의의 동일성을 확인한다. 동일 cutoff에서 periodic_report/earliest_ingested_disclosure의 다른 승인 입력과 회계 기준/assurance를 검사한다.
- [ ] facade와 parameterized SQL을 query_versions.py의 versioned dispatcher로 사용한다. release의 query/adjustment/calendar rule bundle을 선택하고 해당 버전의 frozen calendar/actions/materialization을 사용한다. 미지원 rule bundle이면 명시적 unsupported_version 오류를 반환하며 현재 규칙으로 fallback하지 않는다. observed_system은 입력 captured, decision.recorded_at, derived.materialized_at을 cutoff로 제한한다. materialized/cache 결과는 release+cutoff+history_mode+basis+trade_scope+anchor+query_version에 연결한다. convenience latest를 query default로 사용하지 않는다.
- [ ] quality/disclosure profile와 각 policy version/research option을 query cache key에 포함한다. 재현 가능한 data payload에는 고정 coverage/입력 시점/definition/lineage를 담고 current collector health/latency/요청 시간은 별도 live operational envelope로 반환한다. 동일 context의 data payload bytes/hash만 재현성 대상으로 삼고 live envelope는 제외한다. 오늘 health 변화가 과거 data payload/eligible mask를 바꾸는 회귀를 차단한다.
- [ ] rolling 계산은 market exchange_session grid의 N세션을 요구한다. missing 여부를 출력하고 feature-required coverage를 강제한다. source quality grade와 reconstructed_public/observed_system을 응답에 표시한다. cli.py에 validate --scope all [--release-id], release build --market, query smoke --release-id --market handler를 등록하고 fixture DB에서 subprocess exit/output 검사를 한다.
- [ ] python -m pytest tests/integration/test_dataset_query.py tests/integration/test_temporal_leakage.py -q --require-postgres 후 git commit -m 'feat: expose release-bound SQL data queries'.

**통합 회귀:** release R1에 원공시와 원가격을 고정한다. 새 공시/가격 correction을 넣고 최초/정정 이력을 모두 가진 R2를 발행한다. R1의 동일 QueryContext data payload bytes/hash가 동일해야 한다. live operational envelope는 해시에서 제외한다. R2의 정정 전 cutoff는 최초 fact, 정정 후 cutoff는 정정 fact를 반환해야 한다. cutoff를 앞당기면 이후 공시·기업행동·anchor 정보가 제외돼야 한다.

## Task D4: 전체 백필·증분·정정·복원

**Create:** src/backtest_data/ingestion/supervisor.py; releases/backup.py; tests/integration/test_collection_recovery.py, test_backup_restore.py, test_financial_restore_without_documents.py; docs/data/operations.md; tests/integration/test_full_universe_e2e.py. **Modify:** src/backtest_data/cli.py, tests/integration/test_cli_workflow.py.

- [ ] provider 제한, 종목 실패, 장기 중단 후 gap catchup, 재확인/no-change, 백업에 raw 파일 누락, 동일 run 재시작 tests를 만든다.
- [ ] 하나의 collect CLI에서 market us/kr/both, domain prices/fundamentals/indices, mode bootstrap/incremental/repair를 지원한다. 실패 단위는 가능한 한 종목/공시이며 structural contract 실패는 해당 source 승인을 중지한다. 이중 runtime/다른 repo bridge를 추가하지 않는다.
- [ ] 최초 수집부터 A5 전체 manifest의 모든 종목/ETF/ETN/issuer 작업을 백필한다. 내부 배치/fixture는 전체 대상 축소가 아니다. 실패 종목은 pending/repair에 남기고 source 미지원은 대안/미완료로 보고한다. 역사 마스터가 불완전하면 historical_universe_complete=false다. discovery와 정정을 source 계약에 따라 반복한다.
- [ ] 전체 SQL source/canonical/derived/상품 fact와 이력을 DB snapshot으로 백업하고 실제 archive 대상 파일만 raw manifest로 묶는다. 새 DB/다른 raw root에 복원해 PIT/VWAP hash를 비교하고 SEC/OpenDART 문서가 전혀 없어도 재무 조회가 성공하는지 검사한다. query rule 미지원 거부, maintenance verify-backup --backup-manifest 등록, CLI subprocess도 검사한다. SQL 이력 삭제는 만들지 않는다.
- [ ] E6의 일관 snapshot·다른 failure domain·SQL/WAL와 raw 파일의 공통 복구 horizon, E7 전수 상태/용량 검증을 production 활성화 전에 완료한다. 실제 운영 목표 충족 여부를 측정하고 파일이 없는 복구 시점을 정상으로 인증하지 않는다.
- [ ] python -m pytest tests/integration/test_collection_recovery.py tests/integration/test_backup_restore.py tests/integration/test_financial_restore_without_documents.py tests/integration/test_full_universe_e2e.py -q --require-postgres 후 git commit -m 'feat: operate recoverable full-universe collections'.

**운영 명령:**
~~~powershell
python -m backtest_data collect --market both --domain prices --mode bootstrap --start 2016-01-01
python -m backtest_data collect --market both --domain fundamentals --mode incremental
python -m backtest_data collect --market both --domain indices --mode incremental
python -m backtest_data validate --scope all
python -m backtest_data release build --market both
~~~

bootstrap/incremental 종료 상태와 quality/coverage 완료는 별도 집계한다. source가 기간별 요청을 지원하지 않으면 full response 전송과 저장/정규화 범위를 구분한다. SQL COPY/batch 크기·partitioning은 표본 측정으로 정하며 row별 ORM save를 대량 경로에 사용하지 않는다.

## D 완료 기준

전체 target manifest를 분모로 가격·volume·기업/상품 펀더멘탈·정확 VWAP·과거 universe 확보 상태를 보고한다. 고정 release의 정정 전후 재현성과 SQL 재무 복원을 증명한다. operational_complete와 각 domain_coverage_complete를 구분하고 applicable 값의 누락/실패 또는 역사 마스터 공백이 남으면 전체 완료로 선언하지 않는다. partial release는 중간 결과이며 source 권한/이력이 미확보인 값을 만들어내지 않는다.
