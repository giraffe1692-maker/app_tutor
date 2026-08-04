# -*- coding: utf-8 -*-
"""
drive_client.py
관리자가 등록한 "문제 이미지"를 구글 드라이브 폴더에서 파일명으로 찾아 base64로 반환하는 모듈.
(원본 Apps Script의 getProblemImageBase64_ 함수와 동일한 방식)

- 시트의 "이미지파일" 칸에는 파일명만 적어두고, 실제 이미지는 지정한 Drive 폴더에 올려두면 됩니다.
- 서비스 계정에 해당 Drive 폴더를 "뷰어"로 공유해야 접근할 수 있습니다.
"""

import io
import base64

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class DriveClient:
    def __init__(self, service_account_info: dict, folder_id: str):
        creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.folder_id = folder_id
        self._cache: dict[str, tuple[str, str] | None] = {}

    def get_image_base64(self, filename: str) -> tuple[str, str] | None:
        """파일명 -> (base64문자열, mimeType). 못 찾으면 None. 같은 파일은 세션 중 캐시."""
        if not filename:
            return None
        filename = str(filename).strip()
        if not filename:
            return None
        if filename in self._cache:
            return self._cache[filename]

        result = self._fetch(filename)
        self._cache[filename] = result
        return result

    def _fetch(self, filename: str) -> tuple[str, str] | None:
        if not self.folder_id:
            return None
        try:
            safe_name = filename.replace("'", "\\'")
            query = f"name = '{safe_name}' and '{self.folder_id}' in parents and trashed = false"
            res = self.service.files().list(
                q=query, fields="files(id, name, mimeType)", pageSize=1,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            files = res.get("files", [])
            if not files:
                return None

            file_id = files[0]["id"]
            mime_type = files[0].get("mimeType") or "image/png"

            request = self.service.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            return b64, mime_type
        except Exception:  # noqa: BLE001
            return None
