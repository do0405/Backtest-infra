# 전체 흐름의 연결과 확장 계약

2026-10-09 KST 기획 보완. clean SQL·무료 증분 갱신→가격/재무 기반 백테스트→현재 전략 풀/레짐→참고 신호의 연결을 정의한다. 레짐은 전략의 입력이 될 수 있고, 후속 데이터·방법은 여러 판단 위치에 연결할 수 있다. 고급 방법의 실제 구현·새 유료 의존성·범용 workflow 시스템을 이번 범위에 추가하지 않는다.

## 추가 질문과 결정 상태

기존 DEC-01–17은 유지한다. 아래는 전체 흐름을 점검하며 발견한 연결 선택이다. 사용자 답변과 설계 기본안을 구분한다. pending을 확정으로 처리하지 않는다.

| 항목 | 상태 | 사용자 답변 또는 정할 내용 |
|---|---|---|
| DEC-18 판단별 입력 검사와 복잡도 | confirmed direction | 사용자: 구현 복잡도를 높이지 않는다. 설계 기본안: 기존 전략 공통 필수 입력 검사를 기본으로 두고, 필요한 전략에만 entry/exit 입력 목록 분리 옵션을 제공한다. 범용 조건/의존성 엔진은 만들지 않는다 |
| DEC-19 스크리너와 전략의 관계 | confirmed | 사용자: 전략별 풀이 진입 판단 풀이다. 과거 각 시점과 현재에 같은 Python 선택 함수를 사용한다 |
| DEC-20 원격/로컬의 계산 경계 | pending | 원격 데이터 수집·전달 후 로컬에서 일별 연구/현재 신호 계산·저장인지, 원격의 무료 한도에서 신호 계산까지인지 재질문. 사용자 원격 자동 과금 지양은 이미 반영 |
| DEC-21 초기 거래 범위 | pending | 롱온리·현금 기본안 또는 초기 공매도·차입 포함. 상품 데이터 보존 범위와 체결/회계 지원은 별개 |
| DEC-22 한국/미국의 자금 | pending | 시장별 독립 KRW/USD 연구 계좌 또는 초기부터 환전·현금 공유 포트폴리오 |
| DEC-23 현재 신호의 수량 | pending | 조건/근거·선언 목표 비중 기본안 또는 초기부터 수동 계좌 입력에 근거한 실제 참고 수량 |

답변 전에는 이미 문서화된 기본안과 구현 범위를 유지한다. 특히 새 원격 신호 생산자·공매도/차입·공유 자금·실제 계좌 수량 기능을 구현 완료 조건에 몰래 추가하지 않는다. 실제 엔진 호환·무료 공급자 coverage·성능 수치는 질문의 답변으로 인증할 수 없으며 별도 실증한다.

## 작은 공통 계산 흐름

연구 worker 안에서 등록된 Python 함수를 호출한다. 기존 registry·job·state·SQL 저장을 재사용하고 새 scheduler·workflow 언어·그래프 편집기·별도 저장 엔진은 만들지 않는다.

```text
고정 AnalysisDataset + EvaluationContext
  → 필요한 가격/재무 feature와 레짐
  → 전략별 Python pool selector
  → 진입 풀 + 관리 중인 포지션의 전략 판단
  → 백테스트: 모의체결·원장·평가
  → 현재 계산: 조건·근거·참고 신호
  → 각 결과 SQL 저장·화면 조회
```

레짐 사용 여부는 전략 설정이 선언한다. 레짐 화면을 켜는 행위가 전체 전략의 필터·비중·청산 규칙을 바꾸지 않는다. 새 데이터를 수집했다고 기존 전략이 그 데이터를 자동으로 쓰지도 않는다.

## 동일한 평가 문맥

