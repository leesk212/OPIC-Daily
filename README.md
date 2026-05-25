# 매일 영어 — Django 로컬 버전

영어 일기 + OPIc 학습용 로컬 풀스택 앱. SQLite로 영구 저장하고, 로컬에 설치된 Claude Code CLI를 AI 백엔드로 사용합니다.

## 준비물

1. **Python 3.10+**
2. **Claude Code CLI** — 설치 후 `claude login` 완료 상태
   - 설치 가이드: <https://docs.claude.com/en/docs/claude-code/quickstart>
3. (선택) macOS / Linux 셸

## 빠른 시작

```bash
cd opic-daily
./run.sh
```

자동으로:
1. `claude` 명령어 존재 확인
2. `.venv/` 생성 + Django 설치
3. SQLite 마이그레이션
4. http://localhost:8000 에 서버 시작 + 브라우저 자동 오픈

종료:
```bash
./stop.sh
# 또는 Ctrl+C
```

## 🐳 Docker로 실행 (한 줄 시작)

호스트에 Python·Node·`claude` CLI 설치 없이도 컨테이너 하나로 동일 환경에서 실행됩니다. Claude Code CLI는 이미지 안에 포함돼 있고, 인증 세션과 SQLite DB는 볼륨으로 보존됩니다.

### 1) docker compose로 (가장 추천)

```bash
# 이미지 받아서 백그라운드 실행
docker compose up -d

# 최초 1회: Claude Code CLI 로그인 (브라우저 URL 발급됨)
docker exec -it opic-daily claude login

# 접속
open http://localhost:8000
```

- 로그인 정보는 `claude-auth` named volume에 저장되므로 컨테이너 재시작/업데이트해도 유지됩니다.
- SQLite DB와 로그는 호스트의 `./data/`에 영구 저장됩니다.

종료:
```bash
docker compose down            # 컨테이너만 정지 (data + 인증 유지)
docker compose down -v         # 볼륨까지 삭제 (인증 초기화)
```

### 2) 순수 `docker run`만으로

```bash
docker run -d --name opic-daily \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v opic-daily-auth:/root/.claude \
  -it ghcr.io/leesk212/opic-daily:latest

docker exec -it opic-daily claude login
```

### 3) API key를 환경변수로 (로그인 생략하고 싶을 때)

```bash
docker run -d --name opic-daily \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/leesk212/opic-daily:latest
```

### 로컬 빌드 (이미지 안 받고 직접 빌드)

```bash
docker compose build              # 또는 docker build -t opic-daily .
docker compose up -d
```

> 이미지는 main 브랜치에 push가 발생할 때마다 GitHub Actions가 자동으로 `ghcr.io/leesk212/opic-daily:latest` (linux/amd64 + linux/arm64)로 빌드·게시합니다.

## 폴더 구조

```
opic-daily/
├── manage.py
├── opic_daily/             # Django 프로젝트 설정
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── api/                    # API 앱
│   ├── models.py           # Entry, Preference
│   ├── views.py            # /api/entries, /api/feedback, /api/import
│   ├── urls.py
│   ├── ai_client.py        # `claude --print` subprocess wrapper
│   └── prompts.py          # diary / opic 프롬프트 빌더
├── frontend/
│   └── templates/
│       └── index.html      # 단일 페이지 앱 (아티팩트 변형)
├── data/
│   └── db.sqlite3          # SQLite DB (gitignored)
├── requirements.txt
├── run.sh / stop.sh
└── README.md
```

## API

| Method | Path | 동작 |
|--------|------|------|
| GET    | `/` | 메인 페이지 |
| GET    | `/api/entries/` | 모든 entry 조회 |
| POST   | `/api/entries/` | 새 entry 저장 |
| DELETE | `/api/entries/<id>/` | entry 삭제 |
| POST   | `/api/feedback/` | AI 첨삭 요청 (`{ mode, text, opicQuestion?, model }`) |
| POST   | `/api/import/` | 아티팩트에서 export한 JSON 일괄 import |
| GET    | `/api/health/` | 헬스체크 |

## AI 백엔드 — Claude Code 연동

`api/ai_client.py`가 `subprocess`로 `claude --print --model <model> < prompt`를 호출합니다.

