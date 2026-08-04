# -*- coding: utf-8 -*-
"""
utils.py
- LaTeX 구분자 변환: \\( \\) / \\[ \\] -> $ $ / $$ $$  (Streamlit markdown은 $ 구분자의 MathJax/KaTeX만 인식)
- 문제 이미지(관리자 등록) / 첨부 이미지(학생 제출) 로컬 저장 헬퍼
"""

import os
import re
import base64
import uuid

PROBLEM_IMAGE_DIR = "problem_images"   # 관리자가 미리 넣어둔 문제 이미지 폴더 (앱과 함께 배포)
UPLOAD_IMAGE_DIR = "uploads"           # 학생이 업로드한 이미지 저장 폴더 (세션 중 임시 저장)

os.makedirs(UPLOAD_IMAGE_DIR, exist_ok=True)


def latex_for_markdown(text: str) -> str:
    """AI/시트 텍스트에 포함된 \\( \\) \\[ \\] 구분자를 Streamlit이 렌더링하는 $ $ / $$ $$ 로 변환."""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"\\\[(.*?)\\\]", r"$$\1$$", s, flags=re.DOTALL)
    s = re.sub(r"\\\((.*?)\\\)", r"$\1$", s, flags=re.DOTALL)
    return s


def find_problem_image_path(filename: str) -> str | None:
    """problem_images/ 폴더에서 파일명으로 이미지 경로 탐색."""
    if not filename:
        return None
    path = os.path.join(PROBLEM_IMAGE_DIR, str(filename).strip())
    return path if os.path.exists(path) else None


def save_uploaded_image(uploaded_file, prefix: str = "img") -> tuple[str, str]:
    """
    streamlit UploadedFile 을 로컬에 저장하고 (파일명, 저장경로)를 반환.
    주의: Streamlit Cloud 등 배포 환경은 파일시스템이 휘발성일 수 있으므로,
    영구 보관이 필요하면 별도 스토리지(Drive API 업로드 등) 연동을 권장합니다.
    """
    ext = os.path.splitext(uploaded_file.name)[1] or ".png"
    unique_name = f"{prefix}_{uuid.uuid4().hex[:8]}{ext}"
    path = os.path.join(UPLOAD_IMAGE_DIR, unique_name)
    with open(path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return unique_name, path


def image_to_base64(path: str) -> tuple[str, str] | None:
    """로컬 이미지 파일 -> (base64문자열, mime타입)"""
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(ext, "image/png")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return b64, mime
