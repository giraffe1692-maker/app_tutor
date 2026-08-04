# -*- coding: utf-8 -*-
"""
sheets_client.py
Google Sheets 연동 모듈 (9개 시트: 계정 / 문제_Lv0~3 / 설문(문항) / 설문(기준) / 설문(기록) / 대화내역)

원본 Apps Script(Code.gs)의 로직을 그대로 Python으로 이식했습니다.
gspread + google-auth 서비스 계정 인증을 사용합니다.

[안정성 개선]
- Streamlit은 상호작용마다 스크립트를 처음부터 다시 실행하는데, 기존 코드는 같은 시트를
  한 번의 재실행 안에서도 여러 번(get_group_progress -> get_chat_history 등) 중복해서 읽어와
  구글 시트 API의 분당 읽기 한도(사용자당 분당 60회)를 쉽게 초과했습니다. 이것이
  "This app has encountered an error... gspread.exceptions.APIError" 의 주된 원인입니다.
- 아래 두 가지로 이를 완화합니다.
  1) 짧은 TTL(기본 4초)의 인메모리 읽기 캐시: 같은 재실행 안에서 동일 시트를 반복 조회해도
     실제 API 호출은 한 번만 나가도록 함. 쓰기(append/update) 발생 시 해당 시트 캐시는 즉시 무효화.
  2) 429(rate limit) / 5xx / 순간 네트워크 오류에 대한 지수 백오프 재시도.
     재시도를 다 소진하면 사용자에게 친절한 SheetsTemporaryError 로 감싸서 올립니다.
     app.py 에서 이 예외를 잡아 "잠시 후 다시 시도해주세요" 형태로 보여줍니다.
"""

from __future__ import annotations
import datetime as dt
import functools
import time
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# 문제 시트 컬럼 인덱스 (0-based) - "문제번호 소문항 제목 문제내용 이미지파일 보기1~5 정답번호 힌트 활성여부"
COL_GROUP, COL_SUB, COL_TITLE, COL_CONTENT, COL_IMAGE = 0, 1, 2, 3, 4
COL_CH1, COL_CH2, COL_CH3, COL_CH4, COL_CH5 = 5, 6, 7, 8, 9
COL_ANSWER_IDX, COL_HINT, COL_ACTIVE = 10, 11, 12

MIN_LEVEL, MAX_LEVEL = 0, 3

# 대화내역에 자동으로 기록되는 "총평(리뷰)" 메시지의 접두어. 중복 생성을 막는 데 사용.
REVIEW_PREFIX = "📊 [단원 총평]"


def _now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SheetsTemporaryError(Exception):
    """구글 시트 API 호출이 재시도 후에도 계속 실패할 때 발생. app.py에서 사용자 친화적으로 처리."""