- 인증: 이미 `claude login` 되어 있으면 그 세션 사용
- 모델 alias: `haiku` / `sonnet` / `opus` (Claude Code CLI가 처리)
- 첫 호출 시 1-2초 cold start
- 타임아웃 120초

## 아티팩트에서 데이터 이전

1. Cowork 아티팩트 → 통계 모달 → **📦 데이터 내보내기** → JSON 다운로드
2. 로컬 서버 실행 후 통계 모달 → **📥 데이터 가져오기** → 받은 JSON 선택
3. 모든 entry, streak, 잔디가 그대로 복원됨

## 🌐 외부 접속 (Cloudflare Tunnel — **완전 무료**)

휴대폰이나 외부에서 접속하려면. **신용카드/계정 가입 불필요**, 그냥 CLI 한 줄.

```bash
# 1. cloudflared 설치 (한 번만)
brew install cloudflared    # macOS

# 2. 서버 띄운 상태에서 다른 터미널:
./tunnel.sh
```

→ 자동으로 랜덤 `*.trycloudflare.com` URL 발급 (Quick Tunnel, 무료).

그 URL을 ⚙️ **설정 모달**의 "사이트 URL"에 넣으면 알림 이메일 링크에도 반영됩니다.

> 영구 도메인을 원하면 Cloudflare 계정 만들어서 named tunnel 설정 (이것도 무료): https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/

## 🔔 매일 23시 KST 이메일 알림

### 1. WebUI에서 설정 (가장 쉬움)

서버 띄운 뒤 우측 상단 **⚙️** 버튼 클릭 → 설정 모달에서 입력:

- **수신자 이메일**: `leesk212@gmail.com` (기본값)
- **발신자 이메일**: 본인 Gmail 주소
- **SMTP host**: `smtp.gmail.com`
- **Port**: `587`
- **SMTP 비밀번호**: Gmail **앱 비밀번호** (https://myaccount.google.com/apppasswords)
- **사이트 URL**: localhost 또는 Cloudflare Tunnel URL

저장 후 **📧 테스트 이메일** 버튼으로 확인.

### 2. 매일 23:00 KST 자동 전송 — crontab

```bash
crontab -e
```

다음 한 줄 추가 (Mac 시스템 시간이 KST면 그대로 23시):

```cron
0 23 * * * /Users/danny/Desktop/mini-proj/opic-daily/run-notify.sh
```

저장하고 종료. 확인:
```bash
crontab -l
```

**시스템 시간대 확인:**
```bash
date +%Z       # KST 나오면 OK
```

> macOS 잠자기 중엔 cron이 안 돕니다. 잠 안 자게 하거나 `caffeinate` 또는 launchd의 `WakeFromSleep` 옵션 사용.

### 3. launchd 방식 (macOS, sleep 깨우기 포함)

`~/Library/LaunchAgents/com.danny.opic-daily-notify.plist` 생성:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.danny.opic-daily-notify</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/danny/Desktop/mini-proj/opic-daily/run-notify.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/danny/Desktop/mini-proj/opic-daily/data/launchd-out.log</string>
  <key>StandardErrorPath</key><string>/Users/danny/Desktop/mini-proj/opic-daily/data/launchd-err.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.danny.opic-daily-notify.plist
launchctl start com.danny.opic-daily-notify     # 즉시 한 번 테스트
```

### 알림 로그

전송 결과는 `data/notify.log`에 누적됩니다.

```bash
tail -f data/notify.log
```

## 트러블슈팅

**"`claude` 명령어를 찾을 수 없어요"**
→ Claude Code 미설치 또는 PATH 누락. `which claude`로 확인.

**Opic 마이크 작동 안 함**
→ `http://localhost:8000` (HTTPS 아니어도 localhost는 OK)으로 접속했는지 확인. `file://` 직접 열면 차단됨.

**첨삭이 빈 응답으로 옴**
→ 디버그 패널에서 raw response 확인. Claude Code 인증 만료일 수도. `claude login` 다시.

**포트 8000 충돌**
```bash
PORT=9000 ./run.sh
```

## 라이선스

개인 학습용. 자유롭게 수정.
