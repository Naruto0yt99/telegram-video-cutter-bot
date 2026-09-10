from pathlib import Path

from config import GOOGLE_CLIENT_SECRET, GOOGLE_TOKEN, TEMP_DIR


SCOPES = [
    "https://www.googleapis.com/auth/drive",
]


def get_drive_service():
    """
    Create an authenticated Google Drive API service.

    First authentication requires:
        client_secret.json

    After authentication:
        data/token.json
    """

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials = None
    token_path = Path(GOOGLE_TOKEN)
    client_secret_path = Path(GOOGLE_CLIENT_SECRET)

    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            SCOPES,
        )

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if not client_secret_path.exists():
            raise FileNotFoundError(
                f"Google OAuth file not found: {client_secret_path}"
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_path),
            SCOPES,
        )

        credentials = flow.run_local_server(
            host="localhost",
            port=0,
            access_type="offline",
            prompt="consent",
        )

        token_path.parent.mkdir(parents=True, exist_ok=True)

        token_path.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def get_file_id_from_url(url: str) -> str | None:
    import re

    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)

        if match:
            return match.group(1)

    return None


def make_drive_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def upload_file(
    file_path: str | Path,
    filename: str | None = None,
) -> str:

    from googleapiclient.http import MediaFileUpload

    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(file_path)

    service = get_drive_service()

    if not filename:
        filename = file_path.name

    metadata = {
        "name": filename,
    }

    media = MediaFileUpload(
        str(file_path),
        resumable=True,
    )

    result = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,size,webViewLink",
        )
        .execute()
    )

    file_id = result["id"]

    # Try to make the generated file accessible by link.
    try:
        service.permissions().create(
            fileId=file_id,
            body={
                "type": "anyone",
                "role": "reader",
            },
        ).execute()
    except Exception:
        # Some Google Workspace accounts restrict public sharing.
        pass

    return make_drive_view_url(file_id)


def download_file(
    file_id: str,
    destination: str | Path,
):
    from googleapiclient.http import MediaIoBaseDownload

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    service = get_drive_service()

    request = service.files().get_media(
        fileId=file_id,
    )

    with destination.open("wb") as output:
        downloader = MediaIoBaseDownload(
            output,
            request,
        )

        done = False

        while not done:
            _, done = downloader.next_chunk()

    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("Google Drive download produced an empty file.")

    return destination
