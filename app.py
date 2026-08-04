# -*- coding: utf-8 -*-
"""
app.py
물리 문제 힌트 제공 앱 (Streamlit 버전)

원본 Google Apps Script(Code.gs + index.html)의 기능을 Python + Streamlit으로 이식.
- 구글 시트 9종 연동 (gspread)
- Groq API를 통한 대화형 힌트 / 채점 피드백
- LaTeX 렌더링은 Streamlit 기본 마크다운($...$, $$...$$)을 사용

실행 전 .streamlit/secrets.toml 에 아래 값을 설정해야 합니다. (예시는 .streamlit/secrets_example.toml 참고)
"""

import streamlit as st

from sheets_client import SheetsClient
from groq_client import GroqClient
from utils import latex_for_markdown, find_problem_image_path, save_uploaded_image, image_to_base64

st.set_page_config(page_title="물리 문제 힌트 제공 시스템", page_icon="🧲", layout="centered")

# ---------------------------------------------------------------------------
# 리소스 초기화 (캐시)
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def get_sheets_client() -> SheetsClient:
    sheet_id = st.secrets["SHEET_ID"]
    sa_info = dict(st.secrets["gcp_service_account"])
    return SheetsClient(sheet_id=sheet_id, service_account_info=sa_info)


@st.cache_resource(show_spinner=False)
def get_groq_client() -> GroqClient:
    return GroqClient(
        api_key=st.secrets["GROQ_API_KEY"],
        primary_model=st.secrets.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        fallback_models=st.secrets.get(
            "GROQ_FALLBACK_MODELS",
            ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        ),
    )


