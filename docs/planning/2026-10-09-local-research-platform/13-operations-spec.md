# 실행 예산 무료 운영 백업 복구 명세

## 확인한 로컬 환경

2026-10-09 읽기 전용 하드웨어 조회에서 CPU는 Ryzen 7 9700X 8코어/16논리 프로세서, RAM은 31.1GiB, Windows 11 Home 10.0.26200이었다. 순간 가용 RAM은 5.3GiB였다. E는 총 1863.0GiB·여유 678.6GiB, D는 총 488.3GiB·여유 308.4GiB, C는 총 441.4GiB·여유 39.5GiB였다.

값은 당시 스냅샷이며 현재 가용량은 실행 시작 때 다시 측정한다. Docker PostgreSQL named volume과 WSL virtual disk의 실제 호스트 저장 위치는 확인 대상이다. 데이터 폴더가 E에 있다고 DB volume도 E라고 가정하지 않는다. C 여유가 작으므로 전체 백필 전에 실제 volume·로그·image·임시 파일 위치와 증가량을 확인한다. 기존 volume 자동 이동·삭제는 하지 않는다.

## 하루 5시간 이내 로컬 사용

| 항목 | 초기 기본값 | 동작 |
|---|---|---|
| 로컬 background window | 시작 후 최대 240분 | 새 heavy job 중단, 안전 checkpoint·남은 동기화·정리 |
| 일일 hard window | KST 날짜별 최대 300분 | owned workers cooperative pause, 자동 재기동 없음 |
| 무거운 분석 worker | 1개 | projection/backtest/screener 계산 직렬, API 조회 별도 |
| 계산 thread | 최대 4 후보 | BLAS/Numba cap·below-normal priority 후보, 실측 조정 |
| 메모리 상한 | min(4GiB, 시작 available RAM−4GiB) | 1GiB 이하이면 heavy job 대기, 작은 청크·spill |
| 가용 RAM 보호 | 실행 중 available<3GiB이면 pause 후보 | OS·사용 앱 여유 확보, 강제 kill 대신 경계 pause |
| 저장 경고·보류 | 실제 volume free<20% 또는 예상 작업 필요량+reserve 부족 | 신규 backfill·cache write 보류, 기존 SQL 유지 |
| task slice | 기본 5~10분 | 페이지·세션·청크 경계마다 저장·재개 |

300분은 앱이 소유한 로컬 background 실행 창의 누적 wall-clock 상한이다. 병렬 task 시간을 각각 더해 가동 시간을 과장하지 않고, 날짜를 넘는 작업은 날짜별 active interval로 분할한다. 240분은 기본 새 작업 마감 기준이며 실제 수집·분석은 더 짧게 끝날 수 있다. 모든 PC 사용 시간을 프로그램이 통제한다는 의미는 아니다.

DEC-08/14에 따라 이 운영 기본값은 이번 기획·구현·검증 작업의 진행 제한이 아니다. 이번 작업은 완료까지 진행하되 실제 PC 가용 RAM·디스크·source quota·데이터 무결성 보호를 끄지는 않는다. 장기 일상 앱은 5시간 사용 목표와 checkpoint/resume을 유지한다.

PC 자동 종료·절전 설정 변경은 초기 범위가 아니다. 사용자가 종료할 수 있도록 “일시중지·데이터 저장 완료” 상태를 표시한다. 전수 10년 backfill을 5시간 안에 완료한다는 보장은 없다. 여러 날/원격으로 나누고 전체 target·진행·부족 기간을 보존한다.

로컬 앱 launcher가 소유하지 않는 기존 WMI 수집 프로세스는 자동 종료하지 않는다. 구현 시 현재 worker identity·lock·checkpoint·SQL lease를 확인해 **검증된 인계 절차**로 새 예산 시스템에 편입한다. 인계 이전과 이후의 실행 주체를 문서화한다.

## 저장 규모 계획

현재 master 17,758개×252세션×11년은 약 4,922만 security-session 후보 cell이다. 모든 종목이 전체 기간 존재했다는 가정의 산술 상한 예시이며 역사 상폐 추가·실제 상장기간·source version/confirmation 이력으로 실제 행 수는 달라진다.