`EvaluationContext`는 dataset/release·시장·의사결정 cutoff·calendar·universe scope·data policy·definition bundle·config·state/portfolio refs를 고정하는 작은 DTO다. 개별 단계가 실행 도중 latest 자료를 다시 찾지 않는다. 초기에는 09의 기존 DTO에 필요한 필드를 추가해 공유하고 새 컨텍스트 서비스나 관리용 DB를 만들지 않는다.

가격은 경제 세션 cutoff, 재무는 공개 버전 PIT를 적용한다. 미국·한국의 같은 달력 날짜를 같은 지식 시점으로 합치지 않는다. 신규 dataset은 동기화 commit·closure·적격성을 확인한 뒤 만들어 분석에 제공한다. 미완료 import를 최신 완성 dataset처럼 읽지 않는다.

같은 dataset·cutoff·pool 규칙·feature/regime 정의·config·state·portfolio 입력이면 백테스트의 폐장 판단과 현재 판단이 일치해야 한다. 실제 보유와 모의 보유가 다르면 수량/청산 대상이 달라질 수 있으므로 동일 결과 인증의 비교 조건도 기록한다. 신호 생성 날짜와 판단 대상 세션을 구분한다.

## 전략 풀은 진입 대상

`pool_selector(context, features, config)`는 후보 security IDs·rank/근거·eligible/excluded·입력 refs를 반환하는 등록 Python 함수다. 별도 no-code 필터 엔진은 만들지 않는다. 일/주/월 등 selector 실행 schedule과 그 결과 사용 시점은 전략 config에 선언한다. 백테스트가 오늘의 상위 종목 목록을 과거 전체에 적용하면 안 된다.

스크리너는 전략의 같은 selector 결과를 보여준다. 여러 전략의 합집합 화면은 각 전략의 pool membership·규칙·기준일을 유지하고 하나의 공통 매수 풀로 바꾸지 않는다. 시장 전체 재무 수집·보관을 현재 기술 후보로 축소하지 않는다. rank의 모집단도 선언한 시장/상품/적격 범위이며 진입 후보끼리 다시 정규화하지 않는다.

`entry_pool`과 `managed_positions`를 구분한다. 후보에서 빠진 보유 종목도 매도/유지 판단 대상에 남는다. 후보 제외는 자동 매도 사유가 아니고 청산·리밸런싱 규칙이 명시적으로 결정해야 한다. 거래정지·상폐·입력 부족으로 판단하지 못하면 해당 상태와 이유를 보존한다.

## 입력 부족과 목표 비중의 의미

기본은 `required_inputs`의 공통 검사다. 필요할 때만 `entry_inputs`, `exit_inputs` 옵션으로 독립성을 선언한다. 예를 들어 재무+가격 진입과 가격 청산을 명시한 전략은 진입 입력 부족을 entry blocked로 기록하면서 적격 exit를 제공할 수 있다. 선언이 없으면 플랫폼이 전략 코드를 추측해 필수 입력을 나누지 않는다. 시장 identity·종목 식별·hash·구조 무결성 실패는 관련 계산의 공통 차단이다.

결과는 entry/exit의 값·status·reason을 구분한다. unavailable을 False·hold·exit=0으로 바꾸지 않는다. 별도 계산을 요청한 규칙의 성공으로 표시하지 않는다. 이 옵션은 작은 DTO와 검사 함수로 구현하며 임의 조건식 해석기·동적 의존성 그래프를 만들지 않는다.

`StrategyDecision`의 target은 `explicit_subset` 또는 `full_portfolio` 범위를 선언한다. 생략된 종목·계산 불가 결과를 자동 비중0으로 처리하지 않는다. full portfolio는 관리 보유 각각의 명시 목표 또는 유지/판단불가를 갖춰야 하며, 명시 목표0만 청산 의사다. signal-only를 백테스트할 때는 기존 명시 sizing policy를 요구한다. 후보 풀이 비어도 기존 보유를 전부 청산하지 않는다.

## 레짐의 연결 위치

