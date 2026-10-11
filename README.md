# Backtest infra

미국·한국 전 종목을 대상으로 가격·거래량·공시 수치와 계산 이력을 SQL에 보관하는 개발 버전입니다. PostgreSQL 18, SQLAlchemy, Alembic을 사용합니다. Django는 추후 관리 화면이 필요할 때 같은 DB 위에 붙일 수 있습니다.

**현재는 연구용 부분 데이터셋입니다. 전 종목·전 기간의 clean/PIT 데이터셋 완성을 의미하지 않습니다.** 수집 예산은 실행 시간을 제한하며 대상 종목을 줄이지 않습니다. 현재 종목 마스터는 NASDAQ/기타 미국 거래소와 KOSPI/KOSDAQ/KONEX 전체를 읽습니다. ETF·ETN과 아직 분류하지 못한 상품도 대상에 남깁니다. 과거 상장폐지·티커 재사용 이력은 별도 공급자 검증이 필요합니다.

## 성과와 날짜별 종목 선택 화면

Backtrader 기반의 일봉 연구 화면을 구현했습니다. 실제 SQL 자료를 고정 입력으로 읽고 이동평균·RSI·모멘텀 전략, 자산 가치·낙폭·비용, 날짜별 후보/제외 사유/목표 비중/주문/실제 체결/보유를 구분해서 표시합니다. 결과 ID로 같은 입력을 불러오고 재현할 수 있습니다. 원주가 체결과 지표용 수정주가 선택은 별개입니다.

Python 3.13의 별도 환경에 `pip install '.[research]'`로 연구 의존성을 설치하고, `frontend`에서 `npm ci`, `npm test`, `npm run build`를 실행합니다. Windows에서는 `scripts/start_research.ps1 -PythonPath <연구-환경-python.exe>`로 시작한 뒤 `http://127.0.0.1:8765/`를 엽니다. 기존 수집 환경과 프로세스는 바꾸지 않습니다. [화면 사용법](docs/data/research-ui.md), [엔진 검증과 범위](docs/data/research-engine-qualification.md), [API 계약](docs/data/research-api.md).

기본 재무 일봉 정책은 `financial_daily_metadata_pit_v1/release_query_v8_daily`입니다. 정확한 공시번호와 공식 공개일을 연결하고 다음 시장 세션부터 사용하며, 정정은 자신의 공개일 이후에 적용합니다. 날짜 미상·공시번호 불일치·같은 날 상충 값은 제외합니다. 아래의 엄격 v5-v7 봉인/조회 이력은 별도로 유지됩니다. 화면의 공시 메타데이터 표는 완성된 재무비율 계산을 의미하지 않습니다.

공개 저장소에는 코드만 기본 제공되며 API 키와 DB 인증은 포함하지 않습니다. [GitHub Actions와 Secrets 설정](docs/data/github-actions-setup.md)의 검증 작업과 원격 수집 작업은 구분됩니다. 원격 수집은 기존 로컬 작업과 계정/체크포인트 소유권 인계가 확인될 때까지 비활성입니다.

## 실행

로컬 `.env`와 기존 공유 키 설정을 사용합니다. 실제 키·토큰·DB 비밀번호는 Git에 넣지 않습니다. 이 작업에서 만든 PostgreSQL 개발 DB는 `127.0.0.1:54328`에 있습니다.

```powershell
docker compose up -d postgres
.venv/Scripts/python.exe -m backtest_data init-db
.venv/Scripts/python.exe -m backtest_data status --market both
.venv/Scripts/python.exe -m backtest_data universe sync --market both --start 2016-01-01 --end 2026-10-07
.venv/Scripts/python.exe -m backtest_data universe link-issuers --market both
.venv/Scripts/python.exe -m backtest_data universe sync-kis-us --market us --manifest-id <전체-target-manifest-id>
.venv/Scripts/python.exe -m backtest_data collect --market both --domain prices --budget-seconds 120
.venv/Scripts/python.exe -m backtest_data collect --market kr --domain prices --budget-seconds 45
.venv/Scripts/python.exe -m backtest_data collect --market kr --domain fundamentals --budget-seconds 45
.venv/Scripts/python.exe -m backtest_data collect --market us --domain fundamentals --budget-seconds 45
.venv/Scripts/python.exe -m backtest_data normalize fundamentals
.venv/Scripts/python.exe -m backtest_data derive metrics --as-of <timezone-aware-ISO-time> --history-mode reconstructed_public
```

첫 환경 구성은 Python 3.13에서 `python -m venv --system-site-packages .venv` 후 `.venv/Scripts/python.exe -m pip install --no-build-isolation -e '.[dev]'`를 사용했습니다. 공유 라이브러리를 사용하지 않는 배포 환경은 별도 venv에서 같은 직접 의존성 버전을 설치해야 합니다.

