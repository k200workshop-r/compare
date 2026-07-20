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
import tempfile
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pdfplumber
import streamlit as st

# 產生總比較 PDF 用的繁中字型（雲端首次產生時自動下載並快取內嵌）
FONT_URL = "https://raw.githubusercontent.com/googlefonts/noto-cjk/main/Sans/Variable/TTF/Subset/NotoSansTC-VF.ttf"
try:
    LOCAL_FONT = Path(__file__).parent / "NotoSansTC-VF.ttf"
except NameError:  # 少數執行環境沒有 __file__
    LOCAL_FONT = Path("NotoSansTC-VF.ttf")

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


def split_depts(raw: str) -> list[str]:
    return [d.strip() for d in re.split(r"[、,，/／;；\s]+", raw) if d.strip()]


def extract_meta(text: str) -> dict[str, Any]:
    """擷取組別名稱與組成科別。"""
    name = ""
    m = re.search(r"紀錄\s*\n\s*([^\n]+)", text)
    if m:
        name = m.group(1).strip()
    depts_raw = ""
    m2 = re.search(r"組成科別[：:]\s*([^\n]+)", text)
    if m2:
        depts_raw = m2.group(1).strip()
    return {"name": name, "depts_raw": depts_raw, "depts": split_depts(depts_raw)}


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

    metas = [extract_meta(t) for t in texts]
    dept_bodies = ["\n".join(mt["depts"]) for mt in metas]
    dept_per_file = [{"科部": {"title": "", "body": b}} if b else {} for b in dept_bodies]
    dept_rows = build_multi_rows(dept_per_file, dept_bodies)

    ans_raw = [section_between(t, SECTION_ANSWERS, [SECTION_AI]) for t in texts]
    ai_raw = [section_between(t, SECTION_AI, [SECTION_TEACHER]) for t in texts]

    ans_rows = build_multi_rows([split_sessions(r, "answers") for r in ans_raw], ans_raw)
    ai_rows = build_multi_rows([split_sessions(r, "ai") for r in ai_raw], ai_raw)

    return {
        "names": names,
        "meta": metas,
        "depts": dept_rows,
        "answers": ans_rows,
        "ai": ai_rows,
        "raw": {"answers": ans_raw, "ai": ai_raw},
    }


