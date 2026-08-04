# 물리 문제 힌트 제공 앱 (Streamlit 버전)

Google Apps Script + HTML로 만들었던 프로토타입을 **Python + Streamlit**으로 이식한 버전입니다.
구글 시트 연동은 `gspread`(서비스 계정 인증), AI 힌트/채점은 `Groq API`를 사용합니다.

## ⚠️ 먼저 하실 일: API 키 재발급

이번에 주신 메시지에 Groq API 키와 GitHub PAT가 평문으로 포함되어 있었습니다. 대화 로그 등을 통해
유출될 수 있으므로 **아래 작업을 지금 바로 해주세요.**

1. https://console.groq.com 에서 기존 키를 폐기(revoke)하고 새 키를 발급
2. https://github.com/settings/tokens 에서 기존 PAT를 Delete 하고 새로 발급 (필요한 최소 권한만 부여)
3. 새 키는 코드에 절대 직접 적지 말고, 아래 `secrets.toml`에만 저장

## 1. 폴더 구조

```
physics_hint_app/
├── app.py                 # 메인 Streamlit 앱
├── sheets_client.py        # 구글 시트 CRUD 로직
├── groq_client.py          # Groq API 호출 (모델 자동 폴백)
├── utils.py                 # LaTeX 변환, 이미지 저장 헬퍼
├── requirements.txt
├── problem_images/          # 관리자가 등록한 문제 이미지 (시트의 "이미지파일" 값과 파일명 일치)
├── uploads/                  # 학생이 업로드한 사진 (자동 생성, 세션 중 임시 저장)
└── .streamlit/
    └── secrets_example.toml # secrets.toml 작성용 템플릿
```

## 2. 구글 시트 준비

기존에 쓰시던 스프레드시트(ID: `1KidghOU2KTJOpxRtoBcOWGpFiEs0cm8WRV-B9TxGHPs`)를 그대로 사용합니다.
시트 이름과 헤더 구조는 기존 문서와 동일해야 합니다.

| 시트명 | 필드 |
|---|---|
| 계정 | 이름, 학번, 등록일시, 최근접속일시, 수준, 설문완료여부(Y/N), 현재문제ID |
| 문제_Lv0~Lv3 | 문제번호, 소문항, 제목, 문제내용, 이미지파일, 보기1~5, 정답번호, 힌트, 활성여부 |
| 설문(문항) | 문항번호, 문항내용, 사용여부(Y/N) |
| 설문(기준) | 수준, 최소평균점수, 최대평균점수, 수준명 |
| 설문(기록) | 이름, 학번, 문항번호, 응답점수, 응답일시 |
| 대화내역 | 이름, 학번, 대화순번, 발화자, 메시지내용, 첨부이미지URL(파일명), 타임스탬프, 연결문제ID, 문제레벨 |

각 시트 1행은 헤더로 사용되므로 데이터는 2행부터 입력하세요.

## 3. 구글 서비스 계정 만들기 (Apps Script 대신 Python에서 시트 접근)

Apps Script는 스프레드시트에 바인딩되어 자동으로 권한을 갖지만, Streamlit(Python)에서는
**서비스 계정**을 만들어 시트를 공유해줘야 합니다.

1. https://console.cloud.google.com 에서 프로젝트 생성 (또는 기존 프로젝트 사용)
2. "API 및 서비스 > 라이브러리"에서 **Google Sheets API**, **Google Drive API** 활성화
3. "API 및 서비스 > 사용자 인증 정보 > 사용자 인증 정보 만들기 > 서비스 계정" 생성
4. 생성된 서비스 계정 > "키" 탭 > "키 추가 > JSON" 으로 키 파일 다운로드
5. 다운로드한 JSON의 `client_email` 값을 복사해서, 대상 구글 시트의 "공유" 설정에 **편집자**로 추가
6. JSON 파일 내용을 `.streamlit/secrets.toml`의 `[gcp_service_account]` 섹션에 옮겨 적기

## 4. secrets.toml 작성

`.streamlit/secrets_example.toml`을 복사해서 `.streamlit/secrets.toml`로 저장한 뒤 실제 값을 채워넣으세요.
(Streamlit Community Cloud에 배포하는 경우, 앱 설정의 "Secrets" 메뉴에 동일한 내용을 붙여넣으면 됩니다.)

`.gitignore`에 아래 줄을 추가해서 실수로 커밋되지 않도록 하세요.

```
.streamlit/secrets.toml
uploads/
```

## 5. 문제 이미지 등록

Apps Script 버전은 Drive 폴더에서 이미지를 찾았지만, 이번 요청대로 **업로드한 이미지 파일을 그대로 사용**하도록
`problem_images/` 로컬 폴더 방식으로 구현했습니다.

- 관리자가 문제 이미지를 `problem_images/문제1.png` 형태로 앱 폴더에 넣고
- 구글 시트의 "이미지파일" 칸에 `문제1.png` 처럼 **파일명만** 적으면 앱이 자동으로 찾아 표시합니다.
- Streamlit Cloud에 배포할 경우 이 폴더도 함께 git에 커밋해야 이미지가 보입니다.

학생이 채팅/정답 제출 시 업로드하는 사진은 `uploads/` 폴더에 저장되어 AI에게 전달됩니다.
(Streamlit Cloud 등 배포 환경은 재시작 시 로컬 파일이 사라질 수 있는 휘발성 파일시스템이라,
장기 보관이 필요하면 Drive API 업로드 연동을 추가하는 것을 권장합니다.)

## 6. 실행

```bash
cd physics_hint_app
pip install -r requirements.txt
streamlit run app.py
```

## 7. Streamlit Community Cloud 배포

1. 이 폴더를 GitHub 저장소에 push (secrets.toml은 제외)
2. https://share.streamlit.io 에서 새 앱 생성, 저장소/브랜치/`app.py` 지정
3. 앱 설정의 "Secrets"에 `.streamlit/secrets_example.toml`과 동일한 형식으로 실제 값 입력
4. Deploy

## 8. 원본 대비 주요 변경점

- **이미지 저장 방식**: Drive 폴더 조회 → 로컬 `problem_images/` 폴더 + 파일명 매칭 (요청하신 대로)
- **LaTeX 렌더링**: 원본은 유니코드 위/아래첨자로 직접 변환했지만, Streamlit은 `$...$` / `$$...$$` 마크다운을
  기본 지원하므로 `\( \)`, `\[ \]` 구분자를 `$`, `$$`로 바꿔주는 방식으로 단순화했습니다 (`utils.latex_for_markdown`).
- **인증**: Apps Script의 자동 바인딩 권한 대신 Google 서비스 계정 인증(`gspread`)을 사용합니다.
- 그 외 화면 흐름(로그인 → 신규자 설문 → 문제/대화 메인 화면), 시트 9종 구조, 레벨 승강 로직,
  소문항(꼬리문제) 순차 잠금 해제, 문제 히스토리 미리보기 등은 원본 로직을 그대로 이식했습니다.
