from __future__ import annotations  # Python 3.9 호환 — `int | None` 등 PEP 604 어노테이션 lazy 평가

from django.db import models


class User(models.Model):
    """Lightweight user — just an ID issued by admin. No password.

    Entries and any future per-user data point at this row. Admin
    isn't a User row — it's a global password gate.
    """
    username = models.CharField(max_length=64, unique=True, db_index=True)
    display_name = models.CharField(max_length=128, blank=True, default='')
    preferences = models.JSONField(default=dict, blank=True)  # per-user prefs: opic_selected_topics, etc.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.username

    def to_dict(self, entries_count: int | None = None):
        out = {
            'username': self.username,
            'displayName': self.display_name,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
        }
        if entries_count is not None:
            out['entriesCount'] = entries_count
        return out


class Expression(models.Model):
    """Conversational English expression curated from the Notion DB,
    or auto-extracted from a user's diary/opic feedback.

    Shown on the dashboard as "오늘의 영어 회화 표현" — random one per visit,
    click to expand Korean meaning + example + tip + category.

    source 구분:
      'curated'  — Notion DB / admin이 가져온 일반 표현 풀
      'feedback' — 사용자 첨삭 결과에서 자동 추출 (source_entry로 출처 추적)
    """
    SOURCE_CHOICES = [
        ('curated', 'Curated'),
        ('feedback', 'From feedback'),
        ('user_note', 'From user note'),
    ]

    en = models.CharField(max_length=200, unique=True)
    ko = models.CharField(max_length=300)
    example = models.TextField(blank=True, default='')
    tip = models.TextField(blank=True, default='')
    category = models.CharField(max_length=50, blank=True, default='', db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='curated', db_index=True)
    source_entry = models.ForeignKey(
        'Entry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='extracted_expressions',
    )
    source_user = models.ForeignKey(
        'User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='feedback_expressions',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.en

    def to_dict(self):
        return {
            'id': self.id,
            'en': self.en,
            'ko': self.ko,
            'example': self.example,
            'tip': self.tip,
            'category': self.category,
            'source': self.source,
            'sourceEntryId': self.source_entry_id,
            'sourceUser': self.source_user.username if self.source_user_id else None,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
        }


class Entry(models.Model):
    MODE_CHOICES = [
        ('diary', 'Diary'),
        ('opic', 'Opic'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='entries')
    date = models.CharField(max_length=10, db_index=True)  # YYYY-MM-DD
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    text = models.TextField()

    feedback = models.JSONField(null=True, blank=True)
    raw_feedback = models.TextField(null=True, blank=True)

    model = models.CharField(max_length=40, default='haiku')

    # Opic-specific
    opic_combo = models.CharField(max_length=50, null=True, blank=True)
    opic_question_index = models.IntegerField(null=True, blank=True)
    opic_question_text = models.TextField(null=True, blank=True)
    opic_question_type = models.CharField(max_length=50, null=True, blank=True)

    # Audio recording (Opic) — 파일명만 저장. 실제 파일은 data/recordings/<filename>.
    audio_filename = models.CharField(max_length=80, blank=True, default='')

    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['completed_at']

    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date,
            'mode': self.mode,
            'text': self.text,
            'feedback': self.feedback,
            'rawFeedback': self.raw_feedback,
            'model': self.model,
            'opicCombo': self.opic_combo,
            'opicQuestionIndex': self.opic_question_index,
            'opicQuestion': self.opic_question_text,
            'opicQuestionType': self.opic_question_type,
            'audioFilename': self.audio_filename or None,
            'completedAt': self.completed_at.isoformat() if self.completed_at else None,
        }


class Preference(models.Model):
    """Single-row table for user preferences (model choice, daily prompts, etc.)"""
    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()

    def __str__(self):
        return f'{self.key}'
