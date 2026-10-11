# 공급자 연결 실증 — 2026-10-10

로컬 연구 플랫폼 계획 Task 02의 초기 연결 결과다. 전체 역사·상품·기업행동·라이선스 또는 clean coverage 완료를 뜻하지 않는다. 기존 한국 4,507개·미국 13,251개, 합계 17,758개 목표와 2016-01-01~2026-10-07 범위를 유지한다.

| 공급자 | 실제 확인 | 아직 확인하지 못한 범위 |
|---|---|---|
| Alpaca | 무료 과거 SIP 접근 성공. 미국 13,251종목의 전체 기존 가격 큐를 명시적 `--source alpaca`로 실행. 100행 페이지의 첫 SQL commit과 다음 페이지 재개 확인 | 종목별 전 기간 coverage, 과거 상장폐지·티커 재사용, 기업행동 및 독립 공급자 대조, 정확 기간/Anchored VWAP |
| KRX | 승인 후 production 10개 서비스 모두 HTTP 200. 2026-10-07 자료 7,310행을 `krx_quote_observation`에 실제 저장. 접근 성공의 새 `source_qualification` 추가, 이전 401 이력 보존 | 전 기간 적재, 단위·거래 범위·수정 여부·과거 기본정보 기준 검증, canonical 가격과 정확 VWAP 적격성, 지수 과거 구성·비중 |
| KIS | 기존 한국·미국 수집 프로세스 유지. Alpaca 명시 수집은 KIS 큐를 claim·repair·consolidate하지 않음 | 수정주가 정의·기업행동·공식 과거 universe 및 공급자 독립성 |
| SEC/OpenDART | 기존 재무 수집·공개 증거 검증 유지. 관련 없는 KRX/Alpaca YAML 실패가 이 경로를 막지 않도록 설정 로더 분리 | 원본/정정 숫자 버전과 공개 근거의 전수 인증, fiscal/share lineage, 실제 적격 지표 coverage |

## 로컬 설정

- KIS: `C:\Users\pc01\KIS\config\kis_devlp.yaml` — 기존 파일 보존.
- OpenDART: `C:\Users\pc01\OpenDART\config\opendart.yaml` — 기존 파일 보존.
- KRX: `C:\Users\pc01\KRX\config\krx.yaml`.
- Alpaca: `C:\Users\pc01\Alpaca\config\alpaca.yaml`.

새 두 파일은 사용자·SYSTEM·Administrators에만 파일 접근 권한을 부여했다. 키는 프로젝트 코드·Git·검증 결과에 넣지 않았다. 환경변수 우선순위는 유지하며 공급자별 `sources`를 지정해 필요한 파일만 읽는다. 설정 오류는 내용 없이 반환한다.

## 공식 제공 범위와 접근 절차

[Alpaca Basic](https://docs.alpaca.markets/us/docs/about-market-data-api)은 주식/ETF 과거 데이터를 2016년부터 제공하며 최신 15분과 분당 200회 제약을 명시한다. 실제 SIP 접근 성공과 종목별 모든 날짜 확보는 다르다. 현재 수집은 명시적인 SIP/raw/USD와 `asof=-`를 사용한다. [자동 과거 이름 매핑과 수정 옵션](https://docs.alpaca.markets/us/reference/stockbars)은 별도 요청 의미다.

[KRX 공식 이용 절차](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO003.jsp)는 인증키 승인과 원하는 API의 활용신청·승인을 각각 요구한다. 승인 후 production 조회는 성공했다. 공식 sample의 401은 production 승인 실패로 혼용하지 않는다. [서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd)에 주식·ETF는 2010년, ETN은 2014년 이후 제공 범위가 안내되어 있다. [이용약관](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO005.jsp)의 키당 하루 10,000회 한도에 맞춰 이 수집기는 SQL 공유 호출 간격을 최소 10초로 제한한다. 같은 키를 쓰는 다른 프로젝트의 호출까지 통제한 것은 아니다.

공식 stock 문서에서 확인한 헤더는 `AUTH_KEY`, 입력은 `basDd`다. [문서](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=JvJFzlAENzZlPBDNGAWC)의 출력 필드는 `BAS_DD`, `ISU_CD`, `ISU_NM`, `MKT_NM`, `SECT_TP_NM`, OHLC, 전일 대비, `ACC_TRDVOL`, `ACC_TRDVAL`, `MKTCAP`, `LIST_SHRS`이며 string으로 선언되어 있다. 실제 일별 시세의 `ISU_CD`는 단축코드이고 기본정보의 `ISU_CD`는 ISIN, `ISU_SRT_CD`는 단축코드다. 요청 날짜만으로 기본정보의 과거 적격성을 확정하거나 현재 종목 ID에 자동 결합하지 않는다. 가격·금액·수량 단위와 거래 범위 확인 전에는 적격 가격/VWAP으로 승격하지 않는다.

실제 첫 날짜 SQL은 시세 4,411행(유가증권 942, 코스닥 1,823, 코넥스 107, ETF 1,172, ETN 367), 기본정보 2,765행, KRX·KOSPI·KOSDAQ 지수 시세 134행이다. ETF NAV 1,172개와 ETN 지표가치 367개가 숫자 컬럼에 들어간 것도 확인했다. 빈 지수값·잘못된 필드·원문 값은 누락/검증 이슈와 함께 보존하며 0으로 대체하지 않는다. 지수 시세는 구성종목·비중 이력이 아니다.

전체 3,933개 달력 날짜 × 10개 서비스 = 39,330개 요청을 별도 `market_quotes` 큐에 계획한다. 공식 거래일 달력이 아직 검증되지 않아 휴일도 조회 대상으로 남기며 빈 응답을 전 기간 coverage 완료로 해석하지 않는다. 원천 숫자 SQL과 원응답 해시를 모두 보존하고 기존 KIS 가격을 덮어쓰지 않는다.

## 원주가·수정주가·VWAP 경계

이번 Alpaca 연결은 원주가와 일반 거래량, 공급자가 보고한 VWAP을 보존하는 단계다. `VWAP × 일반 거래량`을 정확한 거래대금으로 저장하지 않으며 정확 기간/Anchored VWAP을 인증하지 않는다.

세 소스의 수정주가 대조는 수정 종류·통화·수정 기준일·거래일·동일 증권을 맞춘 뒤 수행해야 한다. KRX 일별 통계 자체가 세 번째 수정주가 서비스임을 확인한 것은 아니다. KIS와 Alpaca의 미국 데이터가 독립적인 upstream인지도 확인이 필요하다. 합의 값은 원본 값과 선택 근거로 보존하고 평균으로 새 가격을 만들지 않는다. 미래 기업행동이 반영된 현재 기준 수정주가를 과거 입력으로 사용하지 않는다.

## 재무 PIT와 플랫폼 다음 단계

사용자가 전달한 공식 공시일 다음 거래일 연구 정책은 검토 입력이다. 이번 변경은 기존 엄격한 원본·정정 공개 cutoff와 v7 financial query를 완화하지 않는다. 새로운 정책을 적용하려면 정확한 공시 번호/숫자 버전, 공식 날짜 근거, 거래일 달력과 별도 정책 버전을 연결해야 한다. 최초 수집·공개·정정·검증 시각을 덮어쓰지 않는다.

일별 자동 날짜 계획, 원격 bundle/sync, 백테스트·레짐·스크리너·참고 시그널과 UI는 별도 구현 단계다. 기존 `incremental`은 계획된 작업의 재개이며 오늘 날짜까지 자동 확장한 상태가 아니다.
