# 요구 추적과 인수 시나리오

제품 요구가 명세·작업·검증에 연결돼야 구현 완료를 판단할 수 있다. 아래 테스트는 신규 구현 후 수행할 인수 항목이며 현재 통과 결과가 아니다. fixture 통과, 실데이터 통과, 공급자 coverage 인증을 서로 대신하지 않는다.

## 요구 추적

| 요구 | 정의 문서 | 핵심 명세 | 구현 작업 | 인수 |
|---|---|---|---|---|
| P01/P03/P12 로컬 이력·시점·불변 | 02/03 PRD | 09/10 | 03–06/08–09 | T01–T10/T14/T25 |
| P02 전수 대상·기간 | 02/03 | 10/13 | 02–04/20 | T01–T04/T29 |
| D01/D02/D05 역사·달력·정지 | 03 | 10/11 | 02–03/07 | T01–T05/T10 |
| D03/D04 원가격·기업행동 | 03 | 10/11 | 04–06/12 | T05/T08/T16/T18 |
| D06–D09 재무·개선·가치 | 03 | 10/11 | 04/06/11 | T06–T09/T17 |
| D10/D11 상품·FX·benchmark | 03 | 10/11 | 02–04/12/14 | T04/T16/T18/T19 |
| D12 전체 coverage | 03 | 10 | 04–06/20 | T02/T09/T29 |
| P08/I01–I12 원격·무료·재개 | 04 | 10/13 | 07–10/19–20 | T10–T15/T26/T27/T30 |
| P04/B01–B05 회계·재현 | 05 | 10/11 | 06/11–14 | T06/T08/T16–T20 |
| B06–B10 평가·framework·부분 연구 | 05 | 11/18 | 06/13–14 | T09/T19–T21 |
| P05/R01–R06 레짐 | 06 | 11 | 11/15 | T20/T21 |
| C01–C07 두 스크리너 | 06 | 10/11/12 | 15/17–18 | T02/T04/T17/T22 |
| P06/P10/S01–S10 참고 신호 | 07 | 10/11/12 | 14/16–18 | T09/T23–T25/T28 |
| P07/W01–W09 앱·차트 | 08 | 09/12 | 17–19 | T22–T24/T27/T28 |
| P09 전략·수학 확장 | 06/17 | 09/11 | 01/06/11/13 | T21/T33 |
| P11 수동 기록 | 07/08 | 10/12 | 16/18 | T23/T25 |
| 하루5시간·저부하 | 00/04/08 | 13 | 01/19 | T27 |
| G01/G09/G10 요청·engine/source 보존 | 17 | 05/09/10/18 | 02/06/07/13/17 | T31/T32 |
| G02/G17/G20 범위·완료·차단 분리 | 17 | 09/14 | 01/11/17/20 | T33/T37/T41 |
| G03/G04/G14 시각·값·이력 | 17 | 03/10/11 | 03–06/09/11/16 | T08/T09/T34/T39 |
| G05/G12/G19 정의·검증·연구 | 17 | 06/11 | 11–15/20 | T33/T38/T42 |
| G06/G07/G08/G13 실제 경로·상태·운영 | 17 | 09/10/12 | 06/13/16–18/20 | T35/T37/T41 |
| G11 독립 기준과 실제 경로 인수 | 17/18 | 11/14 | 12/13/17/20 | T19/T35/T41 |
| G15 자원·전수 분모 | 17 | 13 | 01/05/06/19 | T27/T36 |
| G16 명시 복구·재시도 | 17 | 10/13 | 07–10/13/17 | T11–T15/T31/T32 |
| G18 비용·방법론 부담 | 17/18 | 13/14 | 01/13/20 | T40 |
| D01/D03/D06/B02/B04/B05/R03/S02/S06/G03/G04/G08/G13–G17/G20 사례 재발 방지 | 19 | 03/10/11/13 | 03–07/11/15–20 | T43–T48 |
| B02/B03/S02/G03/G04/G14 연구 체결 가정 | 17/19 DEC-17 | 05/07/10/11/12 | 06/12/13/16/20 | T49 |
| B01/B02/B05/C01/C03/C04/R03/R05/S01–S04/G01/G02/G03/G08/G13/G14/G18 흐름 연결·확장 | 20 | 06/07/09/10/11/12 | 06/11–18/20 | T50–T56 |

각 실제 테스트 결과에는 requirement IDs를 metadata나 인수보고서에 적는다. broad range가 실제 구현에서 빠지면 하위 요구별 행을 추가한다.

## 56개 인수 시나리오

