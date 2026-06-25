"""
AI 臨床訓練 PDF Session 比對工具 (Streamlit 版)

部署到 Streamlit Community Cloud：
1. 把 streamlit_app.py 與 requirements.txt 放到一個 GitHub repo
2. 到 share.streamlit.io 用 GitHub 登入，選這個 repo
3. Main file 填 streamlit_app.py，按 Deploy
4. （選用）密碼保護：在 App settings → Secrets 加上一行
       APP_PASSWORD = "你的密碼"
"""

from __future__ import annotations

import difflib
import io
import logging
import re
import unicodedata
from typing import Any

import pdfplumber
import streamlit as st

# ---------------------------------------------------------------------------
# 區段標題（依你的 PDF 內容調整這三行即可）
# ---------------------------------------------------------------------------
SECTION_ANSWERS = "本組各 Session 作答"
SECTION_AI = "各 Session 的 AI 分析"
SECTION_TEACHER = "教師參考答案"


# ---------------------------------------------------------------------------
# 文字抽取與切割
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    # NFKC：把部首形 / 全形相容字折疊成正常字，避免兩份 PDF 編碼不同造成假差異
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"https://clinicalai[^\n]+", "", text)
    text = re.sub(r"^\d{4}/\d{1,2}/\d{1,2} .+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_text(content: bytes) -> str:
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    chunks: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return normalize_text("\n".join(chunks))


def section_between(text: str, start_marker: str, end_markers: list[str]) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end_positions = [text.find(marker, start) for marker in end_markers]
    end_positions = [pos for pos in end_positions if pos >= 0]
    end = min(end_positions) if end_positions else len(text)
    return text[start:end].strip()


def split_sessions(section: str, style: str) -> dict[str, dict[str, str]]:
    if not section:
        return {}
    if style == "answers":
        pattern = re.compile(r"^Session\s+(\d+)\s*[·・]\s*([^\n]*)", re.MULTILINE)
    else:
        pattern = re.compile(r"^【Session\s+(\d+)】\s*$", re.MULTILINE)
    matches = list(pattern.finditer(section))
    sessions: dict[str, dict[str, str]] = {}
    for index, match in enumerate(matches):
        session_no = match.group(1)
        title = (
            match.group(2).strip()
            if style == "answers" and match.lastindex and match.lastindex >= 2
            else ""
        )
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        body = section[body_start:body_end].strip()
        sessions[session_no] = {"title": title, "body": body}
    return sessions


# ---------------------------------------------------------------------------
# 比對
# ---------------------------------------------------------------------------
def build_line_diff(a: str, b: str) -> tuple[list[dict[str, str]], list[dict[str, str]], bool]:
    a_lines = [line.rstrip() for line in a.splitlines() if line.strip()]
    b_lines = [line.rstrip() for line in b.splitlines() if line.strip()]
    matcher = difflib.SequenceMatcher(a=a_lines, b=b_lines)
    left: list[dict[str, str]] = []
    right: list[dict[str, str]] = []
    changed = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            left.extend({"kind": "same", "text": line} for line in a_lines[i1:i2])
            right.extend({"kind": "same", "text": line} for line in b_lines[j1:j2])
        elif tag == "delete":
            changed = True
            left.extend({"kind": "del", "text": line} for line in a_lines[i1:i2])
        elif tag == "insert":
            changed = True
            right.extend({"kind": "add", "text": line} for line in b_lines[j1:j2])
        elif tag == "replace":
            changed = True
            left.extend({"kind": "del", "text": line} for line in a_lines[i1:i2])
            right.extend({"kind": "add", "text": line} for line in b_lines[j1:j2])
    return left, right, changed


def compare_section(
    a_sessions: dict[str, dict[str, str]],
    b_sessions: dict[str, dict[str, str]],
    raw_a: str = "",
    raw_b: str = "",
) -> list[dict[str, Any]]:
    # 防呆：兩邊都切不出 Session，就整段對整段比，避免顯示空白
    if not a_sessions and not b_sessions and (raw_a or raw_b):
        left, right, changed = build_line_diff(raw_a, raw_b)
        return [{"session": "（整段，未切出 Session）", "title": "", "left": left, "right": right, "changed": changed}]

    order = sorted(set(a_sessions) | set(b_sessions), key=lambda v: int(v) if v.isdigit() else 0)
    rows: list[dict[str, Any]] = []
    for session_no in order:
        a_item = a_sessions.get(session_no, {"title": "", "body": ""})
        b_item = b_sessions.get(session_no, {"title": "", "body": ""})
        left, right, changed = build_line_diff(a_item["body"], b_item["body"])
        rows.append({
            "session": session_no,
            "title": a_item["title"] or b_item["title"],
            "left": left,
            "right": right,
            "changed": changed or a_item["title"] != b_item["title"],
        })
    return rows


def analyze(content_a: bytes, content_b: bytes) -> dict[str, Any]:
    text_a = extract_pdf_text(content_a)
    text_b = extract_pdf_text(content_b)

    answers_a = section_between(text_a, SECTION_ANSWERS, [SECTION_AI])
    answers_b = section_between(text_b, SECTION_ANSWERS, [SECTION_AI])
    ai_a = section_between(text_a, SECTION_AI, [SECTION_TEACHER])
    ai_b = section_between(text_b, SECTION_AI, [SECTION_TEACHER])

    answer_rows = compare_section(
        split_sessions(answers_a, "answers"), split_sessions(answers_b, "answers"), answers_a, answers_b
    )
    ai_rows = compare_section(
        split_sessions(ai_a, "ai"), split_sessions(ai_b, "ai"), ai_a, ai_b
    )
    return {
        "answers": answer_rows,
        "ai": ai_rows,
        "raw": {"answers": (answers_a, answers_b), "ai": (ai_a, ai_b)},
    }


# ---------------------------------------------------------------------------
# 顯示
# ---------------------------------------------------------------------------
def esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render_diff_html(lines: list[dict[str, str]]) -> str:
    if not lines:
        return "<div style='color:#888;padding:8px'>（空白）</div>"
    colors = {"add": "#e7f6ec", "del": "#fdeceb", "same": "transparent"}
    out = []
    for row in lines:
        bg = colors.get(row["kind"], "transparent")
        out.append(
            f"<div style='background:{bg};padding:2px 6px;border-radius:3px;"
            f"white-space:pre-wrap;word-break:break-word;line-height:1.6'>{esc(row['text'])}</div>"
        )
    return "".join(out)


def show_section(title: str, rows: list[dict[str, Any]], key_prefix: str) -> None:
    changed = sum(1 for r in rows if r["changed"])
    st.subheader(f"{title}")
    st.caption(f"共 {len(rows)} 個 Session，其中 {changed} 個有差異")

    only_diff = st.toggle("只看有差異的 Session", value=True, key=f"{key_prefix}_toggle")
    shown = [r for r in rows if r["changed"]] if only_diff else rows

    if not shown:
        st.info("沒有可顯示的 Session（可能格式切不出，或全部相同）。")
        return

    for r in shown:
        tag = "🔴 有差異" if r["changed"] else "🟢 相同"
        head = f"Session {r['session']}"
        if r["title"]:
            head += f"・{r['title']}"
        with st.expander(f"{head}　{tag}", expanded=r["changed"]):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**PDF A**")
                st.markdown(render_diff_html(r["left"]), unsafe_allow_html=True)
            with c2:
                st.markdown("**PDF B**")
                st.markdown(render_diff_html(r["right"]), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 密碼保護（選用）
# ---------------------------------------------------------------------------
def check_password() -> bool:
    pw = st.secrets.get("APP_PASSWORD", None) if hasattr(st, "secrets") else None
    if not pw:
        return True  # 沒設密碼就直接放行
    if st.session_state.get("authed"):
        return True
    entered = st.text_input("請輸入密碼", type="password")
    if entered == "":
        st.stop()
    if entered == pw:
        st.session_state["authed"] = True
        st.rerun()
    else:
        st.error("密碼錯誤")
        st.stop()
    return False


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="AI 臨床訓練 PDF Session 比較", layout="wide")
    st.title("AI 臨床訓練 PDF Session 比較")
    st.write("上傳兩份紀錄 PDF，自動比較「本組各 Session 作答」與「各 Session 的 AI 分析」的差異。")

    check_password()

    col_a, col_b = st.columns(2)
    with col_a:
        file_a = st.file_uploader("PDF A", type="pdf", key="a")
    with col_b:
        file_b = st.file_uploader("PDF B", type="pdf", key="b")

    if not (file_a and file_b):
        st.info("請選擇兩份 PDF。")
        return

    if st.button("開始比較", type="primary"):
        with st.spinner("正在抽取 PDF 文字並比對…"):
            try:
                result = analyze(file_a.getvalue(), file_b.getvalue())
            except Exception as exc:  # noqa: BLE001
                st.error(f"處理失敗：{exc}")
                return
        st.session_state["result"] = result

    result = st.session_state.get("result")
    if not result:
        return

    tab1, tab2, tab3 = st.tabs(["本組作答", "AI 分析", "原始抽出文字"])
    with tab1:
        show_section("本組各 Session 作答", result["answers"], "ans")
    with tab2:
        show_section("各 Session 的 AI 分析", result["ai"], "ai")
    with tab3:
        st.caption("用來檢查切割是否正確；若這裡是空白，代表標題沒抓到。")
        st.text_area("作答區段 A", result["raw"]["answers"][0], height=160)
        st.text_area("作答區段 B", result["raw"]["answers"][1], height=160)
        st.text_area("AI 區段 A", result["raw"]["ai"][0], height=160)
        st.text_area("AI 區段 B", result["raw"]["ai"][1], height=160)


if __name__ == "__main__":
    main()
