# -*- coding: utf-8 -*-
"""
groq_client.py
Groq Chat Completions API 호출 모듈 (자동 모델 폴백 포함).
API 키는 반드시 st.secrets["GROQ_API_KEY"] 를 통해 주입하세요 (코드에 하드코딩 금지).
"""

import re
import json
import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

THINK_TAG_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


def _strip_think(text: str) -> str:
    return THINK_TAG_RE.sub("", text or "").strip()


class GroqClient:
    def __init__(self, api_key: str, primary_model: str, fallback_models: list[str] | None = None):
        self.api_key = api_key
        self.primary_model = primary_model
        self.fallback_models = fallback_models or []

    def _models_to_try(self) -> list[str]:
        models = [self.primary_model] + [m for m in self.fallback_models if m != self.primary_model]
        return models

    def chat(self, messages: list[dict], json_mode: bool = False, temperature: float | None = None,
              max_tokens: int | None = None) -> str:
        last_error = ""
        for model_name in self._models_to_try():
            payload = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature if temperature is not None else (0.1 if json_mode else 0.4),
                "max_tokens": max_tokens if max_tokens is not None else (1024 if json_mode else 1536),
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                res = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    data=json.dumps(payload),
                    timeout=60,
                )
                data = res.json()
                if "error" in data:
                    last_error = data["error"].get("message", str(data["error"]))
                    continue
                content = data["choices"][0]["message"].get("content", "")
                content = _strip_think(content)
                if content:
                    return content
            except Exception as e:  # noqa: BLE001
                last_error = str(e)

        return f"AI 응답 처리 중 오류가 발생했습니다: {last_error}"

    @staticmethod
    def extract_json(text: str) -> dict | None:
        if not text:
            return None
        s = _strip_think(text)
        s = re.sub(r"```json|```", "", s).strip()
        match = re.search(r"\{[\s\S]*\}", s)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return None