def init_state():
    defaults = {
        "stage": "login",          # login | survey | main
        "user": None,               # {name, studentId, level, surveyDone}
        "active_problem": None,
        "preview_problem": None,
        "preview_mode": False,
        "survey_questions": None,
        "survey_answers": {},
        "chat_tab": "현재 대화",
        "answer_result": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_state()

try:
    sheets = get_sheets_client()
    groq = get_groq_client()
except Exception as e:  # noqa: BLE001
    st.error(
        "설정 오류: 구글 시트 / Groq API 연결에 실패했습니다. "
        ".streamlit/secrets.toml 설정을 확인해주세요.\n\n" + str(e)
    )
    st.stop()

SYSTEM_PROMPT_HEADER = (
    "당신은 친절하고 인내심 있는 고등학교 물리 선생님입니다. "
    "학생이 스스로 사고하여 답을 찾도록 단계적인 힌트만 제공하고, 최종 정답(보기 번호)은 학생이 근접하게 접근하기 전까지 직접 알려주지 마세요. "
    "설명은 반드시 자연스럽고 친절한 한국어로, 간결하고 명확하게 합니다. "
    "내부 사고 과정이나 <think> 태그, 영어 초안을 절대 출력하지 말고 한국어 답변만 바로 작성하세요.\n"
    "수식이나 물리 기호를 표현할 때는 항상 LaTeX 표기를 사용하세요. 인라인 수식은 \\( 와 \\) 사이에, "
    "독립된 줄로 강조할 수식은 \\[ 와 \\] 사이에 작성하세요. "
    "아래첨자·위첨자가 두 글자 이상이면 반드시 중괄호로 묶으세요 (예: q_{enc}, v_{0})."
)


# ---------------------------------------------------------------------------
# 1. 로그인 화면
# ---------------------------------------------------------------------------
def render_login():
    st.title("🧲 물리 문제 힌트 제공 시스템")
    st.subheader("사용자 확인")

    with st.form("login_form"):
        name = st.text_input("이름")
        student_id = st.text_input("학번")
        submitted = st.form_submit_button("접속하기", use_container_width=True)

    if submitted:
        if not name.strip() or not student_id.strip():
            st.error("이름과 학번을 모두 입력해주세요.")
            return
        with st.spinner("확인 중..."):
            res = sheets.login_user(name, student_id)
        st.session_state.user = res
        st.session_state.stage = "survey" if (res["isNew"] or not res["surveyDone"]) else "main"
        st.rerun()


# ---------------------------------------------------------------------------
# 2. 설문 화면
# ---------------------------------------------------------------------------
def render_survey():
    st.title("물리 수준 진단 설문")

    if st.session_state.survey_questions is None:
        with st.spinner("설문 문항을 불러오는 중..."):
            st.session_state.survey_questions = sheets.get_survey_questions()

    questions = st.session_state.survey_questions
    if not questions:
        st.warning("등록된 설문 문항이 없습니다. 관리자에게 문의해주세요.")
        return

    answers = st.session_state.survey_answers
    answered = len(answers)
    st.progress(answered / len(questions) if questions else 0, text=f"{answered} / {len(questions)} 문항 응답 완료")

    LIKERT_OPTIONS = [7, 6, 5, 4, 3, 2, 1]  # 왼쪽=그렇다(7점) ~ 오른쪽=그렇지 않다(1점)

    for q in questions:
        st.markdown(f"**{q['num']}. {latex_for_markdown(q['text'])}**")
        cols = st.columns([1] + [1] * 7 + [1])
        with cols[0]:
            st.caption("그렇다")
        default_idx = LIKERT_OPTIONS.index(answers.get(q["num"])) if q["num"] in answers else None
        choice = st.radio(
            f"q_{q['num']}", options=LIKERT_OPTIONS, index=default_idx,
            horizontal=True, label_visibility="collapsed", key=f"likert_{q['num']}",
        )
        with cols[-1]:
            st.caption("그렇지 않다")
        if choice is not None:
            answers[q["num"]] = choice
        st.divider()

    all_answered = len(answers) >= len(questions)
    if st.button("제출하기", disabled=not all_answered, use_container_width=True, type="primary"):
        payload = [{"num": q["num"], "score": answers[q["num"]]} for q in questions]
        with st.spinner("제출 중..."):
            res = sheets.submit_survey(st.session_state.user["name"], st.session_state.user["studentId"], payload)
        st.session_state.user["level"] = res["level"]
        st.session_state.stage = "main"
        st.session_state.survey_questions = None
        st.session_state.survey_answers = {}
        st.rerun()


# ---------------------------------------------------------------------------
# 문제 이미지 렌더
# ---------------------------------------------------------------------------
def render_problem_image(image_file: str):
    if not image_file:
        return
    path = find_problem_image_path(image_file)
    if path:
        st.image(path, use_container_width=True)
    else:
        st.warning(f"⚠ 이미지 파일({image_file})을 찾을 수 없습니다. `problem_images/` 폴더를 확인해주세요.")


# ---------------------------------------------------------------------------
# 문제 박스 렌더 (활성/미리보기 공용)
# ---------------------------------------------------------------------------
def render_problem_box(problem: dict, is_preview: bool):
    user = st.session_state.user
    sub_title = f" - {problem['subLabel']}" if problem.get("subLabel") else ""
    st.markdown(f"### Lv.{problem['level']} · {latex_for_markdown(problem['title'])}{sub_title}")

    # 소문항 탐색 네비게이션
    siblings = problem.get("siblings") or []
    if len(siblings) > 1:
        cols = st.columns(len(siblings))
        for i, s in enumerate(siblings):
            label = s["subLabel"] or "·"
            if s.get("solved"):
                label += " ✓"
            is_current = s["id"] == problem["id"]
            with cols[i]:
                if st.button(label, key=f"subq_{is_preview}_{s['id']}", disabled=is_current,
                             use_container_width=True, type="primary" if is_current else "secondary"):
                    target_level = problem["level"]
                    try:
                        if is_preview:
                            new_p = sheets.get_problem_by_id(user["name"], user["studentId"], target_level, s["id"])
                            st.session_state.preview_problem = new_p
                        else:
                            new_p = sheets.switch_to_sub_question(user["name"], user["studentId"], target_level, s["id"])
                            st.session_state.active_problem = new_p
                        st.session_state.answer_result = None
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))

    st.markdown(latex_for_markdown(problem["content"]))
    render_problem_image(problem.get("imageFile"))

    st.markdown("---")

    answer_key_prefix = f"preview_{problem['id']}" if is_preview else f"active_{problem['id']}"
    selected_choice = None
    free_text_answer = ""

    if problem.get("hasChoices"):
        choice_labels = [f"{c['index']}. {c['text']}" for c in problem["choices"]]
        picked = st.radio(
            "보기를 선택하세요", options=list(range(len(choice_labels))),
            format_func=lambda i: latex_for_markdown(choice_labels[i]),
            key=f"choice_{answer_key_prefix}", index=None,
        )
        if picked is not None:
            selected_choice = problem["choices"][picked]["index"]
    else:
        free_text_answer = st.text_input("최종 정답을 입력하세요", key=f"text_{answer_key_prefix}")

    uploaded_img = st.file_uploader(
        "필기 사진 첨부 (선택)", type=["png", "jpg", "jpeg", "webp"], key=f"upload_{answer_key_prefix}"
    )
    if problem.get("hasChoices"):
        st.caption("사진은 선택사항이며 채점에는 사용되지 않고, AI 피드백 작성 시 참고 자료로만 활용됩니다.")

    submit_disabled = False
    if problem.get("hasChoices") and selected_choice is None:
        submit_disabled = True
    if not problem.get("hasChoices") and not free_text_answer.strip() and uploaded_img is None:
        submit_disabled = True

    if st.button("정답 제출", key=f"submit_{answer_key_prefix}", type="primary",
                 use_container_width=True, disabled=submit_disabled):
        handle_answer_submit(problem, is_preview, selected_choice, free_text_answer, uploaded_img)

    if st.session_state.answer_result and st.session_state.answer_result.get("problem_id") == problem["id"]:
        res = st.session_state.answer_result
        if res["correct"]:
            st.success(latex_for_markdown(res["feedback"]))
        else:
            st.error(latex_for_markdown(res["feedback"]))

    if is_preview:
        st.info("📖 이전에 푼 문제입니다. 다시 제출해도 레벨은 변하지 않습니다.")
        if st.button("현재 문제로 돌아가기", key=f"return_{problem['id']}"):
            st.session_state.preview_mode = False
            st.session_state.preview_problem = None
            st.session_state.answer_result = None
            st.rerun()
    else:
        if st.button("다음 문제 출제", key=f"next_{problem['id']}"):
            handle_next_problem(problem)