| 역할 | 의미 | 필요한 계약 |
|---|---|---|
| display/context | 상태와 근거 표시 | 전략 결정에 영향 없음 |
| entry_filter | 특정 상태에서 진입 허용/제한 | 동일 historical/current filter, unknown 처리 선언 |
| allocation | 선언된 비중·현금 정책의 입력 | 명시 sizing·현금/노출 제약·비용 |
| exit_rule | 선언된 청산 조건의 입력 | 보유 대상·효력 세션·pending exit |

역할은 Python 전략·config의 선택이다. 모델 출력의 명칭이나 점수만으로 전역 청산·비중 조절을 수행하지 않는다. 필수 레짐 입력의 unknown을 임의 정상 레짐으로 채우지 않는다. 레짐 사용/미사용 비교는 별도 config/run으로 저장하고 비용·평가 기간·품질을 맞춘다.

후속 학습 모델은 model definition·fit artifact·training cutoff·prediction cutoff·refit schedule을 구분해야 한다. 어떤 세션까지 학습했는지 없는 모델을 과거 판단에 넣지 않는다. 지금은 필드/프로토콜 자리를 두고 고급 학습·자동 retraining을 구현하지 않는다. 기존 rule regime의 인과성·state 재시작부터 인수한다.

## 선택한 전략과 관련 정의의 고정

선택은 전략 코드만이 아니라 연결된 pool·feature·regime 역할/정의·parameter·execution 계약의 `definition_bundle_hash`에 연결한다. 새 코드·pool 규칙·레짐 역할·필수 feature 정의 변경은 새 bundle/version이며 기존 selected를 자동 인증이나 새 선택으로 복사하지 않는다.

일일 데이터 갱신은 같은 정의로 새 dataset을 계산하는 작업이므로 매번 재선택하지 않는다. 후속 명시 refit schedule도 승인된 계산 계약과 그 세션의 새로운 fit artifact를 구분해야 한다. 새 dependency를 최신 alias로 조용히 resolve해 선택된 판단 의미를 바꾸지 않는다.

## 추가 데이터와 방법의 확장 자리

추가 데이터는 source adapter→원관측/evidence→시간·단위·identity·quality 계약→AnalysisDataset/FeatureView의 순서를 따른다. 가격의 세션 예외를 거시·공시·뉴스의 공개/vintage 시각에 자동 적용하지 않는다. 데이터 종류가 기존 저장/조회 타입으로 표현되지 않으면 명시 adapter·schema 확장이 필요하며, 모든 종류의 데이터가 SQL 변경 없이 추가된다고 약속하지 않는다.

새 feature·regime·selector·strategy·assessment는 등록 Python 함수/모듈, 버전·입력·출력 schema·단위·시점·state/artifact refs·capability·자원/비용을 선언한다. 지원되는 기존 타입의 새 계산은 registry로 확장하고 UI는 typed parameter·결과를 재사용한다. 새 source·외부 HTTP·SQL·브로커는 계산 plugin 안에 넣지 않는다.

초기 공통 출력은 bounded 숫자/상태/근거 표와 시계열이다. 새 방법이 이를 벗어나는 행렬·문서·이미지·복잡한 상호작용을 요구하면 별도 저장/view adapter가 필요하다. 모든 수학 방법의 저장/화면까지 공통 코드를 전혀 바꾸지 않고 지원한다고 과장하지 않는다.

방법의 출력은 feature, pool rank/filter, regime, 진입/청산, sizing 또는 독립 추가 판단에 명시적으로 연결할 수 있다. 독립 assessment를 계산했다는 사실만으로 매수/매도에 영향을 주지 않는다. 고급 방법의 구현은 DEC-13의 후속 범위이며 이번에 이름·상수 confidence·mock 결과로 동작을 가장하지 않는다.

## 매일 갱신과 복원

원격/로컬 계산 위치는 DEC-20 답변을 따른다. 원격 실행은 기존 자동 유료0원·무료량 reserve·미수신분 보존 조건을 유지한다. 무료량이 불명확하거나 부족하면 해당 작업을 보류/재개하고 자동 과금·대상 축소·다른 source 전환으로 해결하지 않는다.