| ID | 입력·사건 | 기대 결과와 증거 |
|---|---|---|
| T01 | 오늘 ticker A가 과거 다른 증권에 쓰임 | 시점별 다른 ID, ambiguous/current assertion 소급 없음 |
| T02 | 상폐·정지·unsupported·새상장·기간 결측 포함 전체 target | 분모 유지, 상장 전 not_applicable·누락 missing·상품 상태 분리 |
| T03 | 중간 상폐·합병 및 terminal payment 누락 | 미해결 사건 기록, 임의 마지막 가격 무한 유지 금지, strict 결과 차단 |
| T04 | 주식·우선·ADR·ETF·ETN 같은 issuer/product 사례 | distinct security, 회사 재무 적용성 분리, ETN 만기·leveraged opt-in |
| T05 | official halt vs supplier volume0+동일OHLC | 공식 정지와 미검증 quote 구분, 해당 세션 fake fill 없음 |
| T06 | 분기 종료 뒤 2개월 후 공개 공시 | 공개 이전 feature unavailable, 이후만 유효; cutoff/lineage 확인 |
| T07 | 나중 정정·CFS/OFS·비12월·YTD·negative denominator | 원값 보존, compatible 분기·정정만 선택, unavailable reasons |
| T08 | 2026수신의2018가격·늦은 재무·legacy observed/public reader | 신규 가격 reader는2018세션 지표 허용, 재무 PIT·old reader/봉인 불변 |
| T09 | supplier-defined 탐색·future 재무·선택과 validation 혼합 | 가격 탐색 등급 보존, 재무 미래 정정/공시 거부, 선택이 검증 인증으로 바뀌지 않음 |
| T10 | 미국 DST·조기폐장·휴일·늦은 SIP·cron missed run | 완료 세션만 생성, missed interval catch-up, 미래/미완료 봉 사용 없음 |
| T11 | payload upload 중 runner 종료·head commit 응답 소실 | 이전 checkpoint 또는 검증된 committed head로 reconcile, cursor 과advance 없음 |
| T12 | duplicate·out-of-order·same ID different hash·parent 누락 | 멱등 0 신규 사실, conflict/quarantine, 누락 seq 건너뛰기 금지 |
| T13 | 파일 path traversal·gzip bomb·numeric/schema invalid | 허용 root/size/schema 검증 실패, SQL partial commit 없음 |
| T14 | raw write 뒤 SQL 실패, SQL commit 뒤 ack 실패 | 실패 cursor 미전진, receipt 재활용, 원 capture/ID 유지 |
| T15 | local/remote same provider·KIS KR+US·old runner 최종 check와 commit 사이 takeover | 단일 control CAS owner/head 갱신, old ETag commit 실패·shared budget·legacy drain 인계 |
| T16 | cash배당·pay date·split·reverse split·현금부족·fee | 기준 hand ledger cash/qty/NAV와 일치, dividend double-count 없음 |
| T17 | 가격/재무/stateful plugin 예시, 같은 dataset과 parameters | expected target·rank·hold/exit 일치, 재무 PIT·가격 cutoff·등록 version 준수 |
| T18 | t 종가 signal·t+1 open missing·정지·unresolved action | same-close fill 없음, pending/blocked reason, 미래 volume sizing 없음 |
| T19 | 후보 framework·손계산 oracle·benchmark·통화·cost | 회계 대조 또는 unsupported, actual engine/version·지원 경계·동일 지표 |
| T20 | 같은 fingerprint 재실행·holdout overlap·trial 변경 | 핵심 산출 재현, fit/test 분리, 중복 test 합산·숨은 trial 없음 |
| T21 | 미래 1년 추가 후 earlier regime/features 비교 | prefix 동일, confirmed 소급 없음, smoothing live 배제 |
| T22 | 결측 rank·동점·excluded·stock와strategy screens | stable tie-break·denominator·reason·화면/CSV 일치 |
| T23 | 연구/selected·보유 없음·stale actual·opposite strategies | selection/validation/data quality 독립, 전략 청산 vs 수동 보유 구분·conflict |
| T24 | PC 시작 시 신호 대상 세션 이미 지남·network offline | expired/stale 표시, 기존 차트·결과 조회 가능 |
| T25 | 정정·새 strategy version·수동 체결 입력 | 기존 signal/run immutable, supersession·journal lineage·새 계산 |
| T26 | 100만 행 초과·cache corruption·release policy mismatch | bounded streaming·memory 증거, fail/rebuild·임의 policy upgrade 없음 |
| T27 | 가용 RAM 부족·disk reserve·240/300분·KST 날짜 경계 | worker1·pause·checkpoint, 기존 외부 process 보호·자동 OS 종료 없음 |
| T28 | API 외부 접근·잘못된Origin·UI lifecycle·license·order route | loopback·token·Origin 차단, cleanup/NOTICE, 주문 endpoint 없음 |
| T29 | full scope10년 이상·신규 역사종목·원 source pin | 가격 세션/재무 공개 profile 전수 report, full clean이 미완이면 grade/gap 보존 |
| T30 | 30일 offline·R2 8GB·미수신+backup ack·임시DB restore | unacked 보존·budget hold·chain/dependency 보호, 복원 closure·run lineage 일치 |
| T31 | 명시 engine 미지원·qualification 미완·default alias | 자동 후보/손계산 engine 전환 없음, 명시 실패·actual engine hash·predeclared default resolution |
| T32 | source429/auth 오류·다른 공급자 정상 | 원 source retry/blocked, 다른 소스로 자동 채움 없음·independent dataset 분리 |
| T33 | 새 Python 전략 등록·미구현 HMM 요청·signal-only sizing 누락 | core 수정 없는 extension·state 인수, 고급 구현/상수 confidence 없음·sizing 명시 요구 |
| T34 | 2018feature에2019/2026봉·2024정정의2022재무·future fit | 가격 future/prefix 누출 차단, 재무 당시 버전만, 늦은 가격 수신은 차단 사유 아님 |
| T35 | 운영 route DB 실패·demo/mock/random 데이터 | 고정 성공 JSON/임시 차트 반환 없음, origin/env 거부·실제 오류/부족 상태 |
| T36 | 전체 대상 계산의RAM/time 부족·청크 경계 rank | chunk/spill/pause·전수 분모 유지, 앞N종목 축소·chunk별 잘못된 rank 금지 |
| T37 | 품질 일부 미완·실제 결과·unavailable·정상 no-position | 등급/실제 산출/이유·데이터 scope가 API/화면/export에서 일치 |
| T38 | 예시 수식·RSI 보유5세션·cost·허용 오차 변경 | hand oracle 일치, 다른 규칙/비용0 자동 치환 없음·definition 변경 근거·hash |
| T39 | 사용자 선택·동일전략 신호·데이터 gap·정정 | 최종 사용자 selected event만, validation 불변·필수입력 blocked·old signal 보존 |
| T40 | paid API/CLI/PRO·월1만원 제안·확인 없음·quota 부족 | 자동 이용0원·확인 없는 이용 없음·전체 비용 제안·보류/미수신 보존 |
| T41 | 함수/fixture 통과지만 SQL/worker/API 미연결 | implementation/data/operation/result 별도, 미연결 성공·필수N/A·전체완료 주장 금지 |
| T42 | 좋은 결과를 얻으려 후보/holdout/기대값/조건을 사후 변경 | 이전 실패/trial 보존·새 version·분리 평가, 테스트 완화·자동선택 없음 |
| T43 | 756봉 초과·9분기 이상·cache eviction·같은 source 정정 | SQL 역사/재무 버전·이전 봉인 증거 보존, cache 창으로 원본 삭제 없음 |
| T44 | 과거 잘못된 가격 행+정상 target·필수 window gap·시장/hash 실패 | 최소 영향 범위 격리·적격 결과 계속, gap 삭제/보간 없음·무결성 실패는 dataset 차단 |
| T45 | 일부 종목 실패·source 순회 완료지만 재무 부족·복구된 traceback·invalid manifest | job/coverage/quality/result 분리·정확한 gap 큐 재개, 전체 반복/로그만으로 실패 판정/허위 coverage 인증 없음 |
| T46 | current-list-only 역사 탐색·상폐 목록 부족·verified membership | current_membership/생존편향 표시·strict 역사 인증 거부, full target 분모 유지 |
| T47 | stateful 전략/기본 레짐의 chunk·재시작·창 이동·미래만 추가된 release·과거 정정 | 전체 순차 재생과 상태/결과 일치, prefix 검증 후 증분 재사용·과거 영향 시점부터 재생·old run 불변 |
| T48 | 최신 수집 실패·과거 성공 결과·다른 release cache·만료 신호 | source date/current attempt 분리, 과거 조회 유지·오늘 입력/active 신호로 대체 없음 |
| T49 | valid raw/양수 거래량·unknown 거래가능·known halt·zero/missing volume·strict/연구 정책 | 연구 정책만 시가 모의체결 허용·unknown/가정/API/CSV 표시, 알려진 차단 유지·미래 거래량 signal/sizing 유입 없음·strict 자동 전환 없음 |
| T50 | 시점별 재무 개선 pool·현재 상위목록·selector schedule·rank 모집단 | 동일 Python 선택 함수로 historical/current pool 재현, 현재 목록 소급·후보끼리 정규화·수집 target 축소 없음 |
| T51 | 빈 진입 풀·풀 밖 보유·생략 target·명시 target0 | 보유 매도/유지 관리 지속, 생략/unknown을 자동 청산으로 해석하지 않음·선언 목표 범위 유지 |
| T52 | 공통 필수 입력 부족·명시 entry/exit 입력 분리·무결성 실패 | 기본 전략 gate 유지, 작은 명시 옵션만 독립 판단·status/reason 보존, 추정 분리/가짜 성공/무결성 완화 없음 |
| T53 | display-only 레짐·진입/비중/청산에 선언한 레짐·unknown·미구현 fit 요청 | 선언한 역할만 영향, 같은 clock/모델 계약·unknown 유지·고급 placeholder 결과 없음 |
| T54 | 같은 context의 백테스트/현재 판단·다른 actual/simulated state·pool/모델 정의 변경 | 동일 입력은 동일 판단, 보유 차이 구분·bundle refs 보존·selected에 새 정의 자동 복사 없음 |
| T55 | 30일 공백·순차 state 복원·뒤늦은 연구 신호·수동 actual 원장 | 선택된 계산 위치/보관 정책 준수, generated/evaluation 시간 분리·과거 신호 비활성·모의 거래 actual 유입 없음 |
| T56 | 지원 타입의 새 feature/selector·독립 assessment·미지원 데이터 타입·미래 공개 입력 | 기존 Python registry/context 확장, 연결한 판단만 영향·시간/단위/비용 검증·미지원 차단·고급 구현 자동 도입 없음 |

