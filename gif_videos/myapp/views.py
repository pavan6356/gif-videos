"""
Chunked video upload views.

Endpoints:
  POST /api/upload/chunk/     — receive one chunk
  POST /api/upload/finalize/  — assemble all chunks + enqueue GIF job
  GET  /api/upload/status/    — check an in-progress upload (resume support)
  DELETE /api/upload/cancel/  — discard chunks for an uploadId
"""

import hashlib
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import InMemoryUploadedFile
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from .models import UploadSession
from .tasks import process_video_to_gif

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

CHUNK_UPLOAD_ROOT: Path = Path(getattr(settings, 'CHUNK_UPLOAD_ROOT', '/tmp/gif_uploads'))
MAX_CHUNK_SIZE_BYTES: int = getattr(settings, 'MAX_CHUNK_SIZE_BYTES', 10 * 1024 * 1024)   # 10 MB
MAX_FILE_SIZE_BYTES:  int = getattr(settings, 'MAX_FILE_SIZE_BYTES',  2 * 1024 ** 3)       # 2 GB
MAX_TOTAL_CHUNKS:     int = getattr(settings, 'MAX_TOTAL_CHUNKS', 500)

ALLOWED_MIME_TYPES = {
    'video/mp4', 'video/webm', 'video/quicktime',
    'video/x-matroska', 'video/avi', 'video/x-msvideo',
}

# Safe upload_id: UUID-only, no path traversal possible
UPLOAD_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')


# ─── Helpers ──────────────────────────────────────────────────────────────────

def chunk_dir(upload_id: str) -> Path:
    """Returns the temp directory for a given upload_id."""
    return CHUNK_UPLOAD_ROOT / upload_id


def chunk_path(upload_id: str, index: int) -> Path:
    return chunk_dir(upload_id) / f'{index:05d}.part'


def validate_upload_id(upload_id: str) -> bool:
    return bool(UPLOAD_ID_RE.match(upload_id))


def safe_filename(name: str) -> str:
    """Strip path components, keep extension, limit length."""
    name = Path(name).name          # strip directories
    name = re.sub(r'[^\w.\-]', '_', name)
    return name[:200]


def assembled_path(upload_id: str, file_name: str) -> Path:
    safe = safe_filename(file_name)
    return CHUNK_UPLOAD_ROOT / f'{upload_id}_{safe}'


# ─── POST /api/upload/chunk/ ──────────────────────────────────────────────────

