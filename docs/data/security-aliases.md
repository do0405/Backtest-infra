# 종목 코드·이름 매핑

데이터의 연결 키는 기존 `security.id`이다. 티커·ISIN·공급자 코드·국문/영문명은 SQL `identifier_history`에 별칭으로 저장하고 해당 ID를 찾는 입력으로 사용한다. 원 가격·재무 데이터의 ID를 바꾸거나 별칭마다 데이터를 복제하지 않는다. 회사의 `issuer.id`와 거래 가능한 증권의 `security.id`는 구분한다.

## 등록 범위와 규칙

`universe sync-aliases`는 최신 target manifest 전체를 처리한다. 미국·한국의 주식·ETF·ETN 및 아직 상품 분류를 확인하지 못한 항목도 분모에 남는다. `universe sync`와 `universe link-issuers` 이후에도 등록한다. 디렉터리 코드·이름·식별자, 명시적으로 제공된 공급자 표기, OpenDART의 기본 종목 코드로 연결한 국문·영문명, SEC의 명시적 티커별 회사명을 등록한다. 회사명이 여러 증권에 해당하면 모호함을 유지한다.

- `005930`, `A005930`, `삼성전자`, `Samsung Electronics`는 삼성전자 보통주 ISIN `KR7005930003`의 기존 ID로 연결한다. 영문 약칭은 공식 이름을 근거로 등록한다. 사용자 요청에 따라 `Samung Electronics` 오타 별칭은 철회했으며 재등록하지 않는다. 오타를 유사도로 자동 매칭하지 않는다.
- 삼성전자우 `005935`/`A005935`/`KR7005931001`은 다른 증권이다. 미국의 A/B 클래스와 해외 예탁증권도 별개로 유지한다. [삼성전자 공식 상장정보](https://www.samsung.com/global/ir/stock-information/stock-listing/).
- Unicode NFKC, 대소문자, 연속 공백을 정규화한다. 선행 0과 주식 클래스의 구두점은 보존한다. `5930`을 `005930`으로 추측하거나, 미국 티커의 마침표를 임의로 하이픈으로 바꾸지 않는다.
- `resolved`는 정확히 한 증권, `ambiguous`는 여러 증권 후보, `unresolved`는 적격 별칭 없음이다. 시장·거래소·별칭 namespace로 범위를 좁힐 수 있다. 후보에는 원별칭·출처·시간 근거를 반환한다.

## 시간과 재현성

각 별칭은 유효 구간 `[valid_from, valid_to)`와 공개/수집/등록 시각을 보존한다. 수정·철회는 기존 행을 덮어쓰지 않고 새 assertion으로 추가한다. 현재 디렉터리에서 얻은 별칭은 관측일 이전의 경제적 유효성을 주장하지 않으며 등록 전에 조회되지 않는다. 공개 시점 재구성과 당시 시스템 관측 모드는 명시적으로 선택한다.

과거에 재사용된 티커와 상장/상폐 이력의 전수 검증은 아직 부족하다. 현재 이름으로 오늘 증권을 찾는 조회와 과거 날짜의 증권을 판별하는 조회를 구분한다. 현 디렉터리 별칭만으로 과거 identity가 검증됐다고 처리하지 않는다. 역사적 입력이 부족하면 `unresolved`/`ambiguous`를 반환한다.

고정 조회는 릴리스의 `identifier_history` membership과 frozen catalog만 읽는다. 새 별칭이나 최신 회사명·발행사 연결은 기존 릴리스에 영향을 주지 않는다. `release_query_v5`는 별칭 정규화 정책도 봉인하며 이전 정책 버전의 릴리스는 임의로 재해석하지 않는다.

정규화는 현재 실행 환경의 Unicode 15.1.0에 고정한다. 다른 Unicode 버전으로 업그레이드하면 기존 별칭을 조용히 재해석하지 않고 거부한다. 시장별 cutoff 날짜 규칙과 assertion 순서, 구현 파일 hash도 릴리스에 고정한다. SEC/DART 회사명 연결이 충돌하거나 근거가 사라지면 기존 별칭을 새 시각으로 철회하고, 근거가 복구되면 새 acceptance를 추가한다.

사용자가 제거한 관리 대상 별칭도 append-only withdrawal로 처리한다. 철회 후의 현재 조회와 새 릴리스에서는 미등록으로 반환하고, 이전 봉인과 당시의 조회 이력은 감사 목적으로 유지한다.

## 사용

```powershell
.venv/Scripts/python.exe -m backtest_data universe sync-aliases --market both
.venv/Scripts/python.exe -m backtest_data resolve A005930 --market kr
.venv/Scripts/python.exe -m backtest_data resolve "Samsung Electronics" --market kr
.venv/Scripts/python.exe -m backtest_data query resolve --identifier "삼성전자" --market kr --release-id <release-id> --as-of <aware-ISO-time> --history-mode observed_system --research-partial
.venv/Scripts/python.exe -m backtest_data query prices --identifier 005930 --market kr --release-id <release-id> --as-of <aware-ISO-time> --history-mode observed_system --research-partial
.venv/Scripts/python.exe -m backtest_data query fundamentals --identifier A005930 --market kr --release-id <release-id> --as-of <aware-ISO-time> --history-mode observed_system --research-partial
```

`--session-date`는 별칭의 경제적 유효일, `--as-of`는 당시 알 수 있었던 정보의 cutoff이다. 생략된 session date는 해당 시장의 cutoff 날짜를 사용한다. unresolved/ambiguous 입력으로 가격·재무 조회가 전체 target 조회로 확대되지 않도록 차단한다. 재무 조회는 고정 증권에서 고정 발행사로 연결하며 발행사 연결이 없으면 차단한다. 회사와 증권 단위 지표가 섞이는 `query metrics`는 명시적인 `--entity-id`를 사용한다.
