"""
AI 臨床訓練 PDF Session 比對工具 (Streamlit 版・支援一次比較 2~5 份)

部署到 Streamlit Community Cloud：
1. 把 streamlit_app.py 與 requirements.txt 放到一個 GitHub repo
2. 到 share.streamlit.io 用 GitHub 登入，選這個 repo
3. Main file 填 streamlit_app.py，按 Deploy
4. （選用）密碼保護：App settings → Secrets 加一行
       APP_PASSWORD = "你的密碼"

比對邏輯：
- 每個 Session 一列，所有上傳的檔案並排成多欄
- 只要「某一行不是所有檔案都有」，該行就會被標色（代表這裡有分歧）
- 全部檔案內容一致的 Session 標「相同」，否則標「有差異」
"""

from __future__ import annotations

import difflib  # noqa: F401  (保留，之後若要做兩兩 diff 可用)
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

MAX_FILES = 5

# 欄位配色（依檔案順序），標色時用對應底色
COL_TINTS = ["#fff3df", "#e7f0ff", "#e9f7ea", "#fdeceb", "#f3e9fb"]


# ---------------------------------------------------------------------------
# 文字抽取與切割
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    # NFKC：把部首形 / 全形相容字折疊成正常字，避免各檔編碼不同造成假差異
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
        sessions[session_no] = {"title": title, "body": section[body_start:body_end].strip()}
    return sessions


# ---------------------------------------------------------------------------
# 多檔比對核心
# ---------------------------------------------------------------------------
def body_lines(body: str) -> list[str]:
    return [ln.rstrip() for ln in body.splitlines() if ln.strip()]


def build_multi_rows(
    per_file_sessions: list[dict[str, dict[str, str]]],
    raw_sections: list[str],
) -> list[dict[str, Any]]:
    """per_file_sessions 依上傳檔案順序；回傳每個 Session 一列。"""
    # 防呆：所有檔都切不出 Session → 整段當成單一 Session 比
    if all(not s for s in per_file_sessions) and any(raw_sections):
        per_file_sessions = [{"整段": {"title": "（未切出 Session）", "body": raw}} for raw in raw_sections]

    all_nos: set[str] = set()
    for s in per_file_sessions:
        all_nos |= set(s.keys())
    order = sorted(all_nos, key=lambda v: int(v) if v.isdigit() else 0)

    rows: list[dict[str, Any]] = []
    for no in order:
        items = [s.get(no) for s in per_file_sessions]  # None 代表該檔沒有此 Session
        bodies = [(it["body"] if it else None) for it in items]
        title = next((it["title"] for it in items if it and it["title"]), "")

        # 共識行 = 所有「有此 Session」的檔案都出現的行
        present_line_sets = [set(body_lines(b)) for b in bodies if b is not None]
        common = set.intersection(*present_line_sets) if present_line_sets else set()

        # changed = 只要有檔缺此 Session，或內容不完全一致
        norm_bodies = [tuple(body_lines(b)) if b is not None else None for b in bodies]
        changed = len(set(norm_bodies)) > 1

        cols: list[dict[str, Any]] = []
        for b in bodies:
            if b is None:
                cols.append({"missing": True, "lines": []})
                continue
            lines = [
                {"kind": "same" if ln in common else "diff", "text": ln}
                for ln in body_lines(b)
            ]
            cols.append({"missing": False, "lines": lines})
        rows.append({"session": no, "title": title, "changed": changed, "cols": cols})
    return rows


