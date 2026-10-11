# 무료 원격 수집과 로컬 SQL 적재 PRD

기존 [원격 설계](../../data/free-remote-update-local-sql.md)를 계승해 **공개 최소 수집 코드 + 표준 GitHub Actions + 비공개 R2 Standard + 로컬 PostgreSQL**을 우선안으로 둔다. 사용자는 비밀값을 제외한 수집 코드 공개를 허용했다. 실제 계정·대상 repo·bucket·키 설정은 배포 단계에 구성하며 현재 전체 저장소를 자동 공개하지 않는다.

## 해결할 문제

사용자는 PC를 하루 5시간 이내 사용하는 환경을 원한다. PC가 꺼져 있어도 신규 일봉·공시·정정을 확보하고, 다음 로컬 사용 시간에 변경분만 SQL에 적재해야 한다. 전체 과거 백필은 여러 날에 걸쳐 재개하고 클라우드 무료량을 실측해 분담한다.

## 요구와 완료 조건

| ID | 요구 | 완료 조건 |
|---|---|---|
| I01 | 증분 작업 생성 | 마지막 성공 날짜가 아니라 coverage gap·신규 공시·정정 대상별 작업 생성 |
| I02 | 유한 실행과 재개 | 예산 종료 전 checkpoint, 다음 실행 누락 없이 재개 |
| I03 | 영속 전달 | payload→hash 검증→commit manifest→head 순서 |
| I04 | 로컬 원자적 적재 | 파일 검증·부모 closure·SQL transaction·import receipt |
| I05 | 중복·순서 변경 허용 | 같은 batch 재처리 0개 신규 관측, 독립 stream 별 cursor |
| I06 | 시점 보존 | remote capture·local import·local compute 시각 분리 |
| I07 | 무료량 통제 | 저장·요청·실행 계정 잔여량 확인, 사전 reserve·보류 |
| I08 | 공급자 소유권 | 계정별 local/remote 동시 수집 차단, KIS KR/US budget 공유 |
| I09 | PC 장기 미접속 | 미수신분 보존, 한도 접근 시 새 수집 보류와 recoverable gap |
| I10 | 원격 의존 없는 복구 | 로컬 bundle folder transport와 공급자 재수집 가능 |
| I11 | 정정 탐지 | 최근 회전 scan·기업행동 이전 구간 재확인·전수 장기 scan 계획 |
| I12 | 공급자 장애 격리 | SEC·DART·KIS 하나의 오류가 전체 큐를 중단하지 않음 |

## 예약과 날짜 기준

KR은 정상 폐장 뒤 16:37 KST, US는 14:37 KST를 초기 polling 후보로 둔다. 기존 Alpaca 어댑터는 완전히 끝난 New York 날짜와 최근 조회 여유를 요구하므로 08:37 KST에는 전날 봉을 제외할 수 있다. 더 이른 미국 갱신은 regular-session 봉 확정과 해당 source scope를 검증한 뒤 별도 정책으로 도입한다. 거래 달력·조기종료·DST·공급자 finalization을 확인하고 미국 세션은 New York 경제 날짜로 기록한다.

가격은 신규 eligible 세션+최근 5세션 재확인, 재무는 신규 filing discovery 하루 1회, 최근 90일 공시 회전 정정 확인을 초기 작업 정책으로 둔다. 예산이 부족하면 작업을 여러 실행으로 나누고 미완료 coverage를 남긴다. 정정 범위를 이 기간으로 영구 한정하지 않고 장기 회전 scan과 source-specific revision discovery를 추가한다.

