# 배포 기록

- 공개 주소: https://noodlesonly.vercel.app
- Vercel 프로젝트: noodlesonly
- 상태: Vercel 빌드 READY, 프로덕션 도메인 연결 및 예약 정리 설정 확인. 공개 화면 기능 검증은 작업 환경의 새 도메인 접근 허용 적용 후 진행합니다.
- 배포 코드: GitHub `codex/study-app` 브랜치
- 실제 Supabase: 기존 프로젝트에 `study_app_login` 로그인 역할과 비공개 `study_app` 스키마, 비공개 `proofs` 사진 버킷 추가. 기존 DB 비밀번호는 변경하지 않았습니다.

## 승인받아 사용한 권한

1. Vercel 프로젝트 생성, 프로덕션 배포, 공개 도메인 연결, 로그인 없는 열람을 위한 Vercel 인증 보호 해제.
2. Supabase 앱 전용 DB 역할·스키마·테이블과 비공개 사진 버킷 생성.
3. Supabase 서버 키와 앱 전용 DB 비밀번호를 Vercel의 암호화된 프로덕션 환경변수에 저장. 세션·예약 작업 비밀키와 운영자 비밀번호도 생성하여 같은 방식으로 저장.
4. GitHub의 앱 전용 브랜치에 코드 게시. 기존 master 브랜치는 변경하지 않았습니다.

관리 토큰인 VERCEL_TOKEN과 SUPABASE_ACCESS_TOKEN은 앱의 실행 환경으로 복사하지 않았습니다. 유료 플랜 전환이나 결제 설정 변경은 수행하지 않았습니다.

## 운영자 입장

Vercel 프로젝트 Settings → Environment Variables에서 `ADMIN_PASSWORD` 값을 확인합니다. 값은 Git이나 채팅에 보관하지 않습니다. 앱에서 개인 계정으로 가입하고 하단의 운영자 입장에 이 값을 입력합니다. 별명은 앱에서 바꿀 수 있습니다.

사진 자동 판독 API는 연결하지 않았으므로 업로드는 운영자 확인 대기로 접수됩니다. 사진은 서버에서 보관 기한을 검사하여 공개하고, 물리 파일 정리는 매일 한국 시간03시 예약 작업이 수행합니다.