가격 bytes/row는 실제 `pg_total_relation_size`/row count에서 table+index+toast를 측정한다. projected SQL size는 유효 row 수×측정 bytes/row×version factor로 추정하고 raw·WAL·cache·snapshot backup·restore scratch를 따로 더한다. 예를 들어 500bytes/row 가정이면 가격 4,922만행만 약 24.6GB지만, 이는 측정 견적이 아니며 재무·증거·다중 버전·인덱스가 추가된다.

매 요청 전체 재무 숫자를 스캔하지 않는다. batch normalization·partitioned releases·range projection·capture ledger 압축을 단계적으로 도입한다. 기존 immutable 원관측을 정리한다는 이유로 지우거나 같은 값의 관측 시각을 합쳐 잃지 않는다. schema 변경은 새 보존 모델과 round-trip 검증 후 적용한다.

## 원격 무료 예산

공식 무료량은 [출처 문서](16-decisions-and-sources.md)를 기준으로 다시 확인한다. 계정의 다른 작업·저장량을 차감한 잔여량만 본 프로젝트에 사용할 수 있다.

| 리소스 | 내부 초기 기준 | 필수 제어 |
|---|---|---|
| R2 Standard 저장 | 6GB warning, 8GB 신규 ingest hold | checkpoint·미수신·참조 closure 우선 보호, 월 GB-month 예측 |
| R2 write/list requests | 월 계정 무료 잔여 80% warning·90% hold | batch object 수·list frequency 제한, multipart 포함 |
| R2 read requests | 같은 정책 | head polling 저빈도·index/range 효율화 |
| 비공개 Actions minutes | 월 계정 잔여 80% warning·90% hold | 설치·retry·DB setup 포함 actual job elapsed |
| Actions artifacts | 70% warning·85% hold + expiry reserve | 보관기간·PC offline 기간 비교, unacked 삭제 금지 |
| Cloud Run 대안 | CPU/RAM 잔여 80% warning·90% hold | job 전체 container resource·시작·retry·egress 견적 |

budget alerts는 강제 과금 상한을 보장하지 않는다. 비용 0을 우선할 때는 앱 사전 reserve·batch 최대량·계정 제한·spending 설정 가능 여부를 확인한다. 로그와 사용량 집계 지연을 위해 safety margin을 둔다. 앱이 quota를 정확히 읽지 못하면 마지막 확인 잔여량에서 보수적으로 reserve하거나 원격 실행을 보류한다.

확정 비용 정책은 `automatic_paid_budget_krw=0`이다. 유료 후보는 데이터+compute+저장+전송+세금·환산을 합한 월합계10,000원 이내에서 제안만 한다. 목적·대안·요금 근거·반복 비용·사용 한도와 사용자 확인을 기록한 뒤 이용 여부를 결정한다. 제안 상한은 지출 승인도 서비스의 강제 과금 상한도 아니다. PRO·GPU·paid CLI·외부 모델·유료 API key를 기본 설치/실행에 포함하지 않는다.

방법/feature manifest에는 의존성·입력·호출량·time/RSS·scope·engine capability를 기록한다. 초기 고급 수학은 interface만이고 실제 학습·모델 제공 작업을 자동 추가하지 않는다. 탐색량은 CPU/RAM·window와 사전 trial plan으로 측정·제한하고, 부족하면 pause/chunk로 처리한다. 기존 target이나 실패 trial을 삭제하지 않는다.

미수신량 가정 100MB/day, local offline 30일이면 약 3GB+checkpoint+raw dependency다. 300MB/day이면 9GB로 초기 8GB hold를 초과한다. 이 값은 가정 예시이며 실제 공시일 집중·정정·backfill을 측정한다. 용량 부족 시 대상을 삭제하지 않고 수집을 보류해 recoverable gap과 source-version loss 위험을 기록한다.

## 소유권과 수집 순서

GitHub workflow concurrency는 같은 repo run의 중복을 막고, provider-account의 R2 CAS owner/fencing은 local/remote를 포함한 소유권을 통제한다. KIS KR/US는 하나의 owner·rate policy다. local backfill과 remote live를 같은 키로 동시에 돌리지 않는다.