GitHub 예약은 지연·누락될 수 있으므로 예약 시각을 데이터 수신 보장 시각으로 사용하지 않는다. 실제 run·covered interval·missed interval을 저장하고 후속 실행에서 catch-up한다. [공식 예약 조건](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## 원격 작업 상태와 현재 SQL 수집기의 연결

현재 수집기는 SQL job·lease·rate budget에 의존한다. Actions에서 CLI만 실행하면 이 설계가 완성되지 않는다. 원격 실행은 작은 임시 PostgreSQL에 **versioned portable checkpoint와 catalog dependency subset**을 복원하고 동일 수집 코드를 사용한다. 로컬 전체 DB·수천만 사실을 원격 DB로 복제하지 않는다.

체크포인트에는 manifest ID, 고정 종목/발행사 ID, 소스·요청 identity, 완료 구간·재시도·원래 capture anchor·fencing epoch·rate resume·반복 호출 중복 방지 키가 들어간다. 변경분과 체크포인트를 같은 commit manifest에 묶어 업로드 이전에 커서를 전진시키지 않는다. 실패 시 마지막 committed checkpoint를 복원한다.

첫 bootstrap은 로컬에서 대상·catalog·pending state를 이식하고 부모 증거를 hash-addressed 객체로 전달한다. 증분에서는 변경된 catalog와 새 evidence만 내보낸다. 기존 동결된 fact는 updates 대신 append delta로 처리한다.

## 전달 방식의 우선순위

| 수단 | 역할 | 사용 조건 |
|---|---|---|
| 비공개 R2 Standard | committed batch·checkpoint·ack 전달 | 기존 설계 우선, 실제 계정 무료 잔여·접근·원자성 실증 |
| 로컬 폴더 | 개발·오프라인 transport | 네트워크 없이 같은 sync 계약 검증 |
| 비공개 Actions artifacts | 짧은 원격 실증·대안 | 무료 저장량·만료·미접속 기간 검증, 영구 이력 저장 아님 |
| Google Drive | 선택 전달 대안 | 계정별 여유·OAuth·versioned head 구현 검증 |
| Google Cloud Run Jobs | 선택 수집 실행 대안 | 무료 실행량·지역·컨테이너·전송까지 견적 확인 |

공개 Actions artifact에 공급자 데이터를 올리지 않는다. R2 bucket은 비공개이며 공개 다운로드 URL을 만들지 않는다. Secrets·로그·batch에서 인증정보를 제외한다. PUBLIC 코드와 PRIVATE 전략·데이터의 분리 검사를 배포 전에 수행한다.

## 무료 운영과 로컬 5시간

소스 호출 수·재시도·압축 크기·checkpoint 크기·R2 작업 수·월 예측량을 실제 dual-market 일회 증분으로 측정한다. 무료 월 운영은 그 결과로 판단한다. 기본 R2 저장량 경고 6GB·보류 8GB는 서비스 강제 과금 차단을 보장하지 않는 앱 내부 기준이다.

로컬은 가동 시작 때 sync와 빠진 작업을 확인하고, 무거운 작업을 1개씩 실행한다. 최대 30일 미접속을 기준으로 원격 변경분·필수 dependency·checkpoint의 실측 용량을 인수한다. 일일 백그라운드 wall-clock 창은 최대 240분+안전 종료 여유로 시작해 전체 300분 안에서 pause한다. worker가 멈추면 checkpoint·진행 상태를 남긴다. PC 자동 종료나 이번 기획 중 기존 수집기 중단은 수행하지 않는다.

이 240/300분 운영 정책은 일상 앱의 기본 설정이다. 이번 기획·구현·검증 작업의 수행 시간을 5시간으로 끊는 조건이 아니다. source 장애는 같은 source의 bounded retry/resume·gap으로 처리하고 다른 공급자로 자동 이동하지 않는다. 원격 실행/저장도 자동 유료 이용 상한은0원이며 월합계10,000원 이하 유료 대안은 사용자에게 제안한 뒤 이용 여부를 확인한다.

미수신 batch를 무료 용량 확보 목적으로 삭제하지 않는다. 로컬 SQL 적재+검증+backup ack 이후에만 정리한다. 공급자에서 과거 버전을 다시 받을 수 없으면 저장 부족이 영구 gap을 만들 수 있으므로 그 위험을 조용히 success로 바꾸지 않는다.
