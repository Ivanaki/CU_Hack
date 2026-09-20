from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile


app = FastAPI(title="Media Upload API")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/mp4",
    "audio/x-m4a",
}
CONTENT_TYPE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


def detect_content_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12].lower()
        if brand in {b"heic", b"heix", b"hevc", b"hevx"}:
            return "image/heic"
        if brand in {b"heif", b"mif1", b"msf1"}:
            return "image/heif"
        return "audio/mp4"
    return None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/upload")
async def upload_file(request: Request) -> dict[str, str | int]:
    request_content_type = request.headers.get("content-type", "")

    if request_content_type.startswith("multipart/form-data"):
        form = await request.form()
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=422, detail="Поле file обязательно")

        filename = file.filename or "upload"
        declared_content_type = file.content_type or "application/octet-stream"
        data = await file.read(MAX_FILE_SIZE + 1)
        await file.close()
    else:
        filename = (
            request.query_params.get("filename")
            or request.headers.get("x-filename")
            or "iphone-upload"
        )
        declared_content_type = request_content_type.split(";", 1)[0].lower()
        data = await request.body()

    if not data:
        raise HTTPException(status_code=400, detail="Получен пустой файл")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Максимальный размер файла — 20 МБ")

    detected_content_type = detect_content_type(data)
    content_type = detected_content_type or declared_content_type

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Поддерживаются только изображения и аудиофайлы",
        )

    suffix = CONTENT_TYPE_SUFFIXES[content_type]
    stored_name = f"{uuid4().hex}{suffix}"
    destination = UPLOAD_DIR / stored_name

    try:
        destination.write_bytes(data)
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    return {
        "filename": filename,
        "stored_as": stored_name,
        "content_type": content_type,
        "size": len(data),
        "url": str(request.url_for("files", path=stored_name)),
    }


@app.get("/files")
def list_files(request: Request) -> dict[str, list[dict[str, str | int]]]:
    files = []

    for path in sorted(UPLOAD_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file():
            files.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "url": str(request.url_for("files", path=path.name)),
                }
            )

    return {"files": files}


app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")