@api_view(['POST'])
@parser_classes([MultiPartParser])
def receive_chunk(request: Request) -> Response:
    """
    Accepts one chunk of a multipart video upload.

    Form fields:
      upload_id     — UUID (client-generated)
      chunk_index   — 0-based integer
      total_chunks  — total number of chunks
      chunk         — binary blob

    Returns 200 JSON { received: true, chunk_index: N }
    """
    # ── Parse + validate fields ──────────────────────────────────────────────
    upload_id   = request.data.get('upload_id', '').strip()
    file_obj: InMemoryUploadedFile = request.FILES.get('chunk')

    try:
        chunk_index  = int(request.data.get('chunk_index', -1))
        total_chunks = int(request.data.get('total_chunks', 0))
    except (TypeError, ValueError):
        return Response({'error': 'chunk_index and total_chunks must be integers'},
                        status=status.HTTP_400_BAD_REQUEST)

    if not validate_upload_id(upload_id):
        return Response({'error': 'Invalid upload_id format'}, status=status.HTTP_400_BAD_REQUEST)

    if file_obj is None:
        return Response({'error': 'Missing chunk file'}, status=status.HTTP_400_BAD_REQUEST)

    if not (0 <= chunk_index < total_chunks):
        return Response({'error': 'chunk_index out of range'}, status=status.HTTP_400_BAD_REQUEST)

    if total_chunks > MAX_TOTAL_CHUNKS:
        return Response({'error': f'Exceeds max chunk count ({MAX_TOTAL_CHUNKS})'},
                        status=status.HTTP_400_BAD_REQUEST)

    if file_obj.size > MAX_CHUNK_SIZE_BYTES:
        return Response({'error': f'Chunk too large (max {MAX_CHUNK_SIZE_BYTES // 1024 // 1024} MB)'},
                        status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    # ── Write chunk to disk ──────────────────────────────────────────────────
    dest = chunk_path(upload_id, chunk_index)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(dest, 'wb') as f:
            for block in file_obj.chunks(chunk_size=256 * 1024):
                f.write(block)
    except OSError as e:
        logger.error('Chunk write failed upload_id=%s chunk=%d err=%s', upload_id, chunk_index, e)
        return Response({'error': 'Disk write failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    logger.debug('Chunk received upload_id=%s index=%d/%d', upload_id, chunk_index, total_chunks - 1)

    return Response({'received': True, 'chunk_index': chunk_index}, status=status.HTTP_200_OK)


# ─── POST /api/upload/finalize/ ───────────────────────────────────────────────

@api_view(['POST'])
def finalize_upload(request: Request) -> Response:
    """
    Assemble all received chunks, validate the assembled file, then
    enqueue a Celery task for GIF conversion.

    JSON body:
      upload_id  — UUID matching chunks on disk
      file_name  — original filename (for extension / logging)

    Returns 202 JSON { job_id: "..." }
    """
    upload_id = request.data.get('upload_id', '').strip()
    file_name = request.data.get('file_name', 'video').strip()

    if not validate_upload_id(upload_id):
        return Response({'error': 'Invalid upload_id'}, status=status.HTTP_400_BAD_REQUEST)

    upload_dir = chunk_dir(upload_id)
    if not upload_dir.exists():
        return Response({'error': 'No chunks found for this upload_id'},
                        status=status.HTTP_404_NOT_FOUND)

    # ── Discover chunks and verify contiguity ────────────────────────────────
    parts = sorted(upload_dir.glob('*.part'), key=lambda p: int(p.stem))

    if not parts:
        return Response({'error': 'No chunk files found'}, status=status.HTTP_400_BAD_REQUEST)

    expected_indices = list(range(len(parts)))
    actual_indices   = [int(p.stem) for p in parts]

    if actual_indices != expected_indices:
        missing = sorted(set(expected_indices) - set(actual_indices))
        return Response({'error': f'Missing chunks: {missing}'}, status=status.HTTP_400_BAD_REQUEST)

    # ── Assemble ─────────────────────────────────────────────────────────────
    out_path = assembled_path(upload_id, file_name)
    total_size = 0

    try:
        with open(out_path, 'wb') as out:
            for part in parts:
                with open(part, 'rb') as p:
                    while True:
                        block = p.read(256 * 1024)
                        if not block:
                            break
                        out.write(block)
                        total_size += len(block)

                        if total_size > MAX_FILE_SIZE_BYTES:
                            out.close()
                            out_path.unlink(missing_ok=True)
                            return Response(
                                {'error': 'Assembled file exceeds size limit'},
                                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            )
    except OSError as e:
        logger.error('Assembly failed upload_id=%s err=%s', upload_id, e)
        out_path.unlink(missing_ok=True)
        return Response({'error': 'Assembly failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # ── Validate assembled file (magic bytes) ────────────────────────────────
    detected_mime = _detect_video_mime(out_path)
    if detected_mime not in ALLOWED_MIME_TYPES:
        out_path.unlink(missing_ok=True)
        return Response(
            {'error': f'Assembled file is not a recognised video (detected: {detected_mime})'},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    # ── Clean up chunk parts ─────────────────────────────────────────────────
    shutil.rmtree(upload_dir, ignore_errors=True)

    # ── Persist session record + enqueue task ────────────────────────────────
    job_id  = str(uuid.uuid4())
    session = UploadSession.objects.create(
        upload_id=upload_id,
        job_id=job_id,
        file_path=str(out_path),
        file_name=safe_filename(file_name),
        file_size=total_size,
        status='queued',
    )

    process_video_to_gif.apply_async(
        args=[job_id, str(out_path)],
        task_id=job_id,
    )

    logger.info(
        'Upload finalised upload_id=%s job_id=%s size=%d bytes',
        upload_id, job_id, total_size,
    )

    return Response({'job_id': job_id}, status=status.HTTP_202_ACCEPTED)


# ─── GET /api/upload/status/ ─────────────────────────────────────────────────

@api_view(['GET'])
def upload_status(request: Request) -> Response:
    """
    Returns which chunks are already on disk for a given upload_id.
    Used by the client to resume an interrupted upload.

    Query params: ?upload_id=<uuid>
    """
    upload_id = request.query_params.get('upload_id', '').strip()

    if not validate_upload_id(upload_id):
        return Response({'error': 'Invalid upload_id'}, status=status.HTTP_400_BAD_REQUEST)

    upload_dir = chunk_dir(upload_id)
    if not upload_dir.exists():
        return Response({'received_chunks': []}, status=status.HTTP_200_OK)

    received = sorted(int(p.stem) for p in upload_dir.glob('*.part'))
    return Response({'received_chunks': received}, status=status.HTTP_200_OK)


# ─── DELETE /api/upload/cancel/ ───────────────────────────────────────────────

@api_view(['DELETE'])
def cancel_upload(request: Request) -> Response:
    """
    Removes all chunk data for a given upload_id.
    Should be called when the user explicitly cancels.
    """
    upload_id = request.data.get('upload_id', '').strip()

    if not validate_upload_id(upload_id):
        return Response({'error': 'Invalid upload_id'}, status=status.HTTP_400_BAD_REQUEST)

    shutil.rmtree(chunk_dir(upload_id), ignore_errors=True)
    return Response({'cancelled': True}, status=status.HTTP_200_OK)


# ─── MIME detection via magic bytes ───────────────────────────────────────────

def _detect_video_mime(path: Path) -> str:
    """
    Check the first 16 bytes against known video magic signatures.
    Does NOT rely on the Content-Type header (which clients can fake).
    """
    SIGNATURES: list[tuple[bytes, int, str]] = [
        (b'\x00\x00\x00\x18ftyp', 0,  'video/mp4'),
        (b'\x00\x00\x00\x20ftyp', 0,  'video/mp4'),
        (b'ftyp',                  4,  'video/mp4'),
        (b'\x1aE\xdf\xa3',        0,  'video/webm'),       # Matroska/WebM EBML
        (b'\x00\x00\x00\x14ftyp', 0,  'video/quicktime'),
        (b'RIFF',                  0,  'video/avi'),
    ]

    try:
        with open(path, 'rb') as f:
            header = f.read(32)
    except OSError:
        return 'application/octet-stream'

    for magic, offset, mime in SIGNATURES:
        if header[offset:offset + len(magic)] == magic:
            return mime

    # QuickTime can start with variable-size atom; check 'ftyp' anywhere in first 32 bytes
    if b'ftyp' in header:
        atom = header[header.index(b'ftyp') + 4:header.index(b'ftyp') + 8]
        if atom in (b'qt  ', b'MSNV'):
            return 'video/quicktime'

    return 'application/octet-stream'