# ---------------------------------------------------------------------------
# 顯示
# ---------------------------------------------------------------------------
def esc(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_col_html(col: dict[str, Any], tint: str, missing_text: str = "（此檔無此 Session）") -> str:
    if col["missing"]:
        return f"<div style='color:#b00;padding:8px;font-style:italic'>{esc(missing_text)}</div>"
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
# 產生「總比較 PDF」
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def register_report_font() -> str:
    """回傳可用的字型名稱。優先內嵌 TTF（任何檢視器都能顯示中文），失敗才退回 CID。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    # 1) repo 內若有附字型檔就直接用
    try:
        if LOCAL_FONT.exists():
            pdfmetrics.registerFont(TTFont("TCReport", str(LOCAL_FONT)))
            return "TCReport"
    except Exception:  # noqa: BLE001
        pass
    # 2) 從網路下載並內嵌（雲端可連外）
    try:
        data = urllib.request.urlopen(FONT_URL, timeout=40).read()
        tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
        tmp.write(data)
        tmp.close()
        pdfmetrics.registerFont(TTFont("TCReport", tmp.name))
        return "TCReport"
    except Exception:  # noqa: BLE001
        pass
    # 3) 退回內建 CID 字型（Chrome / Acrobat / 預覽程式可顯示）
    pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))
    return "MSung-Light"


def build_pdf_report(result: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    font = register_report_font()
    names = result["names"]

    body = ParagraphStyle("body", fontName=font, fontSize=9, leading=13)
    name_s = ParagraphStyle("name", fontName=font, fontSize=9, leading=13)
    h1 = ParagraphStyle("h1", fontName=font, fontSize=16, leading=20, spaceAfter=4)
    h2 = ParagraphStyle("h2", fontName=font, fontSize=13, leading=17, spaceBefore=12,
                        spaceAfter=4, textColor=colors.HexColor("#1d6f68"))
    sess = ParagraphStyle("sess", fontName=font, fontSize=10.5, leading=14, spaceBefore=7, spaceAfter=2)
    small = ParagraphStyle("small", fontName=font, fontSize=8, leading=12, textColor=colors.HexColor("#666666"))

    story: list[Any] = []
    story.append(Paragraph("AI 臨床訓練 PDF Session 總比較", h1))
    story.append(Paragraph(f"比較檔案（共 {len(names)} 份）：" + "　｜　".join(esc(n) for n in names), small))
    story.append(Paragraph("產生時間：" + datetime.now().strftime("%Y-%m-%d %H:%M"), small))
    a_ch = sum(1 for r in result["answers"] if r["changed"])
    i_ch = sum(1 for r in result["ai"] if r["changed"])
    story.append(Paragraph(
        f"本組作答：{len(result['answers'])} 個 Session，{a_ch} 個有差異　；　"
        f"AI 分析：{len(result['ai'])} 個 Session，{i_ch} 個有差異。", body))
    story.append(Paragraph("標色 = 該行並非所有檔案都有（即分歧處）。", small))

    def section(title: str, rows: list[dict[str, Any]], missing_text: str = "（此檔無此 Session）") -> None:
        story.append(Paragraph(title, h2))
        if not rows:
            story.append(Paragraph("（無資料）", body))
            return
        for r in rows:
            badge = "有差異" if r["changed"] else "相同"
            color = "#b03a2e" if r["changed"] else "#1e7d4f"
            label = f"Session {r['session']}" if str(r["session"]).isdigit() else str(r["session"])
            head = label + (f"・{esc(r['title'])}" if r["title"] else "")
            story.append(Paragraph(f'{head}　<font color="{color}">[{badge}]</font>', sess))

            data: list[list[Any]] = []
            for name, col in zip(names, r["cols"]):
                if col["missing"]:
                    content = Paragraph(f'<font color="#b03a2e"><i>{esc(missing_text)}</i></font>', body)
                elif not col["lines"]:
                    content = Paragraph("（空白）", body)
                else:
                    parts = []
                    for ln in col["lines"]:
                        t = esc(ln["text"])
                        parts.append(f'<font backColor="#fff3df">{t}</font>' if ln["kind"] == "diff" else t)
                    content = Paragraph("<br/>".join(parts), body)
                data.append([Paragraph(f"<b>{esc(name)}</b>", name_s), content])

            tbl = Table(data, colWidths=[38 * mm, None])
            tbl.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9dde4")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f6f7f9")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(tbl)

    if result.get("depts"):
        section("一、組員科部", result["depts"], missing_text="（未填組成科別）")
        section("二、本組各 Session 作答", result["answers"])
        section("三、各 Session 的 AI 分析", result["ai"])
    else:
        section("一、本組各 Session 作答", result["answers"])
        section("二、各 Session 的 AI 分析", result["ai"])

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
        leftMargin=14 * mm, rightMargin=14 * mm, title="AI臨床訓練 Session 總比較",
    ).build(story)
    return buf.getvalue()


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
                st.session_state.pop("pdf_bytes", None)  # 新比較→清掉舊 PDF
            except Exception as exc:  # noqa: BLE001
                st.error(f"處理失敗：{exc}")
                return

    result = st.session_state.get("result")
    if not result:
        return

    names = result["names"]

    # 組員科部比較
    st.subheader("組員科部")
    dept_rows = result.get("depts") or []
    if not dept_rows:
        st.info("上傳的 PDF 未含「組成科別」欄位，無法比較科部。")
    else:
        st.caption("標色 = 該科別並非所有組別都有")
        row = dept_rows[0]
        dcols = st.columns(len(names))
        for i, (col_ui, name) in enumerate(zip(dcols, names)):
            with col_ui:
                gname = result["meta"][i]["name"] if i < len(result.get("meta", [])) else ""
                label = esc(name) + (f" <span style='color:#888'>（{esc(gname)}）</span>" if gname else "")
                st.markdown(
                    f"<div style='font-weight:700;font-size:13px;padding-bottom:4px'>{label}</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(render_col_html(row["cols"][i], COL_TINTS[i % len(COL_TINTS)],
                                            missing_text="（未填組成科別）"),
                            unsafe_allow_html=True)

    # 產生 / 下載總比較 PDF
    st.divider()
    cA, cB = st.columns([1, 2])
    with cA:
        if st.button("產生總比較 PDF", key="mkpdf"):
            with st.spinner("正在產生 PDF（首次會下載中文字型，約需數秒）…"):
                try:
                    st.session_state["pdf_bytes"] = build_pdf_report(result)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"PDF 產生失敗：{exc}")
    with cB:
        pdf_bytes = st.session_state.get("pdf_bytes")
        if pdf_bytes:
            fname = "AI臨床訓練_Session總比較_" + datetime.now().strftime("%Y%m%d_%H%M") + ".pdf"
            st.download_button("下載總比較 PDF", data=pdf_bytes, file_name=fname,
                               mime="application/pdf", key="dlpdf")
    st.divider()

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
