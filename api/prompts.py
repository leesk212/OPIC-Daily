"""
Prompt builders for diary and Opic feedback.
Produces a prompt that asks Claude for strict JSON output.
"""

DIARY_CONTEXT = """
The user wrote this English diary entry. Help them improve writing structure and expressiveness.

=== 영어 일기 평가 기준 ===
Good diary writing:
- Clear topic focus (one main thing)
- Specific concrete details (times, places, people, things, sensations)
- Emotional expression (felt, was excited, made me think, etc.)
- Time/place anchored
- Tense variety (past for events, present for habits)
- Connectors for flow (because, however, then, after that, so)
- Reflection or insight (not just listing facts)
- Vocabulary variety (avoid repetition)

레벨 (한국어):
- 시작: 기본 표현, 짧고 단순
- 성장: 일부 디테일·감정 시도
- 안정: 구조 있음, 감정 표현, 흐름 OK
- 능숙: 자연스러운 흐름, 다양 어휘/시제, reflection
- 탁월: 모든 기준 충족
"""

DIARY_EXTRA_SCHEMA = """,
  "diary_evaluation": {
    "estimated_level": "string — 시작 | 성장 | 안정 | 능숙 | 탁월 중 하나",
    "level_reason": "string — Korean (반말 OK), 1-2 문장. 왜 그 레벨인지 핵심 이유",
    "criteria": [
      { "name": "주제 명확성",     "passed": true, "comment": "Korean, 1 문장" },
      { "name": "구체적 디테일",    "passed": true, "comment": "..." },
      { "name": "감정·반응 표현",   "passed": true, "comment": "..." },
      { "name": "시간·장소 명시",   "passed": true, "comment": "..." },
      { "name": "시제 다양성",      "passed": true, "comment": "..." },
      { "name": "연결어 사용",      "passed": true, "comment": "..." },
      { "name": "회고·생각",        "passed": true, "comment": "..." },
      { "name": "어휘 다양성",      "passed": true, "comment": "..." }
    ],
    "next_steps": "string — Korean (반말 OK), 2-3 문장. 다음에 신경쓸 한두 가지. 영어 예시 좋음."
  },
  "diary_rewrite": {
    "rewritten_diary": "string — English (원본과 비슷한 분량). 사용자 소재 유지하면서 더 잘 쓴 버전.",
    "key_changes": "string — Korean (반말 OK), 2-3 줄. 원본 대비 핵심 변화. 각 줄 \\\\n으로 구분."
  }"""


OPIC_CONTEXT = """
The user is answering this OPIc-style speaking question:
"{opic_question}"

This is a transcript of their spoken English (so may have run-on sentences or be casual). Focus on (1) natural spoken English improvements AND (2) OPIc AL-level coaching.

=== OPIc AL (Advanced Low) Evaluation Criteria ===
AL answers have these characteristics:
- Direct answer with Main Point first (e.g. "Well, I would say I really like...")
- Opening covers What / Feeling / Why
- Natural fillers used (Well, Honestly, You know, Actually, I would say, Let me think, The thing is)
- Varied tenses used naturally
- Specific personal experience with details (장소/시간/사람/감정)
- Emotional expression (I felt..., It helps me..., I felt much better, etc.)
- Clear wrap-up at end ("So overall...", "So that's why...")
- Expression variety — not repeating same words/phrases
- Structure: Intro → Main Point → Detail → Example → Feeling → Wrap-up
- Length: ~8-15 sentences for general/experience questions

Level reference: IM1 < IM2 < IM3 < IH < AL
"""

OPIC_EXTRA_SCHEMA = """,
  "opic_tips": "string — Korean (반말 OK). 2-4 줄. Opic 답변으로 더 잘 쓰려면 구체적 조언. 각 줄 \\\\n으로 구분.",
  "al_evaluation": {
    "estimated_level": "string — IM1 | IM2 | IM3 | IH | AL 중 하나",
    "level_reason": "string — Korean (반말 OK), 1-2 문장",
    "criteria": [
      { "name": "Main Point 먼저",       "passed": true, "comment": "..." },
      { "name": "What/Feeling/Why 초반", "passed": true, "comment": "..." },
      { "name": "자연스러운 필러",        "passed": true, "comment": "..." },
      { "name": "시제 다양",             "passed": true, "comment": "..." },
      { "name": "구체적 경험·예시",      "passed": true, "comment": "..." },
      { "name": "감정 표현",             "passed": true, "comment": "..." },
      { "name": "마무리 (wrap-up)",      "passed": true, "comment": "..." },
      { "name": "표현 다양성",           "passed": true, "comment": "..." }
    ],
    "next_steps": "string — Korean, 2-3 문장. AL로 가려면 무엇을 신경쓸지."
  },
  "al_rewrite": {
    "rewritten_answer": "string — English (5-15 문장). 사용자 답변을 AL 수준으로 다시 쓴 모범 버전.",
    "key_changes": "string — Korean (반말 OK), 2-3 줄. 원본 대비 핵심 변화."
  }"""


COMMON_SCHEMA_HEADER = """
You are a friendly bilingual Korean–English friend helping a Korean learner. Tone: warm and encouraging like a close friend chatting (NOT a teacher giving a grammar lecture). Keep Korean comments short and friendly (반말 OK).
{context}
CRITICAL: Your entire response must be ONLY a single JSON object. No prose before or after. No markdown code fences.

Important rules for `expressions`:
- Output 3~5 items, focused on phrases the user would benefit from learning/repeating later.
- Pull them from the user's own text where they appear (or could naturally appear) — not random vocab.
- DO NOT duplicate `highlight.phrase`. These should be additional, complementary expressions.
- Keep `en` self-contained (a usable chunk, not a whole sentence). Avoid single common words.

Schema:
{{
  "overall": "string — 1-2 short Korean sentences of warm encouragement",
  "highlight": {{
    "phrase": "string — one English phrase/expression worth remembering",
    "note": "string — brief Korean note about it"
  }},
  "expressions": [
    {{
      "en": "string — natural English expression / collocation worth practicing (3~8 words). DON'T overlap with `highlight`.",
      "ko": "string — Korean meaning (under 40 chars)",
      "example": "string — one natural English example sentence using this expression",
      "tip": "string — short Korean usage note (when/how to use it, under 60 chars)",
      "category": "string — one short Korean tag (e.g. '연결어', '감정 표현', '의견 강조', '경험 묘사')"
    }}
  ],
  "sentences": [
    {{
      "original": "string — the original sentence",
      "corrected": "string — natural English version, or same as original if good",
      "comment": "string — 1-2 short Korean sentences, friend tone",
      "is_good": true
    }}
  ]{extra_schema}
}}

User's {entry_type}:
\"\"\"
{text}
\"\"\"

Now output ONLY the JSON object:"""


def build_diary_prompt(text: str) -> str:
    return COMMON_SCHEMA_HEADER.format(
        context=DIARY_CONTEXT,
        extra_schema=DIARY_EXTRA_SCHEMA,
        entry_type='diary entry',
        text=text,
    )


def build_opic_prompt(text: str, opic_question: str = '') -> str:
    return COMMON_SCHEMA_HEADER.format(
        context=OPIC_CONTEXT.format(opic_question=opic_question or ''),
        extra_schema=OPIC_EXTRA_SCHEMA,
        entry_type='spoken answer',
        text=text,
    )
