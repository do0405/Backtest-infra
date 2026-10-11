# 일봉 연구 정책과 실제 보완 작업

2026-10-10 사용자 요청을 반영한 기본 조회 정책은 `release_query_v8_daily`다.
기존 v5–v7 봉인과 엄격한 원본 검증 이력은 그대로 보존한다.

재무 숫자는 SEC accession/filed 또는 DART 반환 접수번호/공개일과 연결한다.
공개일 다음 시장 세션부터 사용할 수 있으며 정정값도 자기 공개일 이후에만 선택한다.
공시 번호가 없거나 날짜가 불명확한 값을 회계기간 종료일로 채우지 않는다.
공식 달력이 없을 때 다음 평일을 사용했다는 근거를 표시한다.
원본 아카이브 전체 숫자 비교는 이 일봉 정책의 필수 조건에서 제외했다.
원래 `available_at`, `captured_at`, `first_seen_at`, `extracted_at`은 바꾸지 않는다.

가격은 현지에서 완료된 거래일까지만 조회한다. API 요청과 계약에서 원주가와
수정주가를 구분하며 분할/배당/전체 수정, 거래량 기준을 별도로 보존한다.
기업행동 이력은 보조 자료이며 수집 완전성을 수정주가 채택의 필수 조건으로 삼지 않는다.
현재 공급자 기준 수정값은 과거 기업행동 시점까지 검증된 가격이라는 뜻이 아니다.
기본 가격 조회는 원주가이고 수정주가는 `--basis provider_adjusted`로 명시한다.
수정 거래량과 원거래대금을 섞어 정확 VWAP를 만들지 않는다.

```powershell
python -m backtest_data publication-daily --manifest-id ORIGINAL_ID --progress-path artifacts/daily-publication/progress.json --budget-seconds 300
python -m backtest_data supplier-prices --manifest-id ORIGINAL_ID --adjustment split --progress-path artifacts/supplier-prices/progress.json --budget-seconds 300
python scripts/local_sync.py --private-store PRIVATE_FOLDER --incoming LOCAL_INCOMING
```

위 수집 명령은 운영자가 중복 부재와 종료를 확인한 뒤 재개할 때의 예다.
현재 별도 WMI 워커가 이미 실행 중이며 원주가·재무·KRX·보조 자료 수집기는 그대로다.
SQL 원자료와 페이지 커서는 같은 트랜잭션으로 커밋하고 별도 잠금으로 중복을 막는다.
시작한 전수 대상은 한국 4,507/미국 13,251, 2016-01-01~2026-10-07이다.
공개일 보완의 고정 watermark 이후 새 공시는 다음 보완 실행에서 처리해야 한다.

실제 확인 기록은 `artifacts/daily-publication/production-verification.json`,
`artifacts/remote-verification/production-verification.json`과 각 진행 파일에 있다.
SQL 재처리 검증과 일부 공개일 연결은 전종목 clean 완료가 아니다.
원격 저장소/프로젝트가 아직 정해지지 않아 실제 원격 실행은 비활성이다.
배포와 소스 소유권 이전 전에도 기존 로컬 수집을 계속 유지한다.