# ---------------------------------------------------------------------------
# 정답 제출 -> 채점 -> 레벨 조정
# ---------------------------------------------------------------------------
def handle_answer_submit(problem, is_preview, selected_choice, free_text_answer, uploaded_img):
    user = st.session_state.user
    level, problem_id = problem["level"], problem["id"]

    img_filename, img_b64, img_mime = "", None, None
    if uploaded_img is not None:
        img_filename, img_path = save_uploaded_image(uploaded_img, prefix="answer")
        pair = image_to_base64(img_path)
        if pair:
            img_b64, img_mime = pair

    has_choices = problem.get("hasChoices")
    correct_idx = problem.get("answerIdx", 0)
    hint = problem.get("hint", "")

    with st.spinner("AI가 채점 중입니다..."):
        if has_choices:
            correct = selected_choice == correct_idx
            selected_text = next((c["text"] for c in problem["choices"] if c["index"] == selected_choice), "")
            correct_text = next((c["text"] for c in problem["choices"] if c["index"] == correct_idx), "")

            grading_system = (
                "당신은 학생을 다정하게 지도하는 고등학교 물리 선생님입니다. "
                "학생의 객관식 정답 여부는 이미 결정되어 있습니다. "
                "선생님으로서 학생에게 전달할 맞춤형 한국어 채점 피드백을 작성하세요.\n"
                "1. 정답인 경우: 따뜻한 칭찬과 함께 핵심 물리 개념/수식을 1~2문장으로 짚어주세요.\n"
                "2. 오답인 경우: 정답 보기 번호나 정답을 직접 절대 알려주지 마세요. 대신 학생이 스스로 답을 찾을 수 있도록 "
                "이 문제에서 꼭 생각해보아야 할 핵심 물리 개념이나 공식에 대한 유도 질문을 1개 던져주세요.\n"
                "수식은 LaTeX 표기를 사용하세요 (예: q_{enc}).\n"
                "반드시 아래 형식의 유효한 JSON만 출력하세요. 다른 서론이나 생각 과정은 절대로 출력하지 마세요.\n"
                '{"feedback": "학생에게 전달할 피드백 2~3문장"}'
            )
            choices_str = " / ".join(f"{c['index']}. {c['text']}" for c in problem["choices"])
            user_text = (
                f"[문제 제목] {problem['title']}\n[문제 내용] {problem['content']}\n"
                + (f"[교사용 힌트 참고] {hint}\n" if hint else "")
                + f"[보기] {choices_str}\n"
                + f"[학생이 선택한 보기] {selected_choice}. {selected_text}\n"
                + f"[채점 결과] {'정답' if correct else '오답'}"
            )
            content_arr = [{"type": "text", "text": user_text}]
            prob_img = find_problem_image_path(problem.get("imageFile"))
            if prob_img:
                pair = image_to_base64(prob_img)
                if pair:
                    content_arr.append({"type": "image_url", "image_url": {"url": f"data:{pair[1]};base64,{pair[0]}"}})
            if img_b64:
                content_arr.append({"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{img_b64}"}})

            raw = groq.chat(
                [{"role": "system", "content": grading_system}, {"role": "user", "content": content_arr}],
                json_mode=True,
            )
            parsed = groq.extract_json(raw)
            if parsed and parsed.get("feedback"):
                feedback = parsed["feedback"]
            else:
                feedback = (
                    "축하합니다! 핵심 원리를 정확하게 파악하여 문제를 해결했습니다."
                    if correct else
                    "아쉽게도 정답이 아닙니다. 어떤 물리 법칙이나 수식을 적용해야 할지 다시 한번 떠올려볼까요?"
                )
        else:
            grading_system = (
                "당신은 물리 선생님입니다. 학생이 제출한 답안을 평가하여 JSON으로 반환하세요.\n"
                '{"correct": true 또는 false, "feedback": "학생에게 전달할 피드백 2~3문장"}'
            )
            user_text = (
                f"[문제 제목] {problem['title']}\n[문제 내용] {problem['content']}\n"
                f"[학생 답안] {free_text_answer or '(사진 답안 제출)'}"
            )
            content_arr = [{"type": "text", "text": user_text}]
            if img_b64:
                content_arr.append({"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{img_b64}"}})

            raw = groq.chat(
                [{"role": "system", "content": grading_system}, {"role": "user", "content": content_arr}],
                json_mode=True,
            )
            parsed = groq.extract_json(raw)
            if parsed:
                correct = bool(parsed.get("correct"))
                feedback = parsed.get("feedback") or ("정답입니다!" if correct else "오답입니다.")
            else:
                correct = False
                feedback = "오답입니다. 다시 도전해볼까요?"

    # 레벨 조정 (활성 문제에서만 수행, 미리보기는 레벨 불변)
    new_level = int(level)
    group_complete = True
    level_note = ""
    if not is_preview:
        group_id, sub_label = sheets.parse_problem_id(problem_id)
        siblings = sheets.get_group_siblings(level, group_id)

        if len(siblings) > 1:
            attempts = sheets.get_group_attempts(user["name"], user["studentId"], level, group_id)
            if sub_label not in attempts:
                attempts[sub_label] = correct
            group_complete = all(s["subLabel"] in attempts for s in siblings)
            if group_complete:
                total = len(siblings)
                correct_count = sum(1 for s in siblings if attempts.get(s["subLabel"]) is True)
                required = total // 2 if total % 2 == 0 else round(total / 2)
                if correct_count >= required:
                    new_level = SheetsClient.clamp_level(int(level) + 1)
                else:
                    new_level = SheetsClient.clamp_level(int(level) - 1)
        else:
            group_complete = True
            new_level = SheetsClient.clamp_level(int(level) + 1) if correct else SheetsClient.clamp_level(int(level) - 1)

        if new_level != int(level):
            sheets.update_account_level(user["name"], user["studentId"], new_level)

    # 대화내역 기록
    seq = sheets._get_next_seq(user["name"], user["studentId"])
    submitted_text = (
        f"[정답 제출] {selected_choice}번 선택" + (f" / {free_text_answer}" if free_text_answer else "")
        if has_choices else f"[정답 제출] {free_text_answer or ''}"
    )
    sheets.append_chat(user["name"], user["studentId"], seq, "user", submitted_text, img_filename, problem_id, level)
    seq += 1

    prefix = "✅ 정답입니다! " if correct else "❌ 오답입니다. "
    if is_preview:
        level_note = " (복습용 제출)"
    elif not group_complete:
        level_note = " (다음 소문항으로 연결됩니다)"
    else:
        if new_level > int(level):
            level_note = f" (모든 소문항 완료! 레벨 상승: Lv.{level} → Lv.{new_level})"
        elif new_level < int(level):
            level_note = f" (모든 소문항 완료! 레벨 하락: Lv.{level} → Lv.{new_level})"
        else:
            level_note = f" (모든 소문항 완료! 레벨 유지: Lv.{level})"

    final_message = prefix + feedback + level_note
    sheets.append_chat(user["name"], user["studentId"], seq, "assistant", final_message, "", problem_id, level)

    if not is_preview:
        st.session_state.user["level"] = str(new_level)
        refreshed = sheets.get_problem_by_id(user["name"], user["studentId"], level, problem_id)
        st.session_state.active_problem = refreshed
    else:
        refreshed = sheets.get_problem_by_id(user["name"], user["studentId"], level, problem_id)
        st.session_state.preview_problem = refreshed

    st.session_state.answer_result = {"problem_id": problem_id, "correct": correct, "feedback": final_message}
    st.rerun()