`bootstrap`과 `incremental`은 명시적으로 계획된 작업을 재개합니다. 아직 날짜 증분·회전 정정 스캔을 자동 생성하지 않습니다. `repair`는 해당 시장/영역의 실패·미지원 작업과 불완전 추출을 재검증합니다. 가격 repair는 이전 오류·커서 요약을 보존하고 원래 기간을 새 세대로 다시 확인합니다. 성공/실패는 수집 단계 상태이며 clean coverage 보증이 아닙니다.

작업은 SQL lease/fencing token과 공급자 공통 rate budget을 사용합니다. 동일 요청의 새 종목 마스터는 기존 작업을 재사용합니다. 기존 중복 pending 작업은 원행과 참조를 보존한 `superseded` 상태로 정리합니다. 장시간 실행 로그의 반환 결과는 최근 100건이며 SQL 작업/증거는 보존합니다.

시장·작업 단계별 순서를 SQL에 보존된 시도 횟수로 정하고 실행 중에도 번갈아 처리합니다. 한 공급자의 인증 오류·호출 제한은 다른 공급자의 큐를 멈추지 않습니다. `--budget-seconds`는 계획 확인을 제외한 실행 예산이며 `setup_elapsed_seconds`를 별도로 보고합니다. `repair`는 KIS 날짜 커서와 Alpaca 페이지 토큰/요청 시각을 함께 초기화하고 이전 시도의 요약을 보존합니다.

## 무료 소스 구성

