# 로컬 웹앱과 금융 차트 PRD

Windows에서 launcher로 열고 브라우저에서 쓰는 1인용 한국어 연구 앱이다. PostgreSQL과 API는 loopback에 바인딩한다. 클라우드 UI 호스팅이나 24시간 PC 가동을 요구하지 않는다.

## 화면

| ID | 화면 | 주요 기능 |
|---|---|---|
| W01 | 홈·데이터 상태 | 마지막 eligible 세션·수집/동기화·coverage·시장 레짐·blocked 신호·자원 예산 |
| W02 | 종목 탐색 | 한국어/영어/코드·시장·상품·상폐·정지 필터, 현재/과거 별칭 |
| W03 | 종목 상세·차트 | 캔들·거래량·가격 basis·지표·공시·기업행동·신호·재무·quality |
| W04 | 백테스트 실험 | 전략·기간·universe·비용·모드 입력, async 진행·취소·재개 |
| W05 | 결과 비교 | equity·drawdown·월별·ledger·레짐 성과·OOS·편향·cost |
| W06 | 종목 스크리너 | 가격 모멘텀·재무 개선·평균회귀 상태·제외 사유·CSV |
| W07 | 전략 스크리너 | OOS·레짐 성과·trial 이력·사용자 selected와 validation 독립 표시 |
| W08 | 시그널·보유·기록 | 근거·만료·충돌·수동 보유·확인·메모·체결 입력 |
| W09 | 작업·설정 | 큐·실행 예산·메모리·paused·backup·비용·공급자 상태 |

전략 화면은 Python 플러그인 목록·문서·required inputs·파라미터 schema·실행/비교를 제공한다. 화면 조건 조합 builder·임의 코드 입력/실행 endpoint는 초기 제외다. 신호는 연구용과 selected 전략 결과를 나누고 사용자 선택·데이터 품질·성과 validation을 독립 표시한다.

## 차트 라이브러리 선택

TradingView의 [Lightweight Charts](https://tradingview.github.io/lightweight-charts/docs)를 기본 후보로 삼는다. 캔들·선·거래량·crosshair·time scale·pane·marker를 앱 API의 로컬 데이터로 그린다. 라이선스·NOTICE와 TradingView 링크를 유지한다. 데이터 공급·지표·도형·Pine 해석기·저장 기능은 앱의 별도 구현 범위다.

Advanced Charts·TradingView 웹 서비스 전체와 동등한 기능을 약속하지 않는다. 외부 widget은 로컬 SQL 시점·종목·기업행동 계약을 만족하는 기본 차트 경로로 쓰지 않는다. 성과 차트는 단순 web chart로 통일하고 필요한 경우 Plotly를 별도 분석용으로 실증한다.

## UX 원칙

데이터 세션·가격 basis·통화·research tier·gap은 차트와 결과 가까이에 표시한다. 그래프가 그려진다고 데이터를 clean/PIT로 표시하지 않는다. 상폐·정지 종목도 검색 가능하며 거래가능 상태와 별개로 역사 차트를 보여준다.

가격 색상은 기본 한국식 상승 빨강·하락 파랑을 후보로 두되 사용자 설정으로 미국식 상승 초록·하락 빨강도 지원한다. 색상만으로 의미를 전달하지 않고 텍스트와 아이콘을 함께 사용한다. 다크 기본·라이트 옵션, 키보드 검색·정렬·필터·뒤로 이동을 지원한다.

## 로컬 실행과 부하

launcher는 DB·API·worker 상태를 확인하고 이미 켜져 있으면 재사용한다. 앱 종료·작업 일시중지·서비스 종료를 구분한다. 작업은 하루 5시간 사용 목표 안에서 중단·재개할 수 있고, 무거운 분석은 초기 1개만 실행한다.

offline에서 저장한 릴리스·차트·실험·스크리너를 읽을 수 있다. 새 수집·현재 시그널은 stale·blocked 상태를 보여준다. 초기 설치와 업데이트에 네트워크가 필요할 수 있으나 실행 화면은 CDN이나 외부 폰트를 필수로 요구하지 않는다.

## 완료 조건

빈 데이터·부분 데이터·상폐·정지·결측 재무·worker 오류·RAM 부족·오프라인·만료 시그널의 화면이 정의돼 있어야 한다. 차트는 10년 이상 기간의 필요한 구간을 paging으로 불러오고 대량 종목 표는 서버 pagination/virtualization으로 처리한다. 원계정 전수·audit 이력을 매 화면 요청마다 scan하지 않는다.

1920×1080을 기준 desktop layout으로 구현하고 1280×720까지 주요 기능을 사용 가능하게 한다. 모바일·LAN은 초기 인수 범위가 아니다. 화면·endpoint 계약은 [UI와 API 명세](12-ui-api-spec.md)를 따른다.
