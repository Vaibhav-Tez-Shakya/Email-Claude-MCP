from pathlib import Path
import base64
import hashlib

BASE_DIR = Path(__file__).resolve().parent.parent
ATTACHMENTS_DIR = BASE_DIR / "attachments"


def get_attachment_directory(email_id):
    directory = ATTACHMENTS_DIR / str(email_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def decode_attachment_data(data):
    if not data:
        return b""

    return base64.urlsafe_b64decode(data)


def safe_filename(filename):
    filename = Path(filename or "attachment").name

    if not filename:
        filename = "attachment"

    return filename


def attachment_hash(data):
    return hashlib.sha256(data).hexdigest()


def extract_attachment_parts(payload):
    attachments = []

    def walk(part):
        if not part:
            return

        filename = part.get("filename") or ""
        mime_type = part.get("mimeType") or "application/octet-stream"
        body = part.get("body") or {}

        if filename:
            attachments.append({
                "filename": filename,
                "mime_type": mime_type,
                "attachment_id": body.get("attachmentId"),
                "size": body.get("size") or 0,
                "data": body.get("data")
            })

        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return attachments