def analyze_multi(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    names = [n for n, _ in files]
    texts = [extract_pdf_text(c) for _, c in files]

    ans_raw = [section_between(t, SECTION_ANSWERS, [SECTION_AI]) for t in texts]
    ai_raw = [section_between(t, SECTION_AI, [SECTION_TEACHER]) for t in texts]

    ans_rows = build_multi_rows([split_sessions(r, "answers") for r in ans_raw], ans_raw)
    ai_rows = build_multi_rows([split_sessions(r, "ai") for r in ai_raw], ai_raw)

    return {
        "names": names,
        "answers": ans_rows,
        "ai": ai_rows,
        "raw": {"answers": ans_raw, "ai": ai_raw},
    }


# ---------------------------------------------------------------------------
# 顯示
# ---------------------------------------------------------------------------
def esc(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_col_html(col: dict[str, Any], tint: str) -> str:
    if col["missing"]:
        return "<div style='color:#b00;padding:8px;font-style:italic'>（此檔無此 Session）</div>"
    if not col["lines"]:
        return "<div style='color:#888;padding:8px'>（空白）</div>"
    out = []
    for row in col["lines"]:
        bg = tint if row["kind"] == "diff" else "transparent"
        out.append(
            f"<div style='background:{bg};padding:2px 6px;border-radius:3px;"
            f"white-space:pre-wrap;word-break:break-word;line-height:1.6'>{esc(row['text'])}</div>"
        )
    return "".join(out)


def show_section(title: str, rows: list[dict[str, Any]], names: list[str], key_prefix: str) -> None:
    changed = sum(1 for r in rows if r["changed"])
    st.subheader(title)
    st.caption(f"共 {len(rows)} 個 Session，其中 {changed} 個有差異　·　標色 = 該行並非所有檔案都有")

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
            columns = st.columns(len(names))
            for i, (col_ui, name) in enumerate(zip(columns, names)):
                with col_ui:
                    st.markdown(
                        f"<div style='font-weight:700;font-size:13px;padding-bottom:4px'>"
                        f"{esc(name)}</div>",
                        unsafe_allow_html=True,
                    )
                    tint = COL_TINTS[i % len(COL_TINTS)]
                    st.markdown(render_col_html(r["cols"][i], tint), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 密碼保護（選用）
# ---------------------------------------------------------------------------
def check_password() -> None:
    pw = st.secrets.get("APP_PASSWORD", None) if hasattr(st, "secrets") else None
    if not pw or st.session_state.get("authed"):
        return
    entered = st.text_input("請輸入密碼", type="password")
    if entered == "":
        st.stop()
    if entered == pw:
        st.session_state["authed"] = True
        st.rerun()
    else:
        st.error("密碼錯誤")
        st.stop()


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="AI 臨床訓練 PDF Session 比較", layout="wide")
    st.title("AI 臨床訓練 PDF Session 比較")
    st.write(f"一次上傳 2~{MAX_FILES} 份紀錄 PDF，並排比較「本組各 Session 作答」與「各 Session 的 AI 分析」。")

    check_password()

    uploads = st.file_uploader(
        f"上傳 PDF（可一次拖曳多份，最多 {MAX_FILES} 份）",
        type="pdf",
        accept_multiple_files=True,
    )

    if not uploads:
        st.info("請至少選擇 2 份 PDF。")
        return
    if len(uploads) < 2:
        st.warning("至少要 2 份才能比較。")
        return
    if len(uploads) > MAX_FILES:
        st.warning(f"最多 {MAX_FILES} 份，只取前 {MAX_FILES} 份：{[u.name for u in uploads[:MAX_FILES]]}")
        uploads = uploads[:MAX_FILES]

    st.caption("將比較： " + " ｜ ".join(u.name for u in uploads))

    if st.button("開始比較", type="primary"):
        with st.spinner("正在抽取 PDF 文字並比對…"):
            try:
                files = [(u.name, u.getvalue()) for u in uploads]
                st.session_state["result"] = analyze_multi(files)
            except Exception as exc:  # noqa: BLE001
                st.error(f"處理失敗：{exc}")
                return

    result = st.session_state.get("result")
    if not result:
        return

    names = result["names"]
    tab1, tab2, tab3 = st.tabs(["本組作答", "AI 分析", "原始抽出文字"])
    with tab1:
        show_section("本組各 Session 作答", result["answers"], names, "ans")
    with tab2:
        show_section("各 Session 的 AI 分析", result["ai"], names, "ai")
    with tab3:
        st.caption("用來檢查切割是否正確；若某欄空白，代表該檔標題沒抓到。")
        for i, name in enumerate(names):
            st.markdown(f"**{name}**")
            c1, c2 = st.columns(2)
            with c1:
                st.text_area(f"作答區段 · {name}", result["raw"]["answers"][i], height=140, key=f"raw_ans_{i}")
            with c2:
                st.text_area(f"AI 區段 · {name}", result["raw"]["ai"][i], height=140, key=f"raw_ai_{i}")


if __name__ == "__main__":
    main()