T13의 uncompressed bytes/row count/ratio는 transport 실측으로 상한을 고정한다. T21은 초기 rule/feature·extension clock의 prefix 검증이고 고급 방법 구현을 요구하지 않는다. 희귀 기업행동을 확보하지 못하면 fixture 인수와 actual-source gap을 구분한다. T31–42는 금지된 대체·임시 완료·비용/범위 변조가 생기지 않는지 확인한다. T43–48은 19의 Invest Prototype 사례 재발 방지이며 T49는 사용자 DEC-17의 연구용 모의체결 경계다.

T50–56은 20의 연결 계약이다. 초기 지원 타입의 작은 Python/rule 함수로 extension을 검증하며 HMM·학습 모델·새 데이터 종류의 실제 수집을 요구하지 않는다. pending DEC-20–23은 선택이 확정된 범위로 실행하고, 답변 전에 원격 신호·공매도·공유 자금·실제 수량을 필수 인수로 확대하지 않는다.

## 구현 완료와 데이터 목표 완료

**앱 구현 완료**는 Python 전략 확장·qualification된 framework·등급 있는 실제 결과/진단·실제 SQL/API/UI·sync/복구·런처의 Task01–20 인수를 통과한 상태다. 예시 네 전략을 모두 넣는 것이나 fixture 화면만으로 대신하지 않는다. 필수 actual-source/운영 연결이 막혀 있으면 전체 앱 완료와 분리 보고한다.