def _with_retry(max_retries: int = 4, base_delay: float = 0.7):
    """구글 시트 API의 일시적 오류(429 rate limit, 5xx, 네트워크 오류)에 대해 지수 백오프로 재시도."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return fn(*args, **kwargs)
                except gspread.exceptions.APIError as e:
                    last_exc = e
                    status = None
                    try:
                        status = e.response.status_code
                    except Exception:  # noqa: BLE001
                        pass
                    # 명확히 영구적인 오류(예: 403 권한 없음, 404 시트 없음)는 즉시 올림.
                    if status in (400, 401, 403, 404):
                        raise
                    # 429(속도 제한), 5xx, 혹은 원인이 불명확(redacted)한 오류는 재시도.
                    if attempt < max_retries - 1:
                        time.sleep(base_delay * (2 ** attempt))
                        continue
                    raise SheetsTemporaryError(
                        "구글 시트 요청이 반복적으로 실패했습니다 (아마도 짧은 시간에 너무 많은 요청이 "
                        "몰렸을 가능성이 높습니다). 잠시 후 다시 시도해주세요."
                    ) from e
                except (ConnectionError, TimeoutError) as e:
                    last_exc = e
                    if attempt < max_retries - 1:
                        time.sleep(base_delay * (2 ** attempt))
                        continue
                    raise SheetsTemporaryError(
                        "네트워크 연결이 불안정하여 구글 시트 요청에 실패했습니다. 잠시 후 다시 시도해주세요."
                    ) from e
            raise SheetsTemporaryError(str(last_exc))

        return wrapper

    return decorator


class SheetsClient:
    # 같은 재실행(rerun) 안에서 동일 시트를 반복 조회할 때 실제 API 호출을 줄이기 위한 TTL(초)
    _CACHE_TTL = 4.0

    def __init__(self, sheet_id: str, service_account_info: dict):
        creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id = sheet_id
        self._sh = self.gc.open_by_key(sheet_id)
        self._ws_cache = {}
        self._read_cache: dict[str, tuple[float, list[list]]] = {}

    # ---------------------------------------------------------------
    # 내부 유틸 (재시도 래퍼 + 캐시)
    # ---------------------------------------------------------------
    def ws(self, name: str):
        if name not in self._ws_cache:
            self._ws_cache[name] = self._sh.worksheet(name)
        return self._ws_cache[name]

    @_with_retry()
    def _api_get_all_values(self, worksheet):
        return worksheet.get_all_values()

    @_with_retry()
    def _api_update_cell(self, worksheet, row, col, value):
        return worksheet.update_cell(row, col, value)

    @_with_retry()
    def _api_append_row(self, worksheet, row):
        return worksheet.append_row(row)

    @_with_retry()
    def _api_append_rows(self, worksheet, rows):
        return worksheet.append_rows(rows)

    def _invalidate_cache(self, sheet_name: str):
        self._read_cache.pop(sheet_name, None)

    @staticmethod
    def compute_problem_id(row: list) -> str:
        group = str(row[COL_GROUP]).strip()
        sub = str(row[COL_SUB]).strip() if len(row) > COL_SUB else ""
        return f"{group}-{sub}" if sub else group

    @staticmethod
    def parse_problem_id(pid: str) -> tuple[str, str]:
        s = str(pid or "").strip()
        if "-" not in s:
            return s, ""
        idx = s.rfind("-")
        return s[:idx], s[idx + 1:]

    @staticmethod
    def _clamp(n: int, lo: int, hi: int) -> int:
        return max(lo, min(hi, n))

    @staticmethod
    def extract_choices(row: list) -> list[dict]:
        cols = [COL_CH1, COL_CH2, COL_CH3, COL_CH4, COL_CH5]
        choices = []
        for i, c in enumerate(cols):
            if len(row) > c and str(row[c]).strip() != "":
                choices.append({"index": i + 1, "text": str(row[c])})
        return choices

    def _get_all_rows(self, sheet_name: str, use_cache: bool = True) -> list[list]:
        """헤더를 제외한 전체 데이터 행 (빈 셀도 패딩). 짧은 TTL 동안 재조회 시 캐시 사용."""
        now = time.monotonic()
        if use_cache:
            cached = self._read_cache.get(sheet_name)
            if cached and (now - cached[0]) < self._CACHE_TTL:
                return cached[1]

        values = self._api_get_all_values(self.ws(sheet_name))
        if not values:
            rows: list[list] = []
        else:
            header_len = len(values[0])
            rows = []
            for r in values[1:]:
                if len(r) < header_len:
                    r = r + [""] * (header_len - len(r))
                rows.append(r)

        self._read_cache[sheet_name] = (now, rows)
        return rows

    # ---------------------------------------------------------------
    # 1. 로그인 / 신규-기존 판별
    # ---------------------------------------------------------------
    def login_user(self, name: str, student_id: str) -> dict:
        name, student_id = name.strip(), student_id.strip()
        sheet = self.ws("계정")
        rows = self._get_all_rows("계정")
        now = _now_str()

        for i, r in enumerate(rows):
            if str(r[0]).strip() == name and str(r[1]).strip() == student_id:
                self._api_update_cell(sheet, i + 2, 4, now)  # 최근접속일시
                self._invalidate_cache("계정")
                return {
                    "isNew": False,
                    "name": name,
                    "studentId": student_id,
                    "level": str(r[4]),
                    "surveyDone": str(r[5]).strip() == "Y",
                }

        self._api_append_row(sheet, [name, student_id, now, now, "", "N", ""])
        self._invalidate_cache("계정")
        return {"isNew": True, "name": name, "studentId": student_id, "level": "", "surveyDone": False}

    # ---------------------------------------------------------------
    # 2. 설문 문항 / 3. 설문 제출
    # ---------------------------------------------------------------
    def get_survey_questions(self) -> list[dict]:
        rows = self._get_all_rows("설문(문항)")
        qs = [
            {"num": r[0], "text": r[1]}
            for r in rows
            if len(r) > 2 and str(r[2]).strip() == "Y"
        ]
        qs.sort(key=lambda q: float(q["num"]))
        return qs

    def submit_survey(self, name: str, student_id: str, answers: list[dict]) -> dict:
        """answers: [{num, score}, ...]"""
        record_sheet = self.ws("설문(기록)")
        now = _now_str()
        total = 0
        rows_to_append = []
        for a in answers:
            rows_to_append.append([name, student_id, a["num"], a["score"], now])
            total += float(a["score"])
        if rows_to_append:
            self._api_append_rows(record_sheet, rows_to_append)
            self._invalidate_cache("설문(기록)")
        avg = total / len(answers) if answers else 0

        crit_rows = self._get_all_rows("설문(기준)")
        level = ""
        for r in crit_rows:
            lv, lo, hi = str(r[0]), float(r[1]), float(r[2])
            if lo <= avg <= hi:
                level = lv
                break

        acc_sheet = self.ws("계정")
        acc_rows = self._get_all_rows("계정")
        for i, r in enumerate(acc_rows):
            if str(r[0]).strip() == name and str(r[1]).strip() == student_id:
                self._api_update_cell(acc_sheet, i + 2, 5, level)
                self._api_update_cell(acc_sheet, i + 2, 6, "Y")
                self._invalidate_cache("계정")
                break

        return {"level": level, "avg": avg}

    # ---------------------------------------------------------------
    # 4. 문제 조회
    # ---------------------------------------------------------------
    def get_problem_row(self, level, problem_id: Optional[str] = None) -> Optional[list]:
        sheet_name = f"문제_Lv{level}"
        try:
            rows = self._get_all_rows(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            return None
        active = [r for r in rows if len(r) > COL_ACTIVE and str(r[COL_ACTIVE]).strip() == "Y"]
        if not active:
            return None
        if not problem_id:
            return active[0]
        target = str(problem_id).strip()
        for r in active:
            if self.compute_problem_id(r) == target:
                return r
        return None

    def get_active_groups(self, level) -> list[dict]:
        sheet_name = f"문제_Lv{level}"
        try:
            rows = self._get_all_rows(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            return []
        active = [r for r in rows if len(r) > COL_ACTIVE and str(r[COL_ACTIVE]).strip() == "Y"]
        groups, order = {}, []
        for r in active:
            gid = str(r[COL_GROUP]).strip()
            if gid not in groups:
                groups[gid] = {"title": r[COL_TITLE], "content": r[COL_CONTENT], "rows": []}
                order.append(gid)
            groups[gid]["rows"].append(r)

        result = []
        for gid in order:
            g = groups[gid]
            sorted_rows = sorted(g["rows"], key=lambda rr: str(rr[COL_SUB]))
            result.append({
                "groupId": gid, "title": g["title"], "content": g["content"],
                "entryId": self.compute_problem_id(sorted_rows[0]),
            })
        return result

    def get_group_siblings(self, level, group_id: str) -> list[dict]:
        sheet_name = f"문제_Lv{level}"
        try:
            rows = self._get_all_rows(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            return []
        matched = [
            r for r in rows
            if len(r) > COL_ACTIVE and str(r[COL_ACTIVE]).strip() == "Y"
            and str(r[COL_GROUP]).strip() == str(group_id).strip()
        ]
        matched.sort(key=lambda r: str(r[COL_SUB]))
        return [
            {"id": self.compute_problem_id(r), "subLabel": str(r[COL_SUB]), "title": r[COL_TITLE]}
            for r in matched
        ]

    # ---------------------------------------------------------------
    # 소문항 시도/정답 여부 (대화내역을 한 번만 읽어 재사용)
    # ---------------------------------------------------------------
    def get_group_attempts(self, name, student_id, level, group_id) -> dict:
        """group_id 안의 각 소문항(subLabel) -> 마지막 채점 결과(True=정답/False=오답). 시도 안 했으면 키 없음."""
        history = self.get_chat_history(name, student_id, level)
        attempts: dict[str, bool] = {}
        for h in history:
            if h["role"] != "assistant" or str(h.get("level")) != str(level):
                continue
            msg = str(h["message"])
            is_correct = msg.startswith("✅")
            is_incorrect = msg.startswith("❌")
            if not (is_correct or is_incorrect):
                continue
            gid, sub = self.parse_problem_id(h.get("problemId"))
            if gid == str(group_id):
                attempts[sub] = is_correct  # 마지막 시도 결과로 갱신
        return attempts

    def get_solved_sub_labels(self, name, student_id, level, group_id) -> set:
        attempts = self.get_group_attempts(name, student_id, level, group_id)
        return {sub for sub, ok in attempts.items() if ok}

    def get_attempted_sub_labels(self, name, student_id, level, group_id) -> set:
        attempts = self.get_group_attempts(name, student_id, level, group_id)
        return set(attempts.keys())

    def get_group_progress(self, name, student_id, level, group_id) -> dict:
        siblings = self.get_group_siblings(level, group_id)
        if len(siblings) <= 1:
            return {"siblings": siblings, "solvedSubLabels": [], "unlockedSiblings": siblings, "groupComplete": True}

        attempts = self.get_group_attempts(name, student_id, level, group_id)
        attempted = set(attempts.keys())
        solved = {sub for sub, ok in attempts.items() if ok}

        consecutive = 0
        for s in siblings:
            if s["subLabel"] in attempted:
                consecutive += 1
            else:
                break
        unlocked_count = min(consecutive + 1, len(siblings))

        return {
            "siblings": siblings,
            "solvedSubLabels": list(solved),
            "unlockedSiblings": siblings[:unlocked_count],
            "groupComplete": consecutive == len(siblings),
        }

    def get_next_sibling(self, level, group_id, current_sub_label: str) -> Optional[dict]:
        """현재 소문항 바로 다음 소문항 정보를 반환 (없으면 None)."""
        siblings = self.get_group_siblings(level, group_id)
        for i, s in enumerate(siblings):
            if s["subLabel"] == str(current_sub_label):
                if i + 1 < len(siblings):
                    return siblings[i + 1]
                return None
        return None

    def build_problem_payload(self, row, level, name=None, student_id=None) -> Optional[dict]:
        if row is None:
            return None
        group_id = str(row[COL_GROUP]).strip()
        choices = self.extract_choices(row)
        try:
            answer_idx = int(float(row[COL_ANSWER_IDX])) if str(row[COL_ANSWER_IDX]).strip() else 0
        except ValueError:
            answer_idx = 0
        has_choices = len(choices) >= 2 and answer_idx >= 1

        all_siblings = self.get_group_siblings(level, group_id)
        visible = all_siblings
        if len(all_siblings) > 1 and name and student_id:
            progress = self.get_group_progress(name, student_id, level, group_id)
            solved_set = set(progress["solvedSubLabels"])
            visible = [
                {"id": s["id"], "subLabel": s["subLabel"], "title": s["title"], "solved": s["subLabel"] in solved_set}
                for s in progress["unlockedSiblings"]
            ]

        return {
            "id": self.compute_problem_id(row),
            "groupId": group_id,
            "subLabel": str(row[COL_SUB]),
            "level": str(level),
            "title": row[COL_TITLE],
            "content": row[COL_CONTENT],
            "imageFile": row[COL_IMAGE],
            "hasChoices": has_choices,
            "choices": choices if has_choices else [],
            "answerIdx": answer_idx,
            "hint": row[COL_HINT],
            "siblings": visible,
        }

    def get_current_problem(self, name, student_id) -> dict:
        name, student_id = name.strip(), student_id.strip()
        sheet = self.ws("계정")
        rows = self._get_all_rows("계정")

        for i, r in enumerate(rows):
            if str(r[0]).strip() == name and str(r[1]).strip() == student_id:
                level = str(r[4])
                cur_id = str(r[6]) if len(r) > 6 and r[6] else ""
                row = self.get_problem_row(level, cur_id) if cur_id else None
                if row is None:
                    row = self.get_problem_row(level)
                    if row is not None:
                        cur_id = self.compute_problem_id(row)
                        self._api_update_cell(sheet, i + 2, 7, cur_id)
                        self._invalidate_cache("계정")
                return {"level": level, "problem": self.build_problem_payload(row, level, name, student_id)}
        raise ValueError("계정을 찾을 수 없습니다.")

    def get_problem_by_id(self, name, student_id, level, problem_id) -> Optional[dict]:
        row = self.get_problem_row(level, problem_id)
        return self.build_problem_payload(row, level, name, student_id)

    def switch_to_sub_question(self, name, student_id, level, problem_id) -> dict:
        name, student_id = name.strip(), student_id.strip()
        row = self.get_problem_row(level, problem_id)
        if row is None:
            raise ValueError("문제 정보를 찾을 수 없습니다.")

        group_id = str(row[COL_GROUP]).strip()
        target_sub = str(row[COL_SUB]).strip()
        progress = self.get_group_progress(name, student_id, level, group_id)
        if not any(s["subLabel"] == target_sub for s in progress["unlockedSiblings"]):
            raise ValueError("아직 풀 수 없는 소문항입니다. 이전 소문항을 먼저 해결해주세요.")

        self.update_current_problem_id(name, student_id, self.compute_problem_id(row))
        return self.build_problem_payload(row, level, name, student_id)

    # ---------------------------------------------------------------
    # 5. 대화내역
    # ---------------------------------------------------------------
    def get_chat_history(self, name, student_id, level=None, problem_id=None) -> list[dict]:
        rows = self._get_all_rows("대화내역")
        filtered = [r for r in rows if str(r[0]).strip() == name and str(r[1]).strip() == str(student_id)]

        if level not in (None, ""):
            filtered = [r for r in filtered if str(r[8]).strip() == str(level).strip()]

        if problem_id not in (None, ""):
            target_gid, _ = self.parse_problem_id(problem_id)

            def _match(r):
                rpid = str(r[7]).strip()
                if rpid == str(problem_id).strip():
                    return True
                rgid, _ = self.parse_problem_id(rpid)
                return bool(rgid) and rgid == target_gid

            filtered = [r for r in filtered if _match(r)]

        def _seq(r):
            try:
                return float(r[2])
            except (ValueError, IndexError):
                return 0

        filtered.sort(key=_seq)
        return [
            {
                "seq": r[2], "role": r[3], "message": r[4], "imageFile": r[5],
                "time": r[6], "problemId": r[7], "level": r[8],
            }
            for r in filtered
        ]

    def _get_next_seq(self, name, student_id) -> int:
        rows = self._get_all_rows("대화내역")
        mine = [r for r in rows if str(r[0]).strip() == name and str(r[1]).strip() == str(student_id)]
        if not mine:
            return 1
        return int(max(float(r[2]) for r in mine)) + 1

    def append_chat(self, name, student_id, seq, role, message, image_file, problem_id, level):
        self._api_append_row(
            self.ws("대화내역"),
            [name, student_id, seq, role, message, image_file or "", _now_str(), problem_id or "", level or ""],
        )
        self._invalidate_cache("대화내역")

    def get_problem_history(self, name, student_id) -> list[dict]:
        name, student_id = name.strip(), student_id.strip()
        rows = self._get_all_rows("대화내역")
        seen, result = set(), []
        for r in rows:
            if str(r[0]).strip() != name or str(r[1]).strip() != str(student_id):
                continue
            pid, lvl = r[7], r[8]
            if not pid or lvl == "":
                continue
            key = f"{lvl}|{pid}"
            if key in seen:
                continue
            seen.add(key)
            row = self.get_problem_row(lvl, pid)
            title = row[COL_TITLE] if row is not None else f"문제 {pid}"
            result.append({"level": str(lvl), "id": str(pid), "title": title})
        return result

    # ---------------------------------------------------------------
    # 5-2. 단원(그룹) 총평 - 중복 생성 방지
    # ---------------------------------------------------------------
    def has_group_review(self, name, student_id, level, group_id) -> bool:
        """해당 소문항 그룹에 대한 총평이 이미 대화내역에 기록되어 있는지 확인."""
        history = self.get_chat_history(name, student_id, level)
        for h in history:
            if h["role"] != "assistant":
                continue
            if not str(h["message"]).startswith(REVIEW_PREFIX):
                continue
            gid, _ = self.parse_problem_id(h.get("problemId"))
            if gid == str(group_id):
                return True
        return False

    def append_group_review(self, name, student_id, level, group_id, review_text):
        seq = self._get_next_seq(name, student_id)
        self.append_chat(name, student_id, seq, "assistant", f"{REVIEW_PREFIX} {review_text}", "", group_id, level)

    # ---------------------------------------------------------------
    # 6. 레벨 갱신
    # ---------------------------------------------------------------
    def update_account_level(self, name, student_id, new_level):
        sheet = self.ws("계정")
        rows = self._get_all_rows("계정")
        for i, r in enumerate(rows):
            if str(r[0]).strip() == name and str(r[1]).strip() == str(student_id):
                self._api_update_cell(sheet, i + 2, 5, str(new_level))
                self._invalidate_cache("계정")
                break

    def update_current_problem_id(self, name, student_id, problem_id):
        sheet = self.ws("계정")
        rows = self._get_all_rows("계정")
        for i, r in enumerate(rows):
            if str(r[0]).strip() == name and str(r[1]).strip() == str(student_id):
                self._api_update_cell(sheet, i + 2, 7, problem_id)
                self._invalidate_cache("계정")
                break

    @staticmethod
    def clamp_level(n) -> int:
        return SheetsClient._clamp(int(n), MIN_LEVEL, MAX_LEVEL)
