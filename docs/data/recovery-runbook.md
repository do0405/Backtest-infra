# 로컬 복구 검증

실행 대상은 workspace Compose PostgreSQL 18이다. 현재 기능은 일관 snapshot 백업·검증·테스트 복원이며, 다른 장치 손실이나 WAL 시점 복원을 인증하지 않는다.

1. `python -m backtest_data maintenance backup`를 실행한다. exported snapshot의 `pg_dump`와 그 snapshot의 `raw_object` 참조 파일들을 함께 저장한다. 백업 manifest가 생성되지 않은 디렉터리는 완료된 백업으로 사용하지 않는다.
2. 출력한 `backup_manifest` 경로로 `maintenance verify-backup --backup-manifest <path>`를 실행한다. dump/파일 hash와 byte count가 일치해야 한다.
3. `maintenance restore-drill --backup-manifest <path>`를 실행한다. 새 무작위 `backtest_restore_*_test` DB만 생성하고 모든 모델 테이블의 행 수와 raw hash 목록을 snapshot과 비교한 뒤 그 테스트 DB를 제거한다. 기존 개발/운영 DB는 복원 대상이 아니다.
4. 목표 DB의 실제 장애 복구는 아직 별도 승인·운영 절차가 필요하다. 이 drill을 운영 DB 덮어쓰기 도구로 사용하지 않는다.

2026-10-08 KST 실측: 28개 모델 테이블, source financial fact 94,459행, price observation 8,587행, raw object 97개. SQL dump 약 71 MB, 백업·검증 약 6.8초, 테스트 복원·행 수 확인 약 11.8초였다. 이 백업은 첫 릴리스 봉인 전 snapshot이며, 최종 릴리스를 포함한 후속 백업도 별도로 검증한다. 전수 장기 이력 완료 상태의 RPO/RTO 측정은 아니다.

SEC/OpenDART 문서 파일을 복구 입력으로 요구하지 않는다. 수치·원표기·context·단위·precision·공시/문서 hash·버전은 SQL에 있다. 가격/종목 마스터 파일은 backup의 `raw` 디렉터리에서 복구한다. `global_roles_included=false`이므로 운영 권한을 별도 구성해야 한다.

현재 사본은 workspace의 `data/backups`에 있어 독립 재해 사본이 아니다. 외부 failure domain으로 복제하고 그 사본에서 다시 drill해야 한다. WAL/PITR는 미구현이며, DB 이후 시각과 아직 복제되지 않은 raw 파일의 recovery horizon을 일치시키는 기능도 남아 있다. 같은 장치의 폴더 복사나 PostgreSQL crash recovery를 전체 데이터셋 재해 복구로 표시하지 않는다.
