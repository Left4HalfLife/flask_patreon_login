import secrets
import warnings
from pathlib import Path

from flask import current_app
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from .models import Attachment

TYPES = {
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".png": ("image/png", "image"),
    ".gif": ("image/gif", "image"),
    ".webp": ("image/webp", "image"),
    ".mp4": ("video/mp4", "video"),
    ".webm": ("video/webm", "video"),
    ".pdf": ("application/pdf", "file"),
    ".txt": ("text/plain", "file"),
    ".zip": ("application/zip", "file"),
    ".7z": ("application/x-7z-compressed", "file"),
    ".mp3": ("audio/mpeg", "file"),
    ".wav": ("audio/wav", "file"),
}
IMAGE_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF",
    ".webp": "WEBP",
}


def validate_upload(upload):
    name = secure_filename(upload.filename or "")[:200]
    suffix = Path(name).suffix.lower()
    if suffix not in TYPES:
        raise ValueError(f"Unsupported attachment type: {name or '(unnamed)'}.")
    mime, kind = TYPES[suffix]
    header = upload.stream.read(16)
    upload.stream.seek(0)
    if not header:
        raise ValueError("Empty attachments are not allowed.")
    if kind == "image":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(upload.stream) as image:
                    if image.format != IMAGE_FORMATS[suffix]:
                        raise ValueError("Image contents do not match the filename.")
                    image.verify()
        except (
            UnidentifiedImageError,
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ValueError("Invalid or oversized image.") from exc
        finally:
            upload.stream.seek(0)
    if kind == "video" and (
        (suffix == ".mp4" and header[4:8] != b"ftyp")
        or (suffix == ".webm" and header[:4] != b"\x1a\x45\xdf\xa3")
    ):
        raise ValueError("Invalid video header.")
    return name, mime, kind


def save_upload(upload, validated):
    name, mime, kind = validated
    storage_name = secrets.token_hex(24)
    path = Path(current_app.config["UPLOAD_FOLDER"]) / storage_name
    try:
        upload.save(path)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return Attachment(storage_name=storage_name, name=name, mime=mime, kind=kind)


def remove_files(attachments):
    for attachment in attachments:
        (Path(current_app.config["UPLOAD_FOLDER"]) / attachment.storage_name).unlink(
            missing_ok=True
        )
