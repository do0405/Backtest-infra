# 확장 가능한 무료 백테스트 프레임워크 비교

기준일 2026-10-09 KST. 사용자 요구는 Python으로 전략을 추가하는 로컬 플랫폼이다. 일봉·한국/미국·ETF/ETN·상폐/정지·재무 특징·기업행동과 명시 모의체결을 지원하고, SQL 데이터·시점·품질은 앱이 소유한다. 이번 결과는 공식 문서·원천 코드에 기반한 후보 선정이며 설치·속도·전수 runtime 인증이 아니다.

## 문서 기반 평가 결론

**Zipline-reloaded를 첫 qualification 후보**, **LEAN 오픈소스 엔진을 두 번째 후보**로 검토한다. vectorbt는 대량 배열 연구의 조건부 도구이며 기본 회계 엔진으로 미리 고정하지 않는다. “더 좋은 프레임워크”는 인기도 대신 데이터 계약·기업행동·전략 확장·로컬 운영비·실제 검증 부담으로 판단한다.

사용자 입력을 받아 프레임워크 이름을 자동으로 바꾸는 런타임 fallback은 없다. 후보 검토→동일 contract fixture/SQL snapshot→지원 범위·자원 결과→engine lock의 순서로 도입한다. qualification 실패는 선택 미완료로 남기고 최소 기능을 구현했다고 다른 엔진 이름을 붙이지 않는다.

| 후보 | 확인한 근거 | 이 프로젝트의 추가 작업·부담 | 판단 |
|---|---|---|---|
| Zipline-reloaded | Python event-driven 연구, custom bundle/calendar, split·dividend entitlement/pay-date 관련 API | 한국 calendar/asset metadata·SQL snapshot ingest·PIT 재무 loader·일봉 fill policy·Windows 의존성 실증 | 일봉 주식/펀드 연구의 첫 후보 |
| LEAN 오픈소스 engine | Python/C#·모듈형 event-driven 구조, equity split/dividend/delisting 모델, source build 경로 | .NET/Python bridge·custom KR market/calendar·own data mapping·배당 clock/adjustment/fill-forward override·로컬 build 자원 | Python만의 간단한 배포보다 운영 부담이 크지만 확장 후보 |
| Backtrader | Python strategy/data/broker extension·analyzer 구조 | 기업행동·상폐·PIT 재무·전수 scale 계약의 추가 구현량을 실증해야 함 | 가벼운 전략 실증 후보, native 전체 지원으로 가정 금지 |
| NautilusTrader | Rust/Python event-driven·custom data·명시 backtest engine, 현재 버전의 플랫폼 지원 문서 | equity corporate-action 기능 요청/계정 상태 변경 이슈·v2 전환·Windows 환경 qualification | 현 일봉 equity 목적의 첫 선택으로 확정하지 않음 |
| vectorbt 무료 패키지 | 배열 기반 전략 연구·portfolio API | 이벤트 회계·동시 cash/수량·배당/정지/상폐·정수 lot의 adapter capability 검증 | 선택 연구 가속 도구, 기본 회계 대체 아님 |

표의 순위는 설계 판단이다. 어느 프레임워크도 무료로 한국·미국 역사 전체 데이터, clean/PIT, 실제 상폐 대가를 제공한다는 뜻이 아니다. 동일한 데이터·경제 사건을 지원하는지는 실제 adapter test로 확인한다.

## Zipline에서 확인할 사항

