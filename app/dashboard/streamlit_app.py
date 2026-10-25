"""Streamlit dashboard: runs, pass/fail results, latency trends and regression flags.

Run: ``streamlit run app/dashboard/streamlit_app.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # allow `import app` under streamlit

import altair as alt
import pandas as pd
import streamlit as st

from app.config import get_settings
from app.db.models import RunDoc, RunStatus
from app.db.mongo_client import create_client
from app.db.repo import RunRepository

# Validated categorical palette (fixed order; colour follows the endpoint, not its rank).
SERIES_COLORS = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
]
MAX_TREND_SERIES = len(SERIES_COLORS)
FLAG_LABELS = {
    "latency_regression": "Latency regression",
    "new_5xx": "New 5xx",
    "newly_failing": "Newly failing",
}


@st.cache_resource
def get_repo() -> RunRepository:
    """One repository per Streamlit server process."""
    settings = get_settings()
    repo = RunRepository(create_client(settings.mongodb_uri)[settings.mongodb_db])
    repo.ensure_indexes()
    return repo


def runs_table(runs: list[RunDoc]) -> pd.DataFrame:
    """Summary row per run."""
    return pd.DataFrame(
        [
            {
                "created": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "status": r.status.value,
                "target": r.base_url,
                "cases": r.generation.total_cases,
                "passed": r.summary.passed,
                "failed": r.summary.failed,
                "5xx": r.summary.server_errors,
                "flags": len(r.flags),
                "LLM": "fallback (rules only)" if r.generation.llm_fallback else "used",
                "id": r.id,
            }
            for r in runs
        ]
    )


def trend_frame(runs: list[RunDoc]) -> pd.DataFrame:
    """Long-format p95 per endpoint per completed run (oldest first)."""
    rows = [
        {
            "run": r.created_at.strftime("%m-%d %H:%M:%S"),
            "created_at": r.created_at,
            "endpoint": s.endpoint,
            "p95_ms": s.p95_ms,
            "p50_ms": s.p50_ms,
            "rps": s.rps,
            "flagged": any(
                f.kind == "latency_regression" and f.endpoint == s.endpoint for f in r.flags
            ),
        }
        for r in runs
        if r.status == RunStatus.COMPLETED
        for s in r.load
    ]
    return pd.DataFrame(rows).sort_values("created_at") if rows else pd.DataFrame()


def trend_chart(df: pd.DataFrame, endpoints: list[str]) -> alt.LayerChart:
    """p95 latency over runs, one line per endpoint, crosshair-free point tooltips."""
    domain = sorted(endpoints)
    color = alt.Color(
        "endpoint:N",
        scale=alt.Scale(domain=domain, range=SERIES_COLORS[: len(domain)]),
        legend=alt.Legend(orient="bottom", columns=2, title=None),
    )
    x = alt.X("run:N", sort=None, title="Run", axis=alt.Axis(labelAngle=-30, grid=False))
    y = alt.Y("p95_ms:Q", title="p95 latency (ms)", axis=alt.Axis(gridOpacity=0.4))
    tooltip = [
        alt.Tooltip("endpoint:N"),
        alt.Tooltip("run:N"),
        alt.Tooltip("p95_ms:Q", title="p95 ms", format=".0f"),
        alt.Tooltip("p50_ms:Q", title="p50 ms", format=".0f"),
        alt.Tooltip("rps:Q", format=".1f"),
        alt.Tooltip("flagged:N", title="regression flagged"),
    ]
    base = alt.Chart(df[df["endpoint"].isin(endpoints)]).encode(x=x, y=y, color=color)
    lines = base.mark_line(strokeWidth=2)
    points = base.mark_point(size=70, filled=True, opacity=1).encode(
        shape=alt.Shape(
            "flagged:N",
            scale=alt.Scale(domain=[False, True], range=["circle", "triangle"]),
            legend=None,
        ),
        tooltip=tooltip,
    )
    chart: alt.LayerChart = (lines + points).properties(height=320)
    return chart


def render_run(repo: RunRepository, run: RunDoc) -> None:
    """Details of one run."""
    st.subheader(f"Run {run.id[:8]} · {run.status.value}")
    st.caption(
        f"{run.spec_title or run.spec_url} → {run.base_url} · {run.users} users x {run.duration_s}s"
        f" · started {run.created_at:%Y-%m-%d %H:%M:%S} UTC"
    )
    if run.error:
        st.error(f"Run failed: {run.error}")

    cols = st.columns(5)
    cols[0].metric("Cases", run.generation.total_cases)
    cols[1].metric("Passed", run.summary.passed)
    cols[2].metric("Failed", run.summary.failed)
    cols[3].metric("5xx responses", run.summary.server_errors)
    cols[4].metric("Flags", len(run.flags))

    gen = run.generation
    if gen.llm_fallback:
        reasons = "; ".join(f"{k}: {v}" for k, v in gen.llm_errors.items())
        st.info(f"LLM fallback - rule-based cases were used ({reasons}).")
    else:
        st.caption(f"LLM {gen.model} (prompt {gen.prompt_version}) added {gen.llm_cases} cases.")

    st.markdown("#### Flags")
    if not run.flags:
        st.success("No regressions or new failures against previous runs.")
    for flag in run.flags:
        st.warning(f"**{FLAG_LABELS[flag.kind]}** · `{flag.endpoint}` - {flag.message}", icon="⚠️")

    st.markdown("#### Load test")
    if run.load_error:
        st.error(f"Load test failed: {run.load_error}")
    if run.load:
        st.dataframe(
            pd.DataFrame([s.model_dump() for s in run.load]),
            hide_index=True,
            width="stretch",
        )

    st.markdown("#### Functional results")
    results = repo.get_results(run.id)
    if not results:
        st.caption("No functional results stored.")
        return
    df = pd.DataFrame(
        [
            {
                "result": "PASS" if r.passed else "FAIL",
                "endpoint": r.endpoint,
                "case": r.case_name,
                "category": r.category,
                "source": r.source,
                "expected": r.expected,
                "status": r.status_code,
                "latency_ms": r.latency_ms,
                "reason": r.failure_reason or "",
                "request body": (r.request.get("body_json") or "")[:200],
            }
            for r in results
        ]
    )
    left, right = st.columns([1, 2])
    only_failed = left.toggle("Failures only", value=run.summary.failed > 0)
    endpoint_filter = right.multiselect("Endpoints", sorted(df["endpoint"].unique()))
    if only_failed:
        df = df[df["result"] == "FAIL"]
    if endpoint_filter:
        df = df[df["endpoint"].isin(endpoint_filter)]
    st.dataframe(df, hide_index=True, width="stretch")


def main() -> None:
    """Page entry point."""
    st.set_page_config(page_title="API Sentinel", layout="wide")
    st.title("API Sentinel")
    repo = get_repo()

    all_runs = repo.list_runs(limit=500)
    if not all_runs:
        st.info(
            "No runs yet. Start one with `POST /runs` or "
            "`python -m app.cli run --spec-url ... --base-url ...`."
        )
        return

    targets = sorted({r.base_url for r in all_runs})
    target = st.sidebar.selectbox("Target API", targets)
    limit = st.sidebar.slider("Runs to show", 5, 200, 30)
    if st.sidebar.button("Refresh"):
        st.rerun()
    runs = [r for r in all_runs if r.base_url == target][:limit]

    st.markdown("### Runs")
    table = runs_table(runs)
    st.dataframe(table.drop(columns=["id"]), hide_index=True, width="stretch")

    st.markdown("### p95 latency trend")
    trend = trend_frame(runs)
    if trend.empty:
        st.caption("No completed load tests yet.")
    else:
        endpoints = sorted(trend["endpoint"].unique())
        chosen = st.multiselect(
            "Endpoints",
            endpoints,
            default=endpoints[:MAX_TREND_SERIES],
            max_selections=MAX_TREND_SERIES,
        )
        if chosen:
            st.altair_chart(trend_chart(trend, chosen), width="stretch")
            st.caption("Triangles mark runs where a latency regression was flagged.")
        with st.expander("Trend data (table view)"):
            st.dataframe(
                trend.pivot_table(index="run", columns="endpoint", values="p95_ms", sort=False),
                width="stretch",
            )

    st.divider()
    labels = {
        r.id: f"{r.created_at:%Y-%m-%d %H:%M:%S} · {r.status.value} · {len(r.flags)} flags"
        for r in runs
    }
    selected = st.selectbox("Run details", list(labels), format_func=labels.__getitem__)
    run = repo.get_run(selected) if selected else None
    if run is not None:
        render_run(repo, run)


main()