로컬 동기화 후 필요한 세션을 순서대로 처리해 state·모의 포지션을 복원한다. 실제 계좌의 체결·현금 변화는 사용자 원장만 반영하며 연구 재생의 모의 거래를 actual 원장에 넣지 않는다. 누락 기간의 연구 신호를 저장한다면 generated_at은 실제 재생 시각, evaluation_at은 과거 세션이며 과거 세션의 유효기간을 소급 연장하지 않는다. 현재 active signal은 현재 cutoff와 freshness로 별도 생성한다.

가격 최근5세션·재무 최근90일 재확인과 장기 회전 scan은 04의 기존 초기 계획이다. source별 정정 탐지/재확인 범위·마지막 점검·미완료 구간을 기록한다. 매일 갱신이 모든 10년 데이터를 매일 다시 검증했다는 뜻은 아니다. 새 관측/정정의 새 release가 생기면 관련 feature·pool·regime·state의 cache/계산을 해당 identity로 무효화/재생하며 기존 고정 run은 보존한다.

state checkpoint는 definition/config·마지막 처리 세션·`input_prefix_hash`·입력 refs를 갖는다. 새 release에서 단순히 dataset ID가 달라졌다는 이유만으로 10년을 매일 다시 계산하지 않는다. 마지막 세션까지의 관련 입력·시점별 membership·quality/clock 정책이 같음을 검증하면 state를 재사용하고 새 세션만 처리한다. 신규 공시/봉이 과거 cutoff 이후에만 영향을 주는 경우도 이 경계를 따른다. 같은 종목 수·마지막 날짜·행 수만으로 prefix 동일성을 인증하지 않는다.

과거 가격/재무 버전·membership·정의가 영향을 주면 가장 이른 영향 시점과 전략 warmup/state 의존성을 기록하고 그 이전의 유효 checkpoint부터 재생한다. 유효 checkpoint가 없으면 필요한 전체 재생을 분할/재개한다. 기존 고정 run을 다시 쓰거나 source 정정 검증을 생략해 incremental 성공으로 표시하지 않는다. 비교는 release의 기존 membership/range hash·의존 metadata를 재사용해 bounded로 수행하며 화면 요청마다 전체 원본을 다시 scan하지 않는다.

## 구현과 인수

새 원격 분석 서버·workflow 엔진 없이 Task06/11/15/16의 공통 context·Python 함수·DTO를 보강한다. Task12/13은 목표 범위·체결/회계 capability를 대조하고 Task17/18은 상태와 실제 source date를 그대로 표시한다. 이 문서는 구현 완료 증거가 아니다.

| 확인 항목 | 인수할 반례 |
|---|---|
| pool의 시점별 동일성 | 오늘의 상위20을 과거에 적용·candidate-only 재무 수집·rank 모집단 변경 |
| 진입 풀/보유 관리 | 빈 후보 또는 후보 제외를 비중0/자동 청산으로 처리 |
| 작은 입력 gate | 기본 필수 검사 우회·entry 부족을 False로 바꿈·선언 없이 독립 exit 추정 |
| 레짐 연결 | 표시 모델이 전역 전략에 영향·future fit·unknown 정상화 |
| 판단의 재현 | 같은 context의 historical/current 결과 차이·selected bundle의 조용한 변경 |
| 복원과 계좌 | replay를 당시 발생/live 신호로 표시·모의 거래를 actual 원장에 입력 |
| 데이터/방법 추가 | 새 source의 시간 의미 생략·지원 타입 오인·새 방법의 암묵 진입 연결 |

답변이 끝난 선택은 17의 결정 원장과 관련 PRD·명세·구현 계획·15 인수표에 함께 반영한다. 더 많은 문서/추상화의 수가 완성도 기준은 아니며 실제 동일 판단·원본 보존·명시 상태·작동하는 확장이 완료 기준이다.