공식 bundle 문서는 자신의 일봉 데이터와 calendar 등록을 설명한다. adjustments API는 dividend의 ex/declared/pay/record date와 split 처리를 제공한다. SQL primary를 바꾸지 않고 고정 dataset의 engine 전용 캐시를 생성할 수 있는 후보로 판단한다. [bundles](https://zipline.ml4trading.io/bundles.html), [dividend/split API](https://zipline.ml4trading.io/api-reference.html).

자산 SID·symbol·상장 종료·auto-close 정책을 기존 security_id 및 lifecycle evidence에 매핑한다. engine의 자동 마지막 청산·휴일·가격 보간이 프로젝트의 정의를 바꾸면 override 또는 unsupported로 처리한다. corporate actions가 있다는 이유로 공개 clock·정정 버전까지 우리 PIT 계약을 충족한다고 가정하지 않는다.

Python/dependency 지원은 현재 package 버전·wheel·NumPy/Pandas·C-extension을 실제 Windows 연구 venv에서 확인한다. 공식 repo와 문서 버전이 다를 수 있어 lock된 코드/API를 최종 기준으로 삼는다. 기본 수집 환경을 업그레이드하지 않는다. [공식 저장소](https://github.com/stefan-jansen/zipline-reloaded), [설치](https://zipline.ml4trading.io/install.html), [license](https://github.com/stefan-jansen/zipline-reloaded/blob/main/LICENSE).

## LEAN과 유료 CLI를 구분

LEAN engine은 Apache-2.0 오픈소스이고 공식 repo는 직접 build/run 경로를 제공한다. 반면 공식 LEAN CLI 문서는 사용에 paid-tier organization이 필요하다고 명시한다. 무료 설계에서는 CLI·cloud·vendor data purchase를 당연한 설치 단계로 포함하지 않는다. [engine](https://github.com/QuantConnect/Lean), [직접 실행](https://www.quantconnect.com/docs/v2/lean-engine/getting-started), [CLI 조건](https://www.quantconnect.com/docs/v2/lean-cli/key-concepts/getting-started).

기본 equity normalization은 project의 raw/PIT-adjusted 정의와 다를 수 있다. 전체 미래 기업행동을 쓴 adjustment·fill-forward·margin·dividend cash 적용 시각을 확인하고 명시 설정으로 바꾼다. native cash dividend가 있다는 사실을 정확한 실제 pay-date receivable 모델로 인증하지 않는다. 한국 custom data/calendar와 source evidence를 연결한 probe가 필요하다. [normalization](https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/requesting-data), [portfolio source](https://github.com/QuantConnect/Lean/blob/master/Common/Securities/SecurityPortfolioManager.cs).

## 나머지 후보의 확인 경계

Backtrader의 공식 strategy/broker 구조는 Python 확장에 적합한 후보 근거다. 그러나 이번에는 기업행동·상폐·회계 정책의 실제 native 지원과 Windows 전수 성능을 인증하지 않았다. [공식 저장소](https://github.com/mementum/backtrader), [strategy 문서](https://www.backtrader.com/docu/strategy/).

Nautilus의 공식 repo에는 corporate-action 기능 요청과 v2에서 외부 position/account 변경 패턴에 대한 최근 질문이 있다. 이 보고는 qualification 위험 근거이며 현재 모든 버전이 불가능하다는 결론으로 쓰지 않는다. 해당 사건을 공식 API로 지원하는지, 지원하려면 자체 회계를 얼마나 추가해야 하는지 실증한다. [기능 요청](https://github.com/nautechsystems/nautilus_trader/issues/3307), [v2 계정 상태 질문](https://github.com/nautechsystems/nautilus_trader/discussions/4829), [설치 지원](https://nautilustrader.io/docs/latest/getting_started/installation/).

## qualification 필수 시나리오

1. Python 전략 플러그인을 추가·등록하고 엔진/SQL/UI 공통 코드 수정 없이 새 정의 hash로 실행한다.
2. 같은 frozen dataset·cutoff·비용으로 next-open·정수 lot·cash 부족·매도/매수 순서를 hand ledger와 대조한다.
3. split·cash dividend entitlement/pay date·reverse split remainder·상폐·정지·missing open·분모 유지 사건을 처리하거나 명시 unsupported로 반환한다.
4. 미래 공시·기업행동·membership을 append해 이전 strict decision/features가 바뀌지 않는지 확인한다.
5. 실제 SQL snapshot→engine cache→전략→stored result→API/CSV lineage를 연결한다.
6. KR·US 별 calendar/currency, predeclared scope와 source pin을 지키는지 확인한다.
7. small/medium/full targeted projection의 wall time·RSS·cache bytes·restart를 측정한다. full input 부족과 계산 scale 실패를 구분한다.
8. 손계산 oracle는 검증 도구로만 두고 생산 engine 대신 실행한 결과를 framework 결과로 표시하지 않는다.

선정 기록에는 tag/commit·license·환경 lock·수행 시나리오·미지원 사건·비용·actual engine를 포함한다. 통과 전에는 `selected_for_evaluation`이고 `qualified_default_engine`이 아니다. 무료 운영을 만족한 후보가 없으면 근거와 대안을 제시하며 paid CLI나 자작 전체 엔진으로 조용히 이동하지 않는다.
