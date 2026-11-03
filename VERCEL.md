# Vercel + Supabase 배포

화면과 API는 Vercel Python Function, 계정·인증·정산은 Supabase PostgreSQL, 사진은 Supabase의 비공개 Storage 버킷에 저장합니다. Vercel 임시 파일시스템에는 운영 데이터를 저장하지 않습니다.

## 계정 연결

GitHub로 Vercel/Supabase에 가입한 것과 이 작업 환경에 배포 권한을 전달하는 것은 별개입니다. 에이전트가 대신 배포하려면 환경 설정에 다음 관리 토큰을 안전하게 입력합니다. 토큰 값을 채팅이나 Git에 넣지 마세요.

- `VERCEL_TOKEN`: https://vercel.com/account/tokens
- `SUPABASE_ACCESS_TOKEN`: https://supabase.com/dashboard/account/tokens

이 두 관리 토큰은 배포 작업용이며 웹앱의 실행 환경에는 넣지 않습니다. 이전 PythonAnywhere의 `PA_USERNAME`, `PA_API_TOKEN`은 사용하지 않습니다.

## Supabase

1. 무료 플랜의 프로젝트를 선택하거나 생성합니다. 스터디와 가까운 리전을 사용합니다.
2. Storage에 **비공개** `proofs` 버킷을 만듭니다. 허용 MIME은 `image/jpeg`, 파일 제한은 4MB 이상입니다. 공개 버킷이나 익명 접근 정책은 만들지 않습니다. 사진 공개 열람은 앱 API가 보관 기한을 확인한 뒤 제공합니다.
3. Connect에서 transaction pooler 연결 문자열(포트6543)을 준비합니다. 데이터베이스 비밀번호의 특수문자는 URL 인코딩합니다. 앱은 TLS 인증서와 호스트 이름을 검증합니다. Supabase 호스트에는 번들에 포함된 공식 Supabase Root 2021 CA를 자동 사용합니다. 별도 CA가 필요한 경우 DSN의 `sslrootcert`로 지정할 수 있습니다. 인증서 검증은 유지합니다.
4. 프로젝트 URL과 서버 전용 `service_role` 키를 준비합니다. 이 키는 클라이언트에 노출하지 않습니다.

앱이 첫 연결 때 `study_app` 전용 스키마와 필요한 테이블을 생성합니다. 개인 코드가 있는 테이블은 공개 REST API용 `public` 스키마에 만들지 않습니다. 기본 Supabase 데이터베이스 사용자처럼 스키마 생성 권한이 있는 연결을 사용합니다.

## Vercel

저장소를 프로젝트로 연결하고 루트 디렉터리를 저장소 루트로 설정합니다. `vercel.json`과 `api/index.py`가 라우팅·런타임을 정의합니다. 별도 프론트엔드 빌드 명령은 필요 없습니다.

다음 값을 Vercel 프로젝트의 **Production 비밀 환경변수**에 설정합니다.

| 변수 | 값 |
| --- | --- |
| `DATABASE_URL` | Supabase transaction pooler 연결 문자열 |
| `SUPABASE_URL` | `https://프로젝트참조.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | 서버 전용 service_role 키 |
| `APP_SESSION_SECRET` | 32자 이상의 임의 문자열. 재배포마다 바꾸지 않습니다. |
| `ADMIN_PASSWORD` | 운영자 비밀번호. 10자 이상 권장 |
| `CRON_SECRET` | 32자 이상의 별도 임의 문자열 |

프리뷰 배포는 운영 데이터와 다른 Supabase 프로젝트/자격증명을 사용합니다. 필요한 설정이 없으면 앱은 오류로 시작을 중단하며 임시 SQLite로 대체하지 않습니다. 프로젝트 관리 토큰을 위 앱 변수 대신 사용하지 마세요.

배포 후 처음 별명으로 가입하고, `운영자 입장`에서 설정한 비밀번호로 인증합니다. 운영자도 자신의 회원 계정으로 사진을 올릴 수 있습니다.

## 자동 정리와 사진

- 브라우저는 10MB 이하 원본을 최대1800px JPEG, 2MB 이하(최대 2,000,000바이트)로 압축해 업로드합니다. 서버 요청은 4MB로 제한합니다.
- Vercel Cron이 매일 한국 시간03시(UTC18시)에 `/api/cron/cleanup`을 호출합니다. 해당 경로는 `CRON_SECRET` Bearer 인증이 필요합니다.
- 사진 열람은 업로드 시각부터 21일 보관 기한 즉시 차단됩니다. 물리 파일은 다음 정리 실행 때 제거됩니다. 삭제 오류와 업로드 중단으로 남은 파일도 만료 정보를 보관하여 재시도합니다.
- 자동 사진 판독은 선택 사항입니다. `STUDY_VISION_API_KEY`가 없으면 운영자 검토로 처리합니다.

무료 플랜의 저장량·트래픽·휴면 정책과 Cron 제공 조건은 각 서비스의 현재 조건을 확인합니다. 유료 플랜이나 자동 결제를 임의로 활성화하지 않습니다.

## 배포 후 확인

실제 공개 주소에서 `/api/session`, `/api/state`, 홈 화면·정적 파일을 확인합니다. 개인 코드 가입/재입장, 사진 등록과 비로그인 열람, 운영자 승인·휴식일 수정·납부 체크를 검증합니다. 재배포 후 동일 계정·사진·기록이 남는지, 운영자 코드가 공개 응답에 포함되지 않는지, Cron 인증 및 삭제가 동작하는지 확인합니다.

현재 로컬 검증은 실제 PostgreSQL과 모의 Storage를 사용합니다. 실제 Supabase/Vercel 연결 및 공개 주소는 계정 연결 이후에 검증합니다.
