from django.db import models


class Entry(models.Model):
    MODE_CHOICES = [
        ('diary', 'Diary'),
        ('opic', 'Opic'),
    ]
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
            'completedAt': self.completed_at.isoformat() if self.completed_at else None,
        }


class Preference(models.Model):
    """Single-row table for user preferences (model choice, daily prompts, etc.)"""
    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()

    def __str__(self):
        return f'{self.key}'
