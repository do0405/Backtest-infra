# 로컬 연구 플랫폼 진행 기록

2026-10-10에 같은 프로젝트의 사이드 채팅 「백테스트 플랫폼 통합 기획서 작성」과 `docs/planning/2026-10-09-local-research-platform/` v0.4를 읽고 공급자 연결부터 진행했다. 전체 20개 단계 완료 기록이 아니다.

| 작업 | 구현 | 실제 데이터 준비 | 운영 결과 |
|---|---|---|---|
| Task 02 공급자 연결의 일부 | KRX/Alpaca private YAML 로더, 공급자별 설정 격리, `collect --source`에 따른 claim/repair/consolidation 분리 | Alpaca 무료 과거 SIP 접근 및 실제 100행 SQL commit 성공, KRX production 10개 서비스 모두 HTTP 200 | Alpaca 미국 13,251종목의 기존 전체 기간 가격 큐 실행. 기존 KIS·재무·PIT·coverage 유지 |
| KRX 숫자 SQL 및 전체 기간 계획 | 독립 `krx_quote_observation`, additive migration c71302d6b905, 정확 Decimal/빈값/이슈 보존, service 범위의 shared lease, `krx plan/collect` | 2026-10-07 시세 4,411 + 기본정보 2,765 + 지수 시세 134 = 7,310행 commit. ETF NAV·ETN IV도 숫자 저장 | 2016-01-01~2026-10-07의 39,330개 날짜·서비스 요청. 원천 저장 적격성과 clean coverage 구분 |
| Alpaca 전수 적재 효율 | 신규 traversal 100행 페이지, 가격/선택결정 100행 이하 SQL batch insert. continuation의 기존 page_size/anchor 보존, owner/fencing/5분 TTL 유지 | 1,251개 distinct bar fixture의 가격 INSERT 1,253회 → 13회, 중복·정정·capture·rollback 검증 | 이전 큰 페이지 lease_lost 이력은 보존. 100행 페이지로 재개한 실행에서 SQL 저장과 다음 페이지 재개 확인 |
| 나머지 플랫폼 단계 | 기존 clean 기반 및 계획 보존 | 역사 universe·기업행동·정확 VWAP·원본 공개 PIT·실제 적격 지표 등에 공백 유지 | 자동 날짜 증분·원격 동기화·분석 UI 전체 구현은 아직 미완료 |

새 키는 공급자별 사용자 로컬 config에만 저장했다. [연결 실증 및 한계](platform-source-qualification.md)를 따른다.

검증 명령은 `.venv\Scripts\python.exe -m pytest ... --require-postgres --basetemp artifacts\tests\...`이며 반드시 별도 `_test` PostgreSQL을 사용했다.

- 설정·명시 공급자·KIS/DART/Alpaca 재개 및 공정성: **73 passed**.
- 최신 Alpaca 페이지·배치 적재·lease rollback·supervisor 및 KRX provider: **57 passed**.
- KRX staging·collection·migration PostgreSQL 묶음: **50 passed**. 추가 Decimal envelope 회귀를 포함한 unit/migration 재검증: **20 passed**.
- 부모의 최종 KRX staging/migration/provider·명시 source·credential 단위 검증: **73 passed**. 기존 테스트 묶음과 겹치므로 합산하지 않는다.
- 두 검증 묶음은 일부 겹치므로 합산하지 않는다. fixture 검증은 전수 coverage 완료의 근거가 아니다.

실제 access 결과는 SQL `source_qualification`, 최신 KRX 조회는 `artifacts/provider-activation/krx-services.json`, 첫 날짜 SQL 결과는 `krx-staged-day.json`에 있다. 이전 `qualification.json`의 KRX 401은 이력이다. 실행은 `artifacts/kr-full-workers.json`의 `auxiliary.alpaca_prices`와 `auxiliary.krx_quotes` 및 해당 runtime/identity/results를 따른다. 특정 PID를 영구 고정하지 않는다.

2시간 감시에 별도 Alpaca 실행을 추가했고 정상 진행/동일 차단은 알리지 않는다. 계획 종료와 초기 연결 오류는 새 장애로 반복 보고하지 않으며 감시에서 재시작·재큐·코드 작업을 하지 않는다. 기존 Docker/PostgreSQL 및 E 저장 상태 감시도 유지한다.

2026-10-10 12:35 KST에는 KRX 전체 기간 계획 SQL이 기존 IO 작업과 함께 실행 중이다. 독립 WMI wrapper는 원래 native planner의 PID·UTC 생성시각·경로와 process handle을 확인해 기다린 뒤 전체 큐를 idempotent 계획/재사용하고 수집하도록 시작했다. 현재 phase는 `waiting_for_existing_planner`이며 과거 기간 수집이 이미 끝났거나 39,330개 작업이 모두 commit됐다는 뜻이 아니다. 원래 계획이 중단돼도 후속 실행은 범위 전체를 다시 확인하며 이미 저장된 작업을 재큐하거나 지우지 않는다. 7일 실행 예산은 대기·계획·수집을 합산하고 데이터 범위를 줄이지 않는다. `artifacts/provider-activation/krx-backfill-progress.json`과 `auxiliary.krx_quotes`를 따른다.

Alpaca 12:31 KST 실제 SQL 확인에서는 신규 한 종목의 100행 commit을 확인했으며 다음 페이지가 실행 중이었다. 이 확인 수치를 미국 전수 또는 전체 기간 완료로 해석하지 않는다. 최초 큰 페이지의 lease_lost 이력은 보존하고 자동감시에는 최신 100행 페이지 정책과 KRX production 승인·실제 SQL 적재를 반영했다.
