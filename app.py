"""DataPilot — Streamlit UI only. All agent logic lives in team.py, all checks in guardrails.py."""
import asyncio
import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import BaseChatMessage
from pydantic import ValidationError

import team as tm
from config import Settings
from guardrails import (
    GuardrailError,
    build_profile,
    check_question,
    check_upload,
    excel_sheets,
    load_table,
    normalise_columns,
    prepare_sandbox,
)

WORK_DIR = Path("sandbox").resolve()
STEPS = ["Ask", "Plan", "Code", "Run", "Review", "Answer"]
STEP_OF = {"user": 0, tm.PLANNER: 1, tm.CODER: 2, tm.EXECUTOR: 3, tm.REVIEWER: 4}
AGENTS = {  # name -> (avatar, colour, label)
    "user": ("👤", "#3B3B3B", "You"),
    tm.PLANNER: ("🧭", "#1F5A7A", "Planner"),
    tm.CODER: ("💻", "#5B3A8C", "Coder"),
    tm.EXECUTOR: ("⚙️", "#4A5A2A", "Executor"),
    tm.REVIEWER: ("🔍", "#7A1F1F", "Reviewer"),
}
BADGE = {"ERROR": "#C62828", "FIXED": "#E0A100", "FEEDBACK": "#E0A100", "APPROVED": "#2E7D32", "STOPPED": "#C62828"}
CODE_RE = re.compile(r"```(?:python|py)?\n(.*?)```", re.DOTALL)