초기 운영은 **기존 local 신규 요청 중단→in-flight 종료/audit→checkpoint 저장·SQL lease 해제→collector pause 확인→portable state seed→단일 control CAS로 remote owner 획득→첫 committed batch→local import/validation→인계 완료 기록** 순서다. 먼저 remote를 돌린 뒤 legacy local을 멈추는 순서는 금지한다. 실패 시 remote를 drain하고 control의 epoch·checkpoint를 확인해 local owner로 인계한 뒤 안전 재개한다. 양쪽 cursor를 독립 success로 만들어 병합하지 않는다.

provider별 fresh data·정정·backfill 큐에 우선순위를 두되 미완료 backfill을 굶기지 않도록 fair rotation을 사용한다. OpenDART 점검·rate limit·auth 오류·unsupported를 구분하고 그 정책은 해당 provider stream에만 적용한다.

source 자동 전환은 금지한다. 일시 오류는 원 source retry/resume, 인증·quota 문제는 blocked/hold로 기록한다. independent source 대조 수집은 별도 stream/dataset이며 원 source 완료로 합산하지 않는다. source 채택 변경은 명시 config·새 dataset version으로만 수행한다.

## 백업과 원격 정리

기존 SQL exported snapshot+raw closure backup과 verify/restore drill을 재사용한다. 연구 result·config·model artifact·portfolio journal도 snapshot manifest에 포함한다. 운영 snapshot은 consistent SQL+필수 파일 전체 closure를 포함해야 한다.

기본 후보는 최근 3개 verified snapshot을 로컬 보관하고 독립 디스크에 주 1회 복사다. 실제 DB 규모에 따라 full/incremental/WAL/PITR 선택을 다시 결정한다. 동일 디스크 백업은 장치 고장 대비 사본이 아니다. 무료 R2 변경분 큐를 전체 DB backup 장소로 착각하지 않는다.

RPO 목표는 마지막 verified local snapshot과 미수신 remote bundle로 표현하고, RTO는 실제 복원 실측 후 정한다. 전수 규모 복원 시간을 측정하기 전 5시간 이내 복구를 보장하지 않는다. 복원은 새 임시 DB·새 raw root에서 먼저 검증하고 원래 DB를 덮어쓰지 않는다.

remote batch 삭제는 import+validate+snapshot inclusion+ack, 그리고 최신 checkpoint 및 미수신 dependency 보호 조건을 충족할 때만 한다. manifest chain metadata·최신 checkpoint·catalog dependencies는 batch payload 정리와 분리해 보관한다. batch payload가 삭제돼도 chain·local receipt로 audit할 수 있어야 한다.

## 장애별 복구

| 사건 | 처리 |
|---|---|
| runner가 payload upload 중 종료 | 마지막 committed head 복원, orphan staging은 grace 후 확인 |
| manifest 후 head commit 전 종료 | 미commit orphan, 이전 cursor에서 안전 재수집 |
| head commit 성공 후 응답 소실 | head/manifest hash로 reconcile, 무조건 재advance 금지 |
| 로컬 download 중단 | 부분 파일 hash 검증, complete 파일만 staging |
| raw 저장 뒤 SQL 실패 | cursor 미전진, 동일 batch 재import, orphan은 closure 점검 |
| SQL commit 뒤 ack 실패 | receipt 확인 후 ack 재전송, fact 중복 생성 없음 |
| schema/policy mismatch | quarantine·upgrade 필요 표시, 임의 변환 금지 |
| owner lease 소실 | 신규 호출 중단, stale epoch head commit 거부 |
| PC 30일 미접속 | retained batch import, backlog·quota 위험 표시 |
| artifact 만료 | gap 탐지·source 재수집 가능성 평가, 사라진 과거 version은 unrecoverable 표시 |
| cache 손상 | SQL 기준으로 재생성, signal 근거 없는 cached value 사용 금지 |
| worker OOM·앱 재시작 | checkpoint·lease 복구, 성공 결과로 표시하지 않음 |

## 관측과 알림

status에는 시장·domain·source·queued/retry/running·last eligible session·freshness·capture bounds·coverage gap·rate·owner·sync lag·budget·백업 시각을 표시한다. 정상 unchanged 상태를 반복 알리지 않는다. 앱 내부에는 신규 failure·stall·quota·expiry risk·사용자 조치만 올린다. 외부 채널 발송과 recurring automation 생성은 현재 문서 작성 범위가 아니다.