# ---------------------------------------------------------------------------
# 다음 문제 출제
# ---------------------------------------------------------------------------
def handle_next_problem(current_problem):
    user = st.session_state.user
    level = current_problem["level"]
    prev_group_id, _ = sheets.parse_problem_id(current_problem["id"])

    all_groups = sheets.get_active_groups(level)
    candidates = [g for g in all_groups if g["groupId"] != str(prev_group_id)]
    pool = candidates if candidates else all_groups
    if not pool:
        st.warning("다음 문제를 찾지 못했습니다. 관리자에게 문의해주세요.")
        return

    with st.spinner("다음 문제를 고르는 중..."):
        history = sheets.get_chat_history(user["name"], user["studentId"])[-10:]
        history_text = "\n".join(("학생: " if h["role"] == "user" else "선생님: ") + str(h["message"]) for h in history)
        list_text = "\n".join(f"- ID:{p['groupId']} / 제목:{p['title']} / 내용:{p['content']}" for p in pool)
        prompt = (
            f"아래는 학생과 나눈 최근 대화 내용입니다:\n{history_text}\n\n"
            f"다음은 같은 난이도에서 출제 가능한 문제 목록입니다:\n{list_text}\n\n"
            "학생이 방금 푼 문제와 비슷한 유형이면서 학생에게 적절한 다음 문제를 하나 고르세요. "
            "다른 설명 없이 오직 선택한 문제의 ID만 정확히 출력하세요."
        )
        reply = groq.chat(
            [{"role": "system", "content": "당신은 문제를 선별하는 물리 교사입니다. 반드시 문제 ID만 출력합니다."},
             {"role": "user", "content": prompt}],
        )
        import re as _re
        m = _re.search(r"[A-Za-z0-9_\-]+", str(reply))
        chosen_gid = m.group(0) if m else None
        if not chosen_gid or not any(p["groupId"] == chosen_gid for p in pool):
            import random
            chosen_gid = random.choice(pool)["groupId"]

        chosen = next(p for p in pool if p["groupId"] == chosen_gid)
        row = sheets.get_problem_row(level, chosen["entryId"])
        if row is None:
            st.warning("다음 문제를 찾지 못했습니다.")
            return

        sheets.update_current_problem_id(user["name"], user["studentId"], chosen["entryId"])
        seq = sheets._get_next_seq(user["name"], user["studentId"])
        sheets.append_chat(user["name"], user["studentId"], seq, "assistant",
                            f"[새 문제 출제] {row[2]}", "", chosen["entryId"], level)

        new_problem = sheets.build_problem_payload(row, level, user["name"], user["studentId"])

    st.session_state.active_problem = new_problem
    st.session_state.preview_mode = False
    st.session_state.answer_result = None
    st.rerun()