st.set_page_config(page_title="DataPilot", page_icon="🧭", layout="wide")
st.markdown(
    """<style>
    .dp-steps{display:flex;gap:6px;flex-wrap:wrap;margin:.2rem 0 1rem}
    .dp-step{padding:6px 14px;border-radius:999px;border:1px solid #E3D6D4;color:#8A7F7E;font-size:.85rem;background:#fff}
    .dp-step.done{background:#F6ECEA;color:#7A1F1F;border-color:#E3C9C5}
    .dp-step.active{background:#7A1F1F;color:#fff;border-color:#7A1F1F;font-weight:600}
    .dp-badge{display:inline-block;padding:1px 8px;border-radius:6px;color:#fff;font-size:.72rem;font-weight:700;
              letter-spacing:.04em;margin-left:6px;vertical-align:middle}
    .dp-name{font-weight:600}
    .dp-card{border:1px solid #E3D6D4;border-left:5px solid #7A1F1F;border-radius:10px;padding:1rem 1.2rem;margin-bottom:.8rem}
    .dp-card h5{margin:0 0 .3rem;color:#7A1F1F;font-size:.78rem;letter-spacing:.08em}
    </style>""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- state
def ss():
    return st.session_state


if "loop" not in ss():
    ss().loop = asyncio.new_event_loop()  # one loop for the whole session: model clients are bound to it
    ss().update(bundle=None, file_sig=None, team_mode=None, data_path=None, profile=None, df=None,
                log=[], run=None, pending=None, approved_by_user=False, error=None)


def run_async(coro):
    return ss().loop.run_until_complete(coro)


try:
    SETTINGS = Settings()
except ValidationError as e:
    st.error(f"Configuration error: {e.errors()[0]['msg'].removeprefix('Value error, ')}")
    st.stop()


# ----------------------------------------------------------------------- rendering
def badge(text: str) -> str:
    return f"<span class='dp-badge' style='background:{BADGE[text]}'>{text}</span>"


def progress_html(active: int | None, done_upto: int) -> str:
    pills = []
    for i, name in enumerate(STEPS):
        cls = "active" if i == active else ("done" if i <= done_upto else "")
        pills.append(f"<div class='dp-step {cls}'>{i + 1}. {name}</div>")
    return f"<div class='dp-steps'>{''.join(pills)}</div>"


def render_message(m: dict) -> None:
    avatar, colour, label = AGENTS.get(m["source"], ("🤖", "#555", m["source"]))
    with st.chat_message(m["source"] if m["source"] == "user" else "assistant", avatar=avatar):
        head = f"<span class='dp-name' style='color:{colour}'>{label}</span>"
        st.markdown(head + "".join(badge(b) for b in m["badges"]), unsafe_allow_html=True)
        text = m["text"]
        if m["source"] == tm.CODER:
            prose = CODE_RE.sub("", text).strip()
            if prose:
                st.markdown(prose)
            for code in CODE_RE.findall(text):
                with st.expander("analysis.py", expanded=False):
                    st.code(code, language="python")
        elif m["source"] == tm.EXECUTOR:
            summary, _, detail = text.partition("VALIDATION:")
            st.text(summary.strip()[-1500:] or "(no console output)")
            if detail:
                with st.expander("Validation" + (" ✔ passed" if detail.lstrip().startswith("PASSED") else " ✘ failed")):
                    st.text("VALIDATION:" + detail[:4000])
        else:
            st.markdown(text)


def message_badges(msg: BaseChatMessage, state: tm.RunState) -> list[str]:
    text = msg.to_text()
    if msg.source == tm.CODER and text.lstrip().startswith("Fixed:"):
        return ["FIXED"]
    if msg.source == tm.EXECUTOR and (state.last_exit_code != 0 or state.validation_error):
        return ["ERROR"]
    if msg.source == tm.REVIEWER:
        return ["APPROVED"] if tm.is_approved(msg, state) else (["FEEDBACK"] if "FEEDBACK" in text else [])
    if msg.source == tm.PLANNER and "CANNOT_ANSWER" in text:
        return ["STOPPED"]
    return []


# ------------------------------------------------------------------------- sidebar
def load_upload() -> None:
    with st.sidebar:
        st.markdown("### 📂 Data")
        up = st.file_uploader("Upload sales data", type=["csv", "xlsx"], label_visibility="collapsed")
        if up is None:
            st.caption("Upload a .csv or .xlsx file (≤ 25 MB). Try `sample_data/sales_2026.csv`.")
            return
        data = up.getvalue()
        try:
            ext = check_upload(up.name, len(data), SETTINGS.MAX_FILE_MB)
            sheet = None
            if ext == ".xlsx":
                sheets = excel_sheets(data)
                sheet = st.selectbox("Sheet", sheets) if len(sheets) > 1 else sheets[0]
            sig = (up.name, len(data), sheet)
            if sig != ss().file_sig:
                df_raw = load_table(data, up.name, SETTINGS.MAX_ROWS, sheet)
                df, mapping = normalise_columns(df_raw)
                ss().update(df=df, mapping=mapping, profile=build_profile(df, up.name, sheet), file_sig=sig,
                            data_path=prepare_sandbox(WORK_DIR, df, up.name), log=[], run=None, error=None)
                close_team()
        except GuardrailError as e:
            st.error(str(e))
            ss().file_sig = None
            return
        except Exception as e:  # unreadable file
            st.error(f"Could not read the file: {type(e).__name__}")
            ss().file_sig = None
            return

        p, df = ss().profile, ss().df
        changed = {k: v for k, v in ss().mapping.items() if k != v}
        if changed:
            with st.expander(f"Column names cleaned ({len(changed)})"):
                st.dataframe(pd.DataFrame({"original": changed.keys(), "used as": changed.values()}),
                             hide_index=True, width="stretch")
        c1, c2 = st.columns(2)
        c1.metric("Rows", f"{p.n_rows:,}")
        c2.metric("Columns", len(p.columns))
        if p.date_range:
            st.caption(f"📅 {p.date_range[0]} → {p.date_range[1]}")
        st.dataframe(df.head(20), height=260, width="stretch")

        with st.expander("⚙️ Settings"):
            mode = st.radio("Team mode", ["selector", "roundrobin"],
                            index=["selector", "roundrobin"].index(ss().team_mode or SETTINGS.TEAM_MODE),
                            help="selector = deterministic routing (default); roundrobin = brief baseline")
            if mode != ss().team_mode and ss().team_mode is not None:
                close_team()
            ss().team_mode = mode
            b = ss().bundle
            if b is not None:
                st.markdown("Executor: " + ("🟢 Docker" if b.using_docker else "🟡 Local sandbox"))
            st.caption(f"Models: {SETTINGS.CODER_MODEL} (Coder/Reviewer) · {SETTINGS.PLANNER_MODEL} (Planner/Answer)")


def close_team() -> None:
    if ss().bundle is not None:
        try:
            run_async(tm.close(ss().bundle))
        except Exception:  # already closed / loop issue — nothing to keep
            pass
    ss().update(bundle=None, run=None, log=[], approved_by_user=False)


def get_bundle() -> tm.Bundle:
    if ss().bundle is None:
        settings = SETTINGS.model_copy(update={"TEAM_MODE": ss().team_mode or SETTINGS.TEAM_MODE})
        ss().bundle = run_async(tm.build_team(settings, WORK_DIR))
    return ss().bundle


# ------------------------------------------------------------------------- the run
def run_question(question: str, follow_up: bool, progress, chat) -> None:
    b = get_bundle()
    b.state.self_corrections = b.state.errors = 0
    b.state.result = None
    task = (f"Follow-up question (same data file {ss().data_path}): {question}" if follow_up
            else tm.make_task(question, ss().data_path, ss().profile))
    ss().log.append({"source": "user", "text": question, "badges": []})
    with chat:
        render_message(ss().log[-1])
    progress.markdown(progress_html(0, -1), unsafe_allow_html=True)

    n, tokens, last, stop, done = 0, 0, None, "", 0
    counter = st.empty()
    stream = b.team.run_stream(task=task)
    try:
        while True:
            try:
                item = run_async(stream.__anext__())
            except StopAsyncIteration:
                break
            if isinstance(item, TaskResult):
                stop = item.stop_reason or ""
                continue
            if not isinstance(item, BaseChatMessage):
                continue
            n += 1
            if item.models_usage:
                tokens += item.models_usage.prompt_tokens + item.models_usage.completion_tokens
            if item.source == "user":
                continue  # the task itself — already shown as the question
            last = item
            step = STEP_OF.get(item.source, 0)
            done = max(done, step)
            progress.markdown(progress_html(step, done - 1), unsafe_allow_html=True)
            entry = {"source": item.source, "text": item.to_text(), "badges": message_badges(item, b.state)}
            ss().log.append(entry)
            with chat:
                render_message(entry)
            counter.caption(f"Self-corrections: {b.state.self_corrections} · messages {n}/{SETTINGS.MAX_MESSAGES}")
    except Exception as e:  # LLM/API failure — never show a stack trace
        ss().error = f"The AI service returned an error: {type(e).__name__}: {str(e)[:200]}"
        run_async(b.team.reset())
        return

    run = {"question": question, "messages": n, "tokens": tokens, "stop": stop, "approved": False,
           "self_corrections": b.state.self_corrections, "transcript": list(ss().log)}
    try:
        (WORK_DIR / "team_state.json").write_text(json.dumps(run_async(b.team.save_state()), default=str), "utf-8")
    except Exception:  # state persistence is best-effort; the session object still holds the context
        pass

    if tm.is_approved(last, b.state):
        progress.markdown(progress_html(5, 4), unsafe_allow_html=True)
        with st.spinner("Writing the answer and re-running analysis.py to check reproducibility…"):
            answer, bad, t = run_async(tm.write_answer(b, question))
            repro_ok, repro_msg = run_async(tm.check_reproducible(SETTINGS, WORK_DIR, b.state.result))
        res = b.state.result
        charts = [(c, (WORK_DIR / c.path).read_bytes()) for c in res.charts if (WORK_DIR / c.path).exists()]
        run.update(approved=True, answer=answer, ungrounded=bad, tokens=tokens + t, result=res, charts=charts,
                   code=(WORK_DIR / "analysis.py").read_text(encoding="utf-8"), repro=(repro_ok, repro_msg))
        progress.markdown(progress_html(None, 5), unsafe_allow_html=True)
    elif last is not None and last.source == tm.PLANNER and "CANNOT_ANSWER" in last.to_text():
        run["cannot_answer"] = last.to_text().split("CANNOT_ANSWER:", 1)[-1].strip()
    else:
        errs = [m for m in ss().log if m["source"] == tm.EXECUTOR and "ERROR" in m["badges"]]
        run["last_error"] = errs[-1]["text"][-800:] if errs else (last.to_text()[-800:] if last else "")
    ss().run = run
    ss().approved_by_user = False


# ------------------------------------------------------------------------- results
def show_results(run: dict) -> None:
    if run.get("cannot_answer") is not None:
        st.warning(f"**This question can't be answered from the uploaded columns.** {run['cannot_answer']}")
        return
    if not run["approved"]:
        st.error(f"**Stopped at safety limit** ({run['messages']}/{SETTINGS.MAX_MESSAGES} messages) without approval.")
        if run.get("last_error"):
            with st.expander("Last error"):
                st.text(run["last_error"])
        if st.button("🔁 Try again", type="primary"):
            run_async(ss().bundle.team.reset())
            ss().pending = (run["question"], False)
            st.rerun()
        return

    res, ans = run["result"], run["answer"]
    t_answer, t_charts, t_code, t_log = st.tabs(["📝 Answer", "📊 Charts", "🐍 Code", "📜 Run log"])
    with t_answer:
        if ans is not None:
            st.markdown(
                f"<div class='dp-card'><h5>ANSWER</h5>{ans.answer}</div>"
                f"<div class='dp-card'><h5>WHERE IT CAME FROM</h5>{ans.where_it_came_from}</div>",
                unsafe_allow_html=True,
            )
            st.markdown("<div class='dp-card' style='margin-bottom:.3rem'><h5>SUGGESTED NEXT QUESTION</h5></div>",
                        unsafe_allow_html=True)
            if st.button(f"➡️ {ans.suggested_next_question}", key="next_q"):
                ss().pending = (ans.suggested_next_question, True)
                st.rerun()
        else:
            st.warning("The written answer contained numbers that are not in result.json "
                       f"({', '.join(run['ungrounded'])}), so only the validated findings are shown.")
        metrics = list(res.metrics.items())[:4]
        if metrics:
            for col, (k, v) in zip(st.columns(len(metrics)), metrics):
                col.metric(k.replace("_", " ").title(), f"{v:,.2f}")
        if res.findings:
            st.dataframe(pd.DataFrame([f.model_dump() for f in res.findings]), hide_index=True, width="stretch")
    with t_charts:
        cols = st.columns(2)
        for i, (spec, data) in enumerate(run["charts"]):
            with cols[i % 2]:
                st.image(data, caption=spec.title, width="stretch")
                st.download_button("⬇️ Download PNG", data, file_name=Path(spec.path).name, mime="image/png",
                                   key=f"dl_{i}")
    with t_code:
        ok, msg = run["repro"]
        (st.success if ok else st.error)(("✔ Reproducible — " if ok else "✘ Not reproducible — ") + msg)
        st.code(run["code"], language="python")
        st.download_button("⬇️ Download analysis.py", run["code"], file_name="analysis.py", mime="text/x-python")
    with t_log:
        c1, c2, c3 = st.columns(3)
        c1.metric("Messages", f"{run['messages']}/{SETTINGS.MAX_MESSAGES}")
        c2.metric("Tokens used", f"{run['tokens']:,}")
        c3.metric("Self-corrections", run["self_corrections"])
        st.caption(f"Stop reason: {run['stop']}")
        st.text("\n\n".join(f"[{m['source']}] {' '.join(m['badges'])}\n{m['text']}" for m in run["transcript"]))

    st.divider()
    a, b, _ = st.columns([1, 1, 3])
    if ss().approved_by_user:
        st.success("✅ Answer approved. Ask a follow-up below to continue the same session.")
    elif a.button("✅ Approve answer", type="primary"):
        ss().approved_by_user = True
        st.rerun()
    if b.button("💬 Ask follow-up"):
        st.toast("Type your follow-up question in the box below — the agents keep the context.")


# ---------------------------------------------------------------------------- page
load_upload()
st.markdown("## 🧭 DataPilot <span style='font-size:1rem;color:#8A7F7E'>· self-correcting data analyst</span>",
            unsafe_allow_html=True)
progress = st.empty()
progress.markdown(progress_html(None, 5 if (ss().run or {}).get("approved") else -1), unsafe_allow_html=True)

if ss().df is None:
    st.info("Upload a sales CSV or Excel file in the sidebar, then ask a question such as "
            "**“Why did Electronics revenue drop in September 2026 compared to August?”**")
    st.stop()

if ss().bundle is None:
    with st.spinner("Starting the agent team…"):
        get_bundle()
if not ss().bundle.using_docker:
    st.warning("🟡 Docker is unavailable — code runs in the local `sandbox/` folder (still AST-checked and time-limited).")

chat = st.container()
with chat:
    for m in ss().log:
        render_message(m)

prompt = st.chat_input("Ask a question about your data…" if ss().run is None else "Ask a follow-up question…")
if ss().pending:
    prompt_text, follow = ss().pending
    ss().pending = None
elif prompt:
    prompt_text, follow = prompt, ss().run is not None
else:
    prompt_text, follow = None, False

if prompt_text:
    try:
        q = check_question(prompt_text)
    except GuardrailError as e:
        st.warning(str(e))
    else:
        ss().error = None
        run_question(q, follow, progress, chat)
        st.rerun()

if ss().error:
    st.error(ss().error)
elif ss().run:
    show_results(ss().run)