**전수 clean 목표 완료**는 한국·미국 역사 전체·최소10년 유효 구간의 가격 세션·basis/action·identity/calendar·재무 공개 버전 PIT·evidence closure를 검증한 상태다. 가격 수신 날짜가 늦다는 사실만으로 미달 판정하지 않는다. 실제 gap과 탐색 시계열 등급을 숨기지 않고 fixture·원행 수·현재 목록으로 전수 인증을 대신하지 않는다.

**시그널 기능 완료**는 연구/사용자 selected·data/validation 독립 표시, 실제 plugin 계산·freshness·포지션·만료·정정·주문 부재를 검증한 상태다. selected라는 이유로 필수 입력 실패를 우회하지 않는다. 연구 신호도 실제 입력과 해당 data policy로 계산해야 한다.

## 인수보고서 형식

각 항목에 requirement, test ID, commit/version, fixture or actual source, command, outcome, elapsed/RSS, artifact ref, qualification gaps, follow-up을 기록한다. pass·fail·blocked·not_applicable를 사용한다. 실행하지 않은 항목은 pass로 채우지 않는다.

현재 문서 묶음의 인수는 파일 존재·내부 링크·요구 연결·중요 기본값 일관성·독립 명세 review다. 이 작업에서 신규 플랫폼 runtime·공급자·전략 성과를 검증했다는 의미는 아니다.