# ---------------------------------------------------------------------------
# 채팅
# ---------------------------------------------------------------------------
def render_chat(problem):
    user = st.session_state.user
    level = problem["level"] if problem else user["level"]
    problem_id = problem["id"] if problem else ""

    history = sheets.get_chat_history(user["name"], user["studentId"], level, problem_id)
    for h in history:
        role = "user" if h["role"] == "user" else "assistant"
        with st.chat_message(role):
            st.markdown(latex_for_markdown(h["message"]))
            if h.get("imageFile"):
                st.caption(f"📎 {h['imageFile']}")

    col_text, col_img = st.columns([4, 1])
    with col_img:
        chat_img = st.file_uploader("사진", type=["png", "jpg", "jpeg", "webp"],
                                     key=f"chat_img_{level}_{problem_id}", label_visibility="collapsed")

    prompt = st.chat_input("질문을 입력하거나 정답을 적어보세요")
    if prompt:
        send_chat_message(user, level, problem_id, prompt, chat_img)


def send_chat_message(user, level, problem_id, message, chat_img):
    img_filename, img_b64, img_mime = "", None, None
    if chat_img is not None:
        img_filename, img_path = save_uploaded_image(chat_img, prefix="chat")
        pair = image_to_base64(img_path)
        if pair:
            img_b64, img_mime = pair

    seq = sheets._get_next_seq(user["name"], user["studentId"])
    sheets.append_chat(user["name"], user["studentId"], seq, "user", message, img_filename, problem_id, level)

    row = sheets.get_problem_row(level, problem_id) if problem_id else None
    problem_title = row[2] if row is not None else ""
    problem_content = row[3] if row is not None else ""
    hint = row[11] if row is not None else ""
    choices = sheets.extract_choices(row) if row is not None else []
    choices_text = ("\n[보기]\n" + "\n".join(f"{c['index']}. {c['text']}" for c in choices)) if choices else ""

    system_prompt = (
        SYSTEM_PROMPT_HEADER + "\n\n"
        f"[문제 제목] {problem_title}\n[문제 내용] {problem_content}{choices_text}\n"
        + (f"[교사용 힌트 자료 - 학생에게 그대로 노출 금지] {hint}" if hint else "")
    )

    with st.spinner("AI 선생님이 답변을 작성 중입니다..."):
        messages = [{"role": "system", "content": system_prompt}]

        prob_img_path = find_problem_image_path(row[4]) if row is not None else None
        if prob_img_path:
            pair = image_to_base64(prob_img_path)
            if pair:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "[참고용 문제 이미지입니다]"},
                        {"type": "image_url", "image_url": {"url": f"data:{pair[1]};base64,{pair[0]}"}},
                    ],
                })
                messages.append({"role": "assistant", "content": "문제 이미지를 확인했습니다."})

        history = sheets.get_chat_history(user["name"], user["studentId"], level, problem_id)
        active_history = history[:-1]  # 방금 저장한 user 메시지 제외
        for h in active_history[-8:]:
            messages.append({"role": "user" if h["role"] == "user" else "assistant", "content": str(h["message"])})

        if img_b64:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": message or "(이미지를 첨부했습니다)"},
                    {"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{img_b64}"}},
                ],
            })
        else:
            messages.append({"role": "user", "content": message})

        ai_reply = groq.chat(messages)

    seq += 1
    sheets.append_chat(user["name"], user["studentId"], seq, "assistant", ai_reply, "", problem_id, level)
    st.rerun()


