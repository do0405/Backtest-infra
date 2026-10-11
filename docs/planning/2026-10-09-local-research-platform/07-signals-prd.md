# 장 마감 후 참고 매수 매도 시그널 PRD

시그널은 등록 전략 규칙을 현재 입력에 적용한 결과다. 모든 전략은 연구로 시작하고 연구용 신호를 별도 표시한다. 사용자 최종 선택으로 selected 목록에 올리되 데이터·성과 검증 상태는 별도로 유지한다. 주문 전송·브로커 연결·자동 체결은 제공하지 않는다. 실제 보유와 가상 포지션을 구분한다.

## 요구

| ID | 요구 | 완료 조건 |
|---|---|---|
| S01 | 동일 전략 정의 | 백테스트·스크리닝과 같은 definition hash 사용 |
| S02 | 등급·신선도·거래가능 gate | research/selected·세션·필수 데이터·정지·상품·data policy와 validation 별도 검사 |
| S03 | 근거 설명 | 입력값·조건·레짐·품질·유효 세션·정의 링크 제공 |
| S04 | 수동 보유 연결 | reference portfolio snapshot ID와 actual/simulated 구분 |
| S05 | 충돌 표시 | 여러 전략의 반대 신호를 conflict로 표시 |
| S06 | 정정과 만료 | append-only supersession, 다음 eligible 세션 기준 expires |
| S07 | 외부 전송 통제 | 초기 앱·CSV만, 요청 없는 메시지 발송 없음 |
| S08 | 수동 행위 원장 | 확인·메모·무시·체결 입력을 시스템 signal과 별도 보존 |
| S09 | 멱등성 | 동일 strategy·security·cutoff·release·portfolio·generation key 중복 없음 |
| S10 | 주문 경로 부재 | UI·API·dependencies에서 submit order 기능 없음 |

## 시그널 상태

| 상태 | 화면 의미 |
|---|---|
| entry_candidate | 미보유 또는 비중 증가 후보, 다음 세션에 다시 가격·거래가능 확인 |
| exit_candidate | 전략상 청산 조건; 실제 매도 검토는 수동 보유 연결 시 표시 |
| hold | 조건 유지·전략 포지션 유지 |
| watch | 조건 일부 충족, 아직 진입·청산 조건 미완료 |
| blocked | 해당 계산·사용 등급의 필수 입력·거래가능·품질 조건 부족 |
| conflict | 같은 사용 등급·보유 연결 범위에서 같은 증권에 상반된 전략 결과 |
| expired | 유효 세션·시각이 지나 참고 후보 유효성 종료 |
| superseded | 정정·새 계산·변경 설정으로 새 신호가 대체 |

`exit_candidate`는 실제 보유 여부를 모르는 전략 청산 조건일 수 있다. 이 경우 화면은 “전략 청산 조건”으로 표시하고 실제 계좌 매도 수량을 생성하지 않는다. 수동 보유 snapshot이 오래됐으면 계좌 행동 참고 영역을 blocked로 표시한다.

## 생성 흐름

1. 시장의 완료 세션과 공급자 finalization을 확인한다.
2. 고정 release·cutoff·feature snapshot·model version·전략 selection/validation 상태를 선택한다.
3. 필수 데이터의 freshness·상품 적용성·기업행동·거래상태·시점 정책을 확인하고 research_only/selected-reference/blocked를 정한다.
4. 같은 전략 함수를 실행해 desired state/target weight와 근거를 계산한다.
5. 직전 전략 state 및 수동 portfolio snapshot과 비교해 참고 event를 생성한다.
6. conflict·blocked를 적용하고 signal batch를 SQL에 고정한다.
7. 이후 정정과 새 세션은 기존 event를 수정하지 않고 새 이벤트로 연결한다.

`selection_state`, `strategy_validation`, `data_quality`, `signal_use_scope`는 독립 표시한다. 연구용 신호·사용자가 선택한 전략의 참고 결과를 서로 다른 탭과 export 분류로 제공한다. 선택은 검증 인증이 아니고, 데이터 gate를 우회할 권한도 아니다. 과거 재무 PIT나 무결성 실패는 연구용에서도 계산을 차단한다.

아직 선택하지 않은 전략도 적격 입력으로 연구용 신호를 만들 수 있다. 선택 여부만으로 research 계산을 blocked로 바꾸지 않는다. conflict는 원래 각 전략의 event·근거를 보존하고 별도 충돌 표시를 추가하며, 상반된 결과를 임의 상쇄하거나 하나로 숨기지 않는다.

진입 판단은 DEC-19의 전략 풀에서 수행한다. 보유 종목의 매도/유지는 진입 풀 제외만으로 생략하지 않는다. 기본 필수 입력 검사를 유지하고 명시한 전략만 작은 entry/exit 입력 분리 옵션을 사용한다. unavailable·빈 후보·생략 target을 hold나 비중0으로 바꾸지 않는다. 같은 EvaluationContext의 historical/current 판단과 선택된 definition bundle을 [20 계약](20-pipeline-and-extension-contracts.md)으로 검증한다.

이전 closed session이 최신이라고 PC의 이전 신호를 계속 활성으로 유지하지 않는다. PC를 켰을 때 예정 거래 세션이 이미 지났다면 expired로 표시한다. 미국·한국 개장 시각은 각 시장 달력을 사용한다.

## freshness 정책

가격은 마지막 완료 세션의 required bar가 있어야 한다. 단순 `now−capture<24h` 대신 주말·휴일·조기종료를 반영한다. 재무는 periodic filing 유형별 허용 age와 새 공시/정정 discovery 상태를 확인한다. 기본 후보는 분기 특징 180일, 연간 특징 450일이지만 시장·보고 유형별 정책으로 고정하고 계산 결과에 포함한다.

필수 corporate action이 unknown이면 해당 계산·회계를 차단한다. 공식 거래가능 이력의 unknown만 남으면 DEC-17에 따라 적격 현재 입력의 연구 신호를 제공할 수 있고 `tradability_assumed`와 한계를 표시한다. 아직 오지 않은 다음 세션 OHLCV는 신호 생성 입력으로 요구하거나 미리 읽지 않는다. 현재 확인된 정지·거래 제약은 blocked이며, 거래가능 검증 등급은 unknown을 체결 증거로 인증하지 않는다. 허용된 source-defined 가격 탐색의 연구 결과는 품질 한계를 별도 표시한다. 데이터가 늦어졌다고 전날 값을 조용히 대신 쓰지 않는다. 새 소스는 qualification된 별도 dataset/config의 명시 선택으로만 사용하며 실행 중 자동 전환하지 않는다.

## 예시 카드

`재무 개선 모멘텀 / 종목 A / 진입 후보 / 매출 YoY 12%, 영업마진 +2.1%p / 공시 사용 가능 시각 ... / 가격 세션 ... / 다음 eligible session ... / 수동 보유 연결 없음 / 실제 수량 미산출`

예시는 화면 계약용이며 특정 종목 추천이나 전략 성과 사례가 아니다. 실제 카드에는 security_id·release_id·strategy hash·as_of·expires_at·input_refs·quality reasons를 포함한다.

## 인수

정지·새 공시 누락·재무 capture-only historical validation·stale portfolio·정정·반대 전략·시각 만료 fixture에서 올바른 blocked/conflict/expired/superseded가 나와야 한다. 가격의 늦은 수신만으로 과거 계산을 막지 않고, 선택 전 연구 신호도 제공한다. export도 화면과 같은 상태를 보존한다. 모든 수동 매매 입력은 감사 이벤트이며 외부 주문 호출을 하지 않는다.
