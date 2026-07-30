"""
Celery tasks for video → GIF conversion.
Runs inside a worker process separate from the Django web server.
"""

import logging
import os
from pathlib import Path

from celery import shared_task
from django.utils import timezone

from .models import UploadSession

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=10,
    acks_late=True,          # re-queue if worker crashes mid-task
    track_started=True,
)
def process_video_to_gif(self, job_id: str, video_path: str) -> dict:
    """
    Convert an uploaded video to GIF.

    Steps:
      1. Mark session as processing
      2. Extract frames (MoviePy / FFmpeg)
      3. Encode GIF with palette optimisation
      4. Save GIF path to session
      5. Delete source video
      6. Return result dict

    Retries up to 2 times on transient errors.
    """
    session = None
    try:
        session = UploadSession.objects.get(job_id=job_id)
        session.status = 'processing'
        session.save(update_fields=['status', 'updated_at'])

        gif_path = _convert(video_path, job_id)

        session.status   = 'done'
        session.gif_path = gif_path
        session.save(update_fields=['status', 'gif_path', 'updated_at'])

        # Clean up source video
        try:
            os.unlink(video_path)
        except OSError:
            pass

        logger.info('GIF ready job_id=%s gif=%s', job_id, gif_path)
        return {'job_id': job_id, 'gif_path': gif_path}

    except Exception as exc:
        logger.error('GIF task failed job_id=%s err=%s', job_id, exc, exc_info=True)
        if session:
            try:
                self.retry(exc=exc)
            except self.MaxRetriesExceededError:
                session.status = 'failed'
                session.error  = str(exc)
                session.save(update_fields=['status', 'error', 'updated_at'])
        raise


def _convert(video_path: str, job_id: str) -> str:
    """
    Core conversion using moviepy. Swap this function out to plug in
    Tier 2 (OpenCV smart sampling) or Tier 3 (RIFE interpolation).
    """
    from moviepy.editor import VideoFileClip

    out_dir = Path(video_path).parent
    gif_path = str(out_dir / f'{job_id}.gif')

    with VideoFileClip(video_path) as clip:
        # Sensible defaults — expose these as task kwargs for the Controls Panel
        clip = clip.resize(width=480)     # cap width, preserve aspect
        clip = clip.subclip(0, min(clip.duration, 15))  # max 15 s

        clip.write_gif(
            gif_path,
            fps=12,
            program='ffmpeg',      # ffmpeg is ~10× faster than imageio
            opt='OptimizePlus',    # palette optimisation
            fuzz=1,                # dither strength
            loop=0,                # infinite loop
        )

    return gif_path


# ─── Periodic maintenance task ────────────────────────────────────────────────

@shared_task
def purge_stale_uploads() -> dict:
    """
    Remove chunk directories and source videos older than 2 hours
    that were never finalised. Schedule via Celery Beat:

      CELERY_BEAT_SCHEDULE = {
          'purge-stale-uploads': {
              'task': 'upload.tasks.purge_stale_uploads',
              'schedule': crontab(minute=0),   # hourly
          },
      }
    """
    import shutil
    from datetime import timedelta

    from django.conf import settings

    chunk_root = Path(getattr(settings, 'CHUNK_UPLOAD_ROOT', '/tmp/gif_uploads'))
    cutoff     = timezone.now() - timedelta(hours=2)
    removed    = 0

    if not chunk_root.exists():
        return {'removed': 0}

    for entry in chunk_root.iterdir():
        try:
            mtime = timezone.datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass

    logger.info('Purged %d stale upload entries', removed)
    return {'removed': removed}