# ---------------------------------------------------------------------------
# 3. 메인 화면
# ---------------------------------------------------------------------------
def render_main():
    user = st.session_state.user

    st.title("🧲 물리 문제 힌트 제공 시스템")

    st.subheader("사용자 정보")
    st.table({"이름": [user["name"]], "학번": [user["studentId"]], "수준": [f"Lv.{user['level']}"]})

    if st.session_state.active_problem is None:
        with st.spinner("문제를 불러오는 중..."):
            data = sheets.get_current_problem(user["name"], user["studentId"])
        st.session_state.user["level"] = data["level"]
        st.session_state.active_problem = data["problem"]

    active_problem = st.session_state.active_problem

    st.subheader("제시 문제")

    # 문제 히스토리
    history_list = sheets.get_problem_history(user["name"], user["studentId"])
    if history_list:
        options = [f"{h['level']}|{h['id']}" for h in history_list]
        labels = {f"{h['level']}|{h['id']}": f"Lv.{h['level']} - {h['title']}" for h in history_list}
        active_key = f"{active_problem['level']}|{active_problem['id']}" if active_problem else None
        default_idx = options.index(active_key) if active_key in options else 0
        picked = st.selectbox(
            "문제 히스토리 (이전에 푼 문제 다시 보기 / 다시 풀기)",
            options=options, format_func=lambda k: labels[k], index=default_idx,
        )
        if picked != active_key:
            plevel, pid = picked.split("|")
            preview = sheets.get_problem_by_id(user["name"], user["studentId"], plevel, pid)
            st.session_state.preview_mode = True
            st.session_state.preview_problem = preview
        elif st.session_state.preview_mode and picked == active_key:
            st.session_state.preview_mode = False
            st.session_state.preview_problem = None

    if active_problem is None:
        st.info("해당 수준에 등록된 문제가 없습니다. 관리자에게 문의해주세요.")
    else:
        with st.container(border=True):
            if st.session_state.preview_mode and st.session_state.preview_problem:
                render_problem_box(st.session_state.preview_problem, is_preview=True)
            else:
                render_problem_box(active_problem, is_preview=False)

    st.subheader("학습 도우미와 대화하기")
    tab1, tab2 = st.tabs(["현재 대화", "대화 히스토리 전체"])
    with tab1:
        render_chat(active_problem)
    with tab2:
        full_history = sheets.get_chat_history(user["name"], user["studentId"])
        if not full_history:
            st.caption("아직 대화 기록이 없습니다.")
        else:
            groups, order = {}, []
            for h in full_history:
                key = f"{h.get('level','')}|{h.get('problemId','')}"
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(h)
            for key in order:
                level_part, pid_part = key.split("|")
                label = f"Lv.{level_part} · 문제 {pid_part}" if pid_part else "문제 미연결 대화"
                with st.expander(label):
                    for h in groups[key]:
                        role_label = "학생" if h["role"] == "user" else "AI 선생님"
                        st.markdown(f"**{role_label}** · {h['time']}")
                        st.markdown(latex_for_markdown(h["message"]))
                        if h.get("imageFile"):
                            st.caption(f"📎 {h['imageFile']}")
                        st.divider()

    st.caption("본 시스템은 교육 목적의 학습 지원 도구입니다.")


# ---------------------------------------------------------------------------
# 라우팅
# ---------------------------------------------------------------------------
if st.session_state.stage == "login":
    render_login()
elif st.session_state.stage == "survey":
    render_survey()
else:
    render_main()
