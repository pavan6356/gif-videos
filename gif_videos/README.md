# Chunked Upload Module

## File structure

```
backend/upload/
├── views.py     — 4 API endpoints (chunk, finalize, status, cancel)
├── models.py    — UploadSession record
├── tasks.py     — Celery GIF conversion task + stale purge
├── urls.py      — URL routing
└── README.md

frontend/src/
├── hooks/useChunkedUpload.ts     — core upload logic, retry, resume
└── components/VideoUploader.tsx  — drop zone + progress UI
```

---

## Django settings to add

```python
# settings.py

CHUNK_UPLOAD_ROOT    = '/var/uploads/chunks'   # writable by the app server
MAX_CHUNK_SIZE_BYTES = 10 * 1024 * 1024        # 10 MB per chunk
MAX_FILE_SIZE_BYTES  = 2 * 1024 ** 3           # 2 GB assembled
MAX_TOTAL_CHUNKS     = 500

# Celery
CELERY_BROKER_URL    = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_TRACK_STARTED = True

CELERY_BEAT_SCHEDULE = {
    'purge-stale-uploads': {
        'task':     'upload.tasks.purge_stale_uploads',
        'schedule': 3600,   # every hour (seconds)
    },
}

# DRF
REST_FRAMEWORK = {
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
}

# Nginx: raise client_max_body_size to match MAX_CHUNK_SIZE_BYTES
# client_max_body_size 11m;
```

---

## pip requirements

```
djangorestframework
djangorestframework-simplejwt
celery[redis]
moviepy
redis
```

---

## Quick-start

```bash
# 1. Migrate
python manage.py makemigrations upload
python manage.py migrate

# 2. Start Celery worker
celery -A myproject worker -l info -Q default

# 3. Start Celery beat (stale purge)
celery -A myproject beat -l info

# 4. Wire up urls.py
#    path('api/upload/', include('upload.urls')),
```

---

## API contract

### POST /api/upload/chunk/
```
Content-Type: multipart/form-data
X-CSRFToken: <token>

upload_id     string  UUID (client-generated)
chunk_index   int     0-based
total_chunks  int     total number of chunks
chunk         file    binary blob
```
Response `200`: `{ "received": true, "chunk_index": N }`

### POST /api/upload/finalize/
```json
{ "upload_id": "uuid", "file_name": "myvideo.mp4" }
```
Response `202`: `{ "job_id": "uuid" }`

### GET /api/upload/status/?upload_id=<uuid>
Response `200`: `{ "received_chunks": [0, 1, 3, 4] }`  (missing = [2])

### DELETE /api/upload/cancel/
```json
{ "upload_id": "uuid" }
```
Response `200`: `{ "cancelled": true }`

---

## Resume flow

1. Client generates `uploadId = crypto.randomUUID()` and stores `{ uploadId, fileName, fileSize }` in localStorage.
2. On page reload, client checks localStorage for a matching `(fileName, fileSize)` key.
3. If found, calls `GET /api/upload/status/?upload_id=<uuid>` to confirm which chunks the server already has.
4. Skips confirmed chunks; resumes from the first missing one.
5. On finalize success, removes the localStorage key.

---

## Security notes

- `upload_id` is validated against a strict UUID regex before any filesystem operation — no path traversal possible.
- File magic bytes are checked server-side in `finalize`; the client's `Content-Type` header is ignored.
- Chunk directories are isolated under `CHUNK_UPLOAD_ROOT/<upload_id>/`; no cross-session access.
- Rate limiting should be applied at the nginx/API gateway layer (e.g. 10 requests/min per IP on the chunk endpoint).
```
