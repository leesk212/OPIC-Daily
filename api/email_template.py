"""
"어서 와서 학습하세요" 이메일 템플릿 빌더.
HTML + plain text 둘 다 반환.
"""
import random
from datetime import datetime, timezone, timedelta


KST = timezone(timedelta(hours=9))
DOW_KO = ['월', '화', '수', '목', '금', '토', '일']

FLAVORS = [
    "오늘 영어 한 줄, 내일의 나에게 선물 ✨",
    "5문장만 쓰면 오늘도 streak 살아있어요 🔥",
    "1분만 투자하면 끝나요. 지금 가요 🚀",
    "잠들기 전 영어 한 입 🍪",
    "오늘 빠지면 내일 두 배. 지금이 편해요 😉",
    "탁월은 매일 하는 사람의 것 🌟",
    "Done is better than perfect. 일단 가요!",
    "조용한 밤, 차분히 한 줄 ✍️",
    "Future you will thank present you 💌",
]

QUOTES = [
    "Practice makes progress, not perfect.",
    "Don't be afraid to make mistakes.",
    "Small steps every day.",
    "Quality is not an act, it is a habit. — Aristotle",
    "Progress, not perfection.",
    "Show up. That's already 80%.",
    "Done is better than perfect.",
    "It always seems impossible until it's done. — Mandela",
]


def build_email(site_url: str, status: dict) -> tuple[str, str, str]:
    """
    Returns: (subject, text_body, html_body)

    status: { 'has_diary': bool, 'has_opic': bool }
    """
    now = datetime.now(KST)
    time_str = now.strftime('%H:%M')
    date_str = now.strftime('%Y년 %m월 %d일') + f' ({DOW_KO[now.weekday()]})'
    flavor = random.choice(FLAVORS)
    quote = random.choice(QUOTES)

    if status['has_diary'] and status['has_opic']:
        status_line = '✅ 오늘 일기 + Opic 둘 다 완료!'
        cta_text = '🎉 첨삭 다시 보기 →'
    elif status['has_diary']:
        status_line = '📝 일기 ✓ &nbsp;·&nbsp; 🎤 Opic 아직'
        cta_text = '🎤 Opic 도전하러 →'
    elif status['has_opic']:
        status_line = '🎤 Opic ✓ &nbsp;·&nbsp; 📝 일기 아직'
        cta_text = '📝 일기 쓰러 →'
    else:
        status_line = '☐ 일기 &nbsp;·&nbsp; ☐ Opic'
        cta_text = '✨ 지금 시작하기 →'

    subject = f'🌙 {time_str} — 어서 와서 학습하세요'

    text_body = (
        f"🌙 어서 와서 학습하세요\n"
        f"{date_str} · {time_str} KST\n\n"
        f"{flavor}\n\n"
        f"오늘 상태: {status_line.replace('&nbsp;', ' ').replace('<br>', chr(10))}\n\n"
        f"{cta_text}\n"
        f"{site_url}\n\n"
        f"— {quote}\n"
    )

    html_body = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>오늘의 영어 시간</title>
</head>
<body style="margin:0; padding:0; background:#14171a; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif; color:#e6e6e6; -webkit-font-smoothing:antialiased;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#14171a;">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:480px; background:#1c2025; border-radius:14px; border:1px solid #2a2f36;">
          <tr>
            <td style="padding:32px 28px;">

              <!-- Header -->
              <div style="text-align:center; margin-bottom:24px;">
                <div style="font-size:42px; line-height:1; margin-bottom:14px;">🌙</div>
                <h1 style="font-size:22px; font-weight:700; margin:0 0 6px; color:#e6e6e6; letter-spacing:-0.01em;">어서 와서 학습하세요</h1>
                <p style="font-size:12px; color:#8a8f96; margin:0; letter-spacing:0.04em;">{date_str} · <strong style="color:#f59e0b;">{time_str}</strong> KST</p>
              </div>

              <!-- Flavor message -->
              <p style="font-size:15px; line-height:1.7; color:#b8bdc4; text-align:center; margin:0 0 24px;">
                {flavor}
              </p>

              <!-- Status box -->
              <div style="background:#232830; border-radius:10px; padding:14px 16px; margin-bottom:24px; text-align:center; border-left:3px solid #f59e0b;">
                <div style="font-size:11px; color:#8a8f96; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:6px; font-weight:600;">오늘 상태</div>
                <div style="font-size:15px; color:#e6e6e6; font-weight:600;">{status_line}</div>
              </div>

              <!-- CTA Button -->
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td align="center">
                    <a href="{site_url}" style="display:inline-block; background:#f59e0b; color:#0a0a0a; text-decoration:none; font-weight:700; font-size:15px; padding:14px 32px; border-radius:10px; letter-spacing:-0.01em;">
                      {cta_text}
                    </a>
                  </td>
                </tr>
              </table>

              <!-- URL fallback -->
              <p style="font-size:11px; color:#7a818a; text-align:center; margin:14px 0 0; word-break:break-all;">
                <a href="{site_url}" style="color:#7a818a;">{site_url}</a>
              </p>

              <!-- Quote -->
              <div style="border-top:1px solid #2a2f36; margin-top:28px; padding-top:18px; text-align:center;">
                <p style="font-size:13px; color:#8a8f96; font-style:italic; margin:0 0 4px; line-height:1.6;">
                  "{quote}"
                </p>
                <p style="font-size:11px; color:#5a5f66; margin:6px 0 0;">매일 영어 · 일기 + Opic</p>
              </div>

            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    return subject, text_body, html_body
