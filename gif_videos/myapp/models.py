from django.db import models


class UploadSession(models.Model):
    """Tracks one complete video → GIF pipeline run."""

    STATUS_CHOICES = [
        ('queued',     'Queued'),
        ('processing', 'Processing'),
        ('done',       'Done'),
        ('failed',     'Failed'),
    ]

    upload_id  = models.CharField(max_length=36, unique=True, db_index=True)
    job_id     = models.CharField(max_length=36, unique=True, db_index=True)
    file_path  = models.CharField(max_length=512)
    file_name  = models.CharField(max_length=255)
    file_size  = models.PositiveBigIntegerField()
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    gif_path   = models.CharField(max_length=512, blank=True)
    error      = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'UploadSession({self.job_id[:8]}… — {self.status})'