미국 가격의 우선 후보는 **Alpaca Basic의 과거 SIP**입니다. 무료 실시간 IEX와 구분하며 `feed=sip`, `adjustment=raw`, `asof=-`를 명시합니다. 공식 무료 범위는 2016년 이후, 분당 200회이며 과거 SIP 조회 종료가 15분 이상 과거여야 합니다. 현재 구현은 16분 여유를 두고 완전히 끝난 뉴욕 날짜만 요청합니다. 원래 target 종료일은 유지하고 제외된 최근 구간을 gap으로 기록합니다. [공식 범위](https://docs.alpaca.markets/us/docs/about-market-data-api), [조회 조건](https://docs.alpaca.markets/us/docs/market-data-faq).

무료 계정에서 발급한 키를 비공개 로컬 설정의 `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`에 설정합니다. 키는 Git에 넣지 않습니다. Alpaca SIP 원주가와 분할 수정주가 수집을 연결했고 실제 응답·SQL 저장을 확인했습니다. 미국 KIS 원주가는 별도 소스 작업으로 유지합니다. 연결 성공은 전 기간 수집 완료와 다릅니다.

```powershell
.venv/Scripts/python.exe -m backtest_data sources probe --source alpaca
.venv/Scripts/python.exe -m backtest_data collect --market us --domain prices --budget-seconds 120
```

한국 가격은 KIS와 [KRX 공식 일별 서비스](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd)를 연결했습니다. 승인된 10개 KRX 서비스의 실제 HTTP200 및 독립 SQL 관측 저장을 확인했으며 KIS 가격을 덮어쓰지 않습니다. NAV/IV/지수값 저장은 과거 상품·지수 구성 이력이나 정확 VWAP 완성을 의미하지 않습니다. 펀더멘탈은 SEC·OpenDART를 유지합니다.

미국 KIS는 공식 NAS/NYS/AMS 마스터 원응답을 보존하고 전체 미국 대상과 정확히 매칭합니다. 미매핑·충돌은 공급자 심볼을 추측하지 않고 unsupported로 남깁니다. `MODP=0` 원주가, 직접 보고 OHLC·거래량·거래대금을 Decimal로 보관하며 현재 마스터 증거는 과거 티커 정체성 인증이 아닙니다. 한국과 미국은 같은 KIS 인증 클라이언트·호출 예산을 공유합니다. 짧은 페이지도 명시적 시작 경계 또는 빈 응답까지 계속 확인하며 source exhaustion은 전 기간 coverage 보증이 아닙니다.

한국 KIS의 명시적 거래량·거래대금 0, 동일한 양수 OHLC 기록은 `supplier_reported_unverified` 등급으로 보관합니다. 이는 공급자 보고 견적 기록이며 공식 거래정지·무거래 상태나 exact VWAP를 인증하지 않습니다. 빠진 거래량·거래대금, 불일치 OHLC, 비정상 숫자는 여전히 거부합니다. 과거 실패 원응답의 재처리는 원래 수집 시각을 유지하고 검증 결정만 현재 시각에 추가합니다. 기존 관측·검증 실패 이력은 수정하지 않습니다.

무료 소스의 전 종목·상폐·ETN·과거 티커·기업행동·지수 발표 이력 전체 coverage는 아직 인증하지 않았습니다. Alpaca 응답의 `vw`는 공급자 VWAP 참고값으로만 보관합니다. 일반 거래량 `v`와의 곱을 exact 거래대금으로 사용하지 않습니다. 무료 연결 성공과 완성된 clean/PIT 데이터셋은 별개입니다.

가격은 값이 같은 별도 수집 시각도 confirmation event로 보존합니다. 뒤늦게 도착한 중간 정정이 당시 관측 이력을 바꾸지 않도록 하기 위한 선택입니다. 동일 응답/동일 capture의 재처리는 중복 행을 만들지 않으며 원래 capture를 수정하지 않습니다. 향후 전수 회전 재검증에서는 값과 capture ledger를 분리해 저장량을 줄여야 합니다.

## 저장 계약

| 데이터 | 저장 내용 | 현재 제약 |
|---|---|---|
| 가격·거래량 | 원주가 OHLCV, 거래대금, 통화·수량 단위, feed/venue/basis, 원응답 hash·파일, 정정/되돌림 이력, 선택 결정 | KIS·Alpaca·KRX 실제 수집 연결. 공급자 0거래 보고는 별도 등급. 과거 종목 정체성과 전 기간 coverage는 미검증 |
| SEC/OpenDART 수치 | 표준·비표준 계정, 숫자·원표기·nil, 기간·context·차원·단위·precision, 공시/문서 hash·버전·수집/추출/발견 시각 | 지원 XBRL/Inline XML 숫자만 검증. PDF/비구조화 숫자와 최초 실적 발표 범위는 미지원 |
| 표준/파생 재무 | 매핑·계산 버전, 입력 ID, 기간·회계/주식 기준, 값/미산출 이유, 실제 materialization 시각 | 표준 계정의 SQL 매핑과 계산 실행 연결. 연결/별도·회계기간·주식 기준·상품별 적용성 미검증은 그대로 표시하며 실제 재무비율은 미산출 |
| 수정주가·VWAP | 공급자 선언 수정 기준, 기업행동 보조 근거, 직접 짝지어진 거래대금·수량 또는 적격 체결 집계 | Alpaca split 자료와 체결 집계의 실제 SQL 저장 확인. 역사적 신원·취소 스트림·기업행동 완전성·같은 정의의 독립 2소스 합의는 별도 공백 |
| 릴리스 | 고정 전체 대상·catalog snapshot, 모든 원본/정정 SQL 행과 결정, 입력 closure, 공유 segment, 규칙 버전·행 hash | 부분 릴리스만 허용. 정상 활성화와 전수 품질 profile은 미완료 |

SEC/OpenDART 전체 문서는 메모리에서 파싱하고 영구 파일로 보관하지 않습니다. SQL의 원표기·numeric/context/unit evidence와 hash가 복구 입력입니다. 가격/종목 마스터 원응답은 immutable gzip 파일로 보관합니다. 다른 CIK의 수치가 발견되면 숫자/증거를 보존하되 해당 추출을 격리합니다. 다른 정밀도로 보고된 동일 context 수치는 모두 보존하고, 보수적 공통 구간 검증으로 일치 여부를 판정합니다.

OpenDART JSON v3는 동일 코드·금액의 서로 다른 응답 행도 별개 node로 저장합니다. SQL gate는 전체 원본 node 수와 context를 대조하고 실패 시 savepoint를 되돌립니다. 과거 v2의 누락을 복구하는 `maintenance repair-dart-inventory`는 원래 bytes hash·기업·접수번호·보고서가 모두 맞는 자료만 재조회하고 누락 node를 새 시각으로 추가합니다. 봉인된 document/fact는 복구 대상에서 제외하며 기존 행과 시각을 수정하지 않습니다. 금융 원문 파일은 만들지 않습니다.

JSON 계정 작업은 이미 발견한 공시의 정확한 발행사·접수번호·보고서 제목으로도 계획합니다. XBRL 저장 완료를 기다리지 않으며, 최신 API 응답이 다른 정정 접수번호를 반환하면 과거 공시로 채택하지 않습니다. XBRL 경로의 점검 오류 800은 해당 단계만 보류하고 다른 OpenDART JSON 및 SEC 수집은 계속합니다. 인증·전체 호출 제한과 다른 API의 일시 오류는 공급자 차단으로 분리합니다.

지원 계산 목록에는 분기·TTM, ROE/ROA, 매출총/영업/순이익 마진, CFO−현금 PPE·무형자산 투자 FCF, EBITDA, PER/PBR/EV/EBITDA가 있습니다. 매출·기본/희석 EPS·영업이익의 QoQ/YoY/TTM YoY 성장률 12개와 직접 연간값 계열을 분리합니다. EPS TTM은 호환되는 직접 보고 4개 분기 EPS만 사용합니다. 분할·배당·기간/Anchored VWAP은 필요한 coverage가 없으면 값을 만들지 않습니다. 상세 정의는 [metric catalog](docs/data/financial-metric-catalog.md)에 있습니다.

## 검증과 고정 조회

```powershell
.venv/Scripts/python.exe -m backtest_data validate --scope all
.venv/Scripts/python.exe -m backtest_data release build --manifest-id <전체-target-manifest-id>
.venv/Scripts/python.exe -m backtest_data query smoke --release-id <release-id> --as-of <timezone-aware-ISO-time> --history-mode observed_system --research-partial
.venv/Scripts/python.exe -m backtest_data query source-accounts --release-id <release-id> --as-of <timezone-aware-ISO-time> --history-mode observed_system --entity-id <issuer-uuid> --research-partial
.venv/Scripts/python.exe -m backtest_data query fundamentals --release-id <release-id> --as-of <timezone-aware-ISO-time> --history-mode observed_system --metric-id revenue --research-partial
.venv/Scripts/python.exe -m backtest_data query prices --release-id <release-id> --as-of <timezone-aware-ISO-time> --history-mode reconstructed_public --entity-id <security-uuid> --research-partial
.venv/Scripts/python.exe -m backtest_data query metrics --release-id <release-id> --as-of <timezone-aware-ISO-time> --history-mode reconstructed_public --entity-id <issuer-uuid> --metric-id roe_parent_ttm --research-partial
```

공개 시각이 검증되지 않은 실제 수집값은 **capture-only**입니다. 따라서 현재 수집한 값을 2016년 당시 알고 있었던 값으로 소급하지 않습니다. `observed_system`은 수집·추출·최초 발견·선택/계산 시각도 cutoff에 포함합니다. 연구용 부분 조회도 PIT/무결성/미지원 규칙 오류를 우회하지 않습니다. 반환하는 source accounts는 구조화 원계정 감사 데이터이며 표준 재무 지표의 채택과 구분합니다.

표준 계정 조회는 매핑 버전·원계정·공시와 qualification gap을 반환합니다. 매핑과 계산 적격성을 구분하고, 미지원 custom/차원/nil 계정도 원계정 SQL에 남깁니다. `fundamental_mapping_v1` 해시는 보존하며 현재 `v2`에서 HTTPS IFRS namespace를 추가합니다. [매핑·계산 계약](docs/data/canonical-mapping.md).

종목 코드·이름도 전수 SQL 별칭으로 연결합니다. `universe sync-aliases`로 등록하고 `resolve A005930 --market kr`로 기존 증권 ID를 찾습니다. `query prices`와 `query fundamentals`는 `--identifier "삼성전자" --market kr` 입력을 지원하며, 모호하거나 미등록인 입력은 차단합니다. 보통주·우선주·클래스·예탁증권은 별개입니다. 고정 조회는 `release_query_v5`의 봉인된 별칭만 사용합니다. [별칭·시점 계약](docs/data/security-aliases.md).

## 백업·테스트

```powershell
.venv/Scripts/python.exe -m backtest_data maintenance backup
.venv/Scripts/python.exe -m backtest_data maintenance verify-backup --backup-manifest <manifest.json>
.venv/Scripts/python.exe -m backtest_data maintenance restore-drill --backup-manifest <manifest.json>
.venv/Scripts/python.exe -m backtest_data maintenance replay-kis-prices --audit-path artifacts/kr-price-validation-audit.json --progress-path artifacts/kr-price-recovery-progress.json --budget-seconds 60 --requeue-recovered
.venv/Scripts/python.exe -m pytest -q --require-postgres -p no:cacheprovider --basetemp artifacts/pytest-root
```

백업은 PostgreSQL exported snapshot의 `pg_dump`와 그 snapshot이 참조한 모든 가격/마스터 파일을 결합합니다. 복원 검증은 새 무작위 테스트 DB에만 수행하고 원래 DB를 변경하지 않습니다. 현재 `data/backups` 사본은 같은 장치의 로컬 검증 사본입니다. 다른 failure domain 복제, WAL/PITR, 전수 규모 RPO/RTO는 아직 검증하지 않았습니다. 테스트는 별도의 `backtest_test` PostgreSQL DB를 요구합니다.

현재 빌더는 최대 100만 evidence 행을 한 번에 처리하며 초과 시 실패합니다. 전수 장기 이력의 파티션별 스트리밍 빌드와 선택 조회, capture ledger 압축은 후속 작업입니다. 실제 9만여 재무 행을 포함한 연구용 릴리스의 전수 smoke 조회는 약 24초였습니다. 이는 전체 원행 hash를 다시 확인하는 개발 경로의 단일 실측이며 p95나 전수 완료 상태의 성능 보증이 아닙니다.

전체 설계·단계별 계획은 [implementation plan](docs/superpowers/plans/2026-10-07-clean-dataset-implementation-plan.md), 실제 완료/미완료 경계는 [implementation status](docs/data/implementation-status.md)에 있습니다.
