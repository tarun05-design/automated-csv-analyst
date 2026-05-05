# app.py - Automated CSV Analyst
"""
Automated CSV Analyst - Improved Version

Improvements:
  1. Plotly interactive charts (hover, zoom, pan) instead of static Matplotlib
  2. Per-chart PNG download button
  3. Better dataset type detection (retail, income, spending keywords)
  4. st.cache_data for analysis to avoid re-running on tab switches
  5. Column-level drill-down in Data Preview tab

Environment variables / Streamlit secrets (optional):
- GEMINI_API_KEY
- GOOGLE_API_KEY
- GEMINI_MODEL
"""

import os
import io
import re
import json
import math
import copy
import urllib.request
import urllib.error
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import warnings

try:
    import streamlit as st
except ModuleNotFoundError:
    class _StreamlitShim(dict):
        def __getattr__(self, name):
            if name == "session_state":
                return self
            if name == "sidebar":
                return self
            if name in {"cache_data"}:
                def decorator(*args, **kwargs):
                    def wrapper(func):
                        return func
                    return wrapper
                return decorator
            if name in {"columns", "tabs"}:
                def factory(*args, **kwargs):
                    count = len(args[0]) if args and isinstance(args[0], (list, tuple)) else (args[0] if args else 1)
                    return [self for _ in range(count)]
                return factory
            if name in {"expander", "container", "spinner", "popover"}:
                return lambda *args, **kwargs: self
            if name in {"selectbox", "text_input", "slider", "multiselect", "radio"}:
                return lambda *args, **kwargs: kwargs.get("value") or kwargs.get("index", 0)
            if name == "button":
                return lambda *args, **kwargs: False
            if name == "download_button":
                return lambda *args, **kwargs: None
            return lambda *args, **kwargs: None
        def __call__(self, *args, **kwargs):
            return self
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
    st = _StreamlitShim()

import warnings

warnings.filterwarnings("ignore")

# -----------------------------------------------------------------------------
# PAGE CONFIG
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Automated CSV Analyst",
    page_icon="📊",
    layout="wide"
)

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------
NULL_LIKE_VALUES = ["", " ", "na", "n/a", "null", "none", "-", "--", "nan", "nil", "?"]
REPORT_STYLES = ["Student Report", "Analyst Report", "Executive Summary"]
USER_GOALS = [
    "Auto",
    "General Overview",
    "Data Quality Audit",
    "Compare Groups",
    "Trend Analysis",
    "Executive Summary"
]
MAX_PREVIEW_ROWS = 200
GEMINI_MODEL_OPTIONS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]

# -----------------------------------------------------------------------------
# SESSION STATE
# -----------------------------------------------------------------------------
def init_state():
    defaults = {
        "original_df": None,
        "working_df": None,
        "analysis": None,
        "current_file": None,
        "analysis_time": None,
        "transform_history": [],
        "pinned_insights": [],
        "ai_history": [],
        "user_goal": "Auto",
        "last_question": "",
        "latest_fix": None,
        "quick_fix_preview": None,
        "ai_provider": "Local Only",
        "gemini_api_key_input": "",
        "ai_connection_ok": False,
        "ai_connection_message": "",
        "ai_report_md": "",
        "gemini_model_choice": GEMINI_MODEL_OPTIONS[0],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

# -----------------------------------------------------------------------------
# IO HELPERS
# -----------------------------------------------------------------------------
def read_csv_safely(uploaded_file):
    raw = uploaded_file.getvalue()
    attempts = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
    last_error = None
    for enc in attempts:
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except Exception as exc:
            last_error = exc
    raise ValueError(f"Could not read CSV file. Last error: {last_error}")

def clone_df(df):
    return df.copy(deep=True)

def summarize_columns(cols, limit=4):
    cols = [str(c) for c in cols if str(c).strip()]
    if not cols:
        return ""
    if len(cols) <= limit:
        return ", ".join(cols)
    return f"{', '.join(cols[:limit])} +{len(cols) - limit} more"

def detect_numeric_like_columns(df):
    numeric_like_cols = []
    for col in df.select_dtypes(include=["object", "string"]).columns:
        non_null = df[col].dropna()
        if non_null.empty:
            continue
        cleaned = (
            non_null.astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("%", "", regex=False)
            .str.replace("$", "", regex=False)
            .str.strip()
        )
        converted = pd.to_numeric(cleaned, errors="coerce")
        if len(cleaned) > 0 and converted.notna().mean() >= 0.8:
            numeric_like_cols.append(col)
    return numeric_like_cols

def detect_date_like_columns(df):
    date_cols = []
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            continue
        if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
            continue
        non_null = series.dropna()
        if non_null.empty:
            continue
        parsed, success_ratio = coerce_datetime_preview(non_null)
        if ("date" in col.lower() or "time" in col.lower() or success_ratio >= 0.7) and parsed.notna().sum() > 0:
            date_cols.append(col)
    return date_cols

def build_quick_fix_preview(df):
    if df is None or df.empty:
        return ["No dataset is available for preview yet."]
    preview_points = []
    obj_cols = df.select_dtypes(include=["object", "string"]).columns.tolist()
    trim_cols = []
    for col in obj_cols:
        non_null = df[col].dropna().astype(str)
        if not non_null.empty and (non_null != non_null.str.strip()).any():
            trim_cols.append(col)
    if trim_cols:
        preview_points.append(
            f"Trim leading/trailing spaces in {len(trim_cols)} text column(s): {summarize_columns(trim_cols)}."
        )
    null_markers = {"", "na", "n/a", "null", "none", "nil", "missing", "?", "-", "--", "nan"}
    null_marker_cols = []
    null_marker_count = 0
    for col in obj_cols:
        standardized = df[col].dropna().astype(str).str.strip().str.lower()
        marker_count = int(standardized.isin(null_markers).sum())
        if marker_count > 0:
            null_marker_cols.append(col)
            null_marker_count += marker_count
    if null_marker_count > 0:
        preview_points.append(
            f"Standardize ~{null_marker_count} null-like entries across {len(null_marker_cols)} column(s): {summarize_columns(null_marker_cols)}."
        )
    duplicate_rows = int(df.duplicated().sum())
    if duplicate_rows > 0:
        preview_points.append(f"Remove {duplicate_rows} duplicate row(s).")
    numeric_like_cols = detect_numeric_like_columns(df)
    if numeric_like_cols:
        preview_points.append(
            f"Convert {len(numeric_like_cols)} numeric-like text column(s): {summarize_columns(numeric_like_cols)}."
        )
    date_like_cols = detect_date_like_columns(df)
    if date_like_cols:
        preview_points.append(
            f"Parse {len(date_like_cols)} date/time-like column(s): {summarize_columns(date_like_cols)}."
        )
    numeric_missing_cols = [col for col in df.select_dtypes(include=np.number).columns if df[col].isna().any()]
    other_missing_cols = [col for col in df.columns if col not in numeric_missing_cols and df[col].isna().any()]
    if numeric_missing_cols or other_missing_cols:
        fill_parts = []
        if numeric_missing_cols:
            fill_parts.append(f"fill {len(numeric_missing_cols)} numeric column(s) with median")
        if other_missing_cols:
            fill_parts.append(f"fill {len(other_missing_cols)} non-numeric column(s) with mode / 'Unknown'")
        preview_points.append("Then " + " and ".join(fill_parts) + ".")
    if not preview_points:
        preview_points.append("No major quick-fix changes are needed. The dataset already looks clean.")
    return preview_points

# -----------------------------------------------------------------------------
# COLUMN ROLE INFERENCE
# -----------------------------------------------------------------------------
def coerce_datetime_preview(series):
    try:
        converted = pd.to_datetime(series, errors="coerce")
        success_rate = converted.notna().mean()
        return converted, float(success_rate)
    except Exception:
        return pd.Series([pd.NaT] * len(series)), 0.0

def infer_column_roles(df):
    roles = {
        "numeric": [],
        "categorical": [],
        "datetime": [],
        "boolean": [],
        "id_like": [],
        "text_like": [],
    }
    for col in df.columns:
        s = df[col]
        non_null = s.dropna()
        col_lower = str(col).strip().lower()

        if len(non_null) == 0:
            roles["categorical"].append(col)
            continue

        if pd.api.types.is_bool_dtype(s):
            roles["boolean"].append(col)
            continue

        if pd.api.types.is_numeric_dtype(s):
            if non_null.nunique() >= max(0.9 * len(non_null), 1) and (
                "id" in col_lower or col_lower.endswith("_id") or col_lower == "id"
            ):
                roles["id_like"].append(col)
            else:
                roles["numeric"].append(col)
            continue

        if pd.api.types.is_datetime64_any_dtype(s):
            roles["datetime"].append(col)
            continue

        if pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
            text_values = non_null.astype(str).str.strip()
            converted, rate = coerce_datetime_preview(text_values)
            has_date_name = any(token in col_lower for token in ["date", "time", "day", "month", "year"])
            month_name_ratio = text_values.str.contains(
                r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec",
                case=False,
                regex=True,
            ).mean() if len(text_values) else 0.0
            looks_dateish = text_values.str.contains(r"[-/:,]", regex=True).mean() > 0.4 if len(text_values) else False

            if rate >= 0.6 and (has_date_name or looks_dateish or month_name_ratio > 0.4):
                roles["datetime"].append(col)
                continue

            nunique = text_values.nunique()
            avg_len = text_values.str.len().mean() if len(text_values) else 0
            if (
                nunique >= max(0.9 * len(text_values), 1)
                and avg_len < 40
                and ("id" in col_lower or col_lower.endswith("_id") or col_lower == "id")
            ):
                roles["id_like"].append(col)
            elif avg_len > 40:
                roles["text_like"].append(col)
            else:
                roles["categorical"].append(col)
            continue

        roles["categorical"].append(col)
    return roles

# IMPROVED: More specific dataset type detection with retail/marketing keywords
def infer_dataset_type(df, roles):
    cols = [str(c).lower() for c in df.columns]
    joined = " ".join(cols)

    # Retail / marketing (checked before academic to avoid false positives)
    if any(token in joined for token in ["spending", "income", "mall", "customer", "purchase", "basket", "loyalty"]):
        return "Retail / Customer Data"
    if any(token in joined for token in ["sales", "revenue", "profit", "order", "product", "price", "discount"]):
        return "Sales / Business Data"
    if any(token in joined for token in ["student", "attendance", "grade", "subject", "gpa", "exam"]):
        return "Academic / Student Records"
    if any(token in joined for token in ["survey", "response", "feedback", "rating", "satisfaction", "nps"]):
        return "Survey / Feedback Data"
    if roles["datetime"] and any(token in joined for token in ["date", "time", "day", "month", "year"]):
        return "Time-Series / Trend Data"
    if any(token in joined for token in ["city", "state", "country", "region", "location", "lat", "lon"]):
        return "Geographic / Segmentation Data"
    if any(token in joined for token in ["patient", "diagnosis", "age", "bmi", "blood", "health"]):
        return "Healthcare / Medical Data"
    if any(token in joined for token in ["employee", "salary", "department", "hire", "performance"]):
        return "HR / Employee Data"
    return "General Tabular Data"

def choose_analysis_mode(user_goal, inferred_type, roles):
    if user_goal != "Auto":
        return user_goal
    if inferred_type == "Time-Series / Trend Data" or roles["datetime"]:
        return "Trend Analysis"
    if len(roles["numeric"]) == 0:
        return "Data Quality Audit"
    if len(roles["categorical"]) > 0 and len(roles["numeric"]) > 0:
        return "Compare Groups"
    return "General Overview"

def build_analysis_plan(df, roles, mode):
    plan = {
        "mode": mode,
        "modules": ["overview", "quality"],
        "focus_columns": [],
        "chart_types": [],
    }
    if roles["numeric"]:
        plan["modules"].append("numeric_profile")
    if roles["categorical"]:
        plan["modules"].append("categorical_profile")
    if len(roles["numeric"]) >= 2:
        plan["modules"].append("correlations")
        plan["modules"].append("outliers")
    if roles["datetime"]:
        plan["modules"].append("time_analysis")
    if mode == "Data Quality Audit":
        plan["chart_types"] = ["missingness", "top_categories"]
    elif mode == "Compare Groups":
        plan["chart_types"] = ["category_bar", "group_box", "scatter"]
        if roles["categorical"]:
            plan["focus_columns"].append(roles["categorical"][0])
    elif mode == "Trend Analysis":
        plan["chart_types"] = ["time_line", "category_bar"]
        if roles["datetime"]:
            plan["focus_columns"].append(roles["datetime"][0])
    elif mode == "Executive Summary":
        plan["chart_types"] = ["category_bar", "metric_bar"]
    else:
        plan["chart_types"] = ["category_bar", "metric_bar", "histogram"]
    return plan

# -----------------------------------------------------------------------------
# ANALYSIS MODULES
# -----------------------------------------------------------------------------
def summarize_quality(df):
    rows, cols = df.shape
    missing_by_col = df.isna().sum().sort_values(ascending=False)
    duplicate_rows = int(df.duplicated().sum())
    constant_cols = [c for c in df.columns if df[c].nunique(dropna=False) <= 1]
    high_missing_cols = [c for c in df.columns if rows > 0 and df[c].isna().mean() >= 0.3]
    high_card_cols = [c for c in df.columns if df[c].dtype == "object" and df[c].nunique(dropna=True) > min(50, max(10, rows * 0.5))]
    missing_total = int(df.isna().sum().sum())
    missing_pct = round((missing_total / (rows * cols) * 100), 2) if rows and cols else 0.0
    return {
        "rows": rows,
        "cols": cols,
        "missing_total": missing_total,
        "missing_pct": missing_pct,
        "missing_by_col": missing_by_col.to_dict(),
        "duplicate_rows": duplicate_rows,
        "constant_cols": constant_cols,
        "high_missing_cols": high_missing_cols,
        "high_cardinality_cols": high_card_cols,
    }

def summarize_numeric(df, numeric_cols):
    numeric_summary = {}
    for col in numeric_cols:
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) == 0:
            continue
        q1 = float(s.quantile(0.25))
        q3 = float(s.quantile(0.75))
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        outlier_count = int(((s < lower) | (s > upper)).sum())
        numeric_summary[col] = {
            "count": int(s.count()),
            "mean": float(round(s.mean(), 3)),
            "median": float(round(s.median(), 3)),
            "std": float(round(s.std(), 3)) if s.count() > 1 else 0.0,
            "min": float(round(s.min(), 3)),
            "max": float(round(s.max(), 3)),
            "q1": float(round(q1, 3)),
            "q3": float(round(q3, 3)),
            "outlier_count": outlier_count,
            "skew": float(round(s.skew(), 3)) if s.count() > 2 else 0.0,
        }
    return numeric_summary

def summarize_categorical(df, categorical_cols, max_cols=10):
    results = {}
    for col in categorical_cols[:max_cols]:
        s = df[col].astype(str).fillna("Missing")
        vc = s.value_counts(dropna=False).head(8)
        results[col] = {
            "unique": int(df[col].nunique(dropna=True)),
            "top_value": str(vc.index[0]) if len(vc) else None,
            "top_count": int(vc.iloc[0]) if len(vc) else 0,
            "top_pct": float(round((vc.iloc[0] / len(df)) * 100, 2)) if len(df) and len(vc) else 0.0,
            "distribution": {str(k): int(v) for k, v in vc.items()},
        }
    return results

def compute_correlations(df, numeric_cols):
    if len(numeric_cols) < 2:
        return {"matrix": {}, "top_pairs": []}
    safe_cols = numeric_cols[:20]
    corr = df[safe_cols].corr(numeric_only=True)
    pairs = []
    for i, col_a in enumerate(safe_cols):
        for col_b in safe_cols[i + 1:]:
            val = corr.loc[col_a, col_b]
            if pd.notna(val):
                pairs.append((col_a, col_b, float(val)))
    pairs_sorted = sorted(pairs, key=lambda x: abs(x[2]), reverse=True)
    top_pairs = [
        {"col_a": a, "col_b": b, "corr": round(v, 3)}
        for a, b, v in pairs_sorted[:8]
    ]
    return {
        "matrix": corr.round(3).fillna(0).to_dict(),
        "top_pairs": top_pairs,
    }

def summarize_time(df, datetime_cols, numeric_cols):
    if not datetime_cols:
        return {}
    col = datetime_cols[0]
    dt = pd.to_datetime(df[col], errors="coerce")
    valid = dt.dropna()
    summary = {
        "primary_datetime_col": col,
        "valid_dates": int(valid.count()),
    }
    if len(valid):
        summary["min_date"] = str(valid.min())
        summary["max_date"] = str(valid.max())
        summary["date_span_days"] = int((valid.max() - valid.min()).days)
    if numeric_cols and len(valid):
        temp = df.copy()
        temp[col] = dt
        temp = temp.dropna(subset=[col])
        temp["_month"] = temp[col].dt.to_period("M").astype(str)
        num_col = numeric_cols[0]
        monthly = temp.groupby("_month")[num_col].mean().tail(12)
        summary["monthly_series"] = {str(k): float(round(v, 3)) for k, v in monthly.items()}
    return summary

def generate_insight_cards(df, roles, quality, numeric_summary, categorical_summary, corr_data, time_summary, mode):
    insights = []
    rows = quality["rows"]
    missing_pct = quality["missing_pct"]

    if missing_pct == 0:
        insights.append({
            "id": "quality_complete",
            "title": "Dataset is fully complete",
            "type": "data_quality",
            "severity": "low",
            "confidence": 0.98,
            "evidence": "No missing values detected across the dataset.",
            "why_it_matters": "You can move directly into comparison and trend analysis without cleaning first.",
            "action": "Start with segmentation or correlation analysis."
        })
    elif missing_pct >= 15:
        insights.append({
            "id": "quality_missing_high",
            "title": "High missing data risk detected",
            "type": "data_quality",
            "severity": "high",
            "confidence": 0.93,
            "evidence": f"{missing_pct}% of all cells are missing.",
            "why_it_matters": "Heavy missingness can distort charts, averages, and model-like reasoning.",
            "action": "Open Clean Data and fix the most affected columns first."
        })
    elif missing_pct > 0:
        insights.append({
            "id": "quality_missing_moderate",
            "title": "Some missing values need attention",
            "type": "data_quality",
            "severity": "medium",
            "confidence": 0.88,
            "evidence": f"{missing_pct}% of all cells are missing.",
            "why_it_matters": "The dataset is usable, but some metrics may still be biased.",
            "action": "Review missing columns and apply targeted fills or removals."
        })

    if quality["duplicate_rows"] > 0:
        insights.append({
            "id": "duplicates_found",
            "title": "Duplicate rows found",
            "type": "data_quality",
            "severity": "medium",
            "confidence": 0.95,
            "evidence": f"{quality['duplicate_rows']} duplicate rows detected.",
            "why_it_matters": "Duplicates can inflate counts and mislead category or trend analysis.",
            "action": "Use Clean Data to remove duplicates and rerun analysis."
        })

    if quality["constant_cols"]:
        insights.append({
            "id": "constant_columns",
            "title": "Some columns carry little information",
            "type": "schema",
            "severity": "low",
            "confidence": 0.90,
            "evidence": f"Constant columns: {', '.join(quality['constant_cols'][:4])}",
            "why_it_matters": "Constant columns rarely add analytical value.",
            "action": "Drop constant columns if they are not needed for reference."
        })

    for col, meta in list(categorical_summary.items())[:3]:
        if meta["top_pct"] >= 60:
            insights.append({
                "id": f"dom_{col}",
                "title": f"One category dominates '{col}'",
                "type": "segmentation",
                "severity": "medium",
                "confidence": 0.87,
                "evidence": f"'{meta['top_value']}' accounts for {meta['top_pct']}% of rows.",
                "why_it_matters": "Highly imbalanced categories can hide smaller but important groups.",
                "action": f"Compare metrics across {col} to see whether minority groups behave differently."
            })

    numeric_sorted = sorted(
        numeric_summary.items(),
        key=lambda item: item[1].get("outlier_count", 0),
        reverse=True
    )
    if numeric_sorted and numeric_sorted[0][1]["outlier_count"] > 0:
        col, meta = numeric_sorted[0]
        insights.append({
            "id": f"outliers_{col}",
            "title": f"Outliers detected in '{col}'",
            "type": "anomaly",
            "severity": "medium",
            "confidence": 0.86,
            "evidence": f"{meta['outlier_count']} potential outliers found using the IQR method.",
            "why_it_matters": "A few extreme values may be driving averages or correlations.",
            "action": f"Inspect the distribution of {col} with a box plot or filtered view."
        })

    skew_candidates = [(c, m["skew"]) for c, m in numeric_summary.items() if abs(m.get("skew", 0)) >= 1]
    if skew_candidates:
        col, skew = sorted(skew_candidates, key=lambda x: abs(x[1]), reverse=True)[0]
        insights.append({
            "id": f"skew_{col}",
            "title": f"'{col}' is highly skewed",
            "type": "distribution",
            "severity": "low",
            "confidence": 0.83,
            "evidence": f"Skewness = {round(skew, 3)}.",
            "why_it_matters": "Median may represent this column better than mean.",
            "action": f"Use a histogram or box plot before making decisions from averages."
        })

    if corr_data["top_pairs"]:
        top = corr_data["top_pairs"][0]
        if abs(top["corr"]) >= 0.7:
            direction = "positive" if top["corr"] > 0 else "negative"
            insights.append({
                "id": f"corr_{top['col_a']}_{top['col_b']}",
                "title": "Strong numeric relationship detected",
                "type": "correlation",
                "severity": "medium",
                "confidence": 0.89,
                "evidence": f"{top['col_a']} and {top['col_b']} have a {direction} correlation of {top['corr']}.",
                "why_it_matters": "These variables may move together and should be analyzed as a pair.",
                "action": f"Create a scatter plot of {top['col_a']} vs {top['col_b']}."
            })

    if time_summary.get("valid_dates", 0) > 0 and time_summary.get("date_span_days", 0) >= 30:
        insights.append({
            "id": "time_span",
            "title": "Time-based analysis is possible",
            "type": "trend",
            "severity": "low",
            "confidence": 0.91,
            "evidence": f"Date span covers {time_summary['date_span_days']} days in '{time_summary['primary_datetime_col']}'.",
            "why_it_matters": "You can look for trends, seasonality, or before-vs-after changes.",
            "action": "Open the chart section and generate a time trend."
        })

    if rows < 100:
        insights.append({
            "id": "sample_small",
            "title": "Small sample size",
            "type": "risk",
            "severity": "medium",
            "confidence": 0.90,
            "evidence": f"Only {rows} rows are available.",
            "why_it_matters": "Patterns may be unstable and less representative.",
            "action": "Treat conclusions as directional rather than final."
        })
    elif rows >= 1000:
        insights.append({
            "id": "sample_large",
            "title": "Good volume for exploratory analysis",
            "type": "overview",
            "severity": "low",
            "confidence": 0.92,
            "evidence": f"{rows:,} rows available for analysis.",
            "why_it_matters": "Large enough for reliable segmentation and pattern detection.",
            "action": "Use group comparisons or deeper filtering to uncover non-obvious patterns."
        })

    if mode == "Compare Groups" and roles["categorical"] and roles["numeric"]:
        insights.append({
            "id": "mode_compare_groups",
            "title": "This dataset is a good fit for group comparison",
            "type": "planner",
            "severity": "low",
            "confidence": 0.88,
            "evidence": f"Found {len(roles['categorical'])} categorical and {len(roles['numeric'])} numeric columns.",
            "why_it_matters": "You can compare average values across segments instead of only reading overall totals.",
            "action": f"Try comparing {roles['numeric'][0]} across {roles['categorical'][0]}."
        })

    return insights[:12]

def suggest_charts(df, roles, mode, corr_data, time_summary):
    charts = []
    if roles["categorical"]:
        charts.append({
            "title": f"Top categories in {roles['categorical'][0]}",
            "kind": "category_bar",
            "x": roles["categorical"][0],
            "y": None,
            "reason": "Useful for understanding category distribution quickly."
        })
    if roles["numeric"]:
        charts.append({
            "title": f"Distribution of {roles['numeric'][0]}",
            "kind": "histogram",
            "x": roles["numeric"][0],
            "y": None,
            "reason": "Shows spread, skew, and unusual values."
        })
    if len(roles["numeric"]) >= 2:
        pair = corr_data["top_pairs"][0] if corr_data["top_pairs"] else {
            "col_a": roles["numeric"][0],
            "col_b": roles["numeric"][1]
        }
        charts.append({
            "title": f"{pair['col_a']} vs {pair['col_b']}",
            "kind": "scatter",
            "x": pair["col_a"],
            "y": pair["col_b"],
            "reason": "Best candidate pair for numeric relationship analysis."
        })
    if mode == "Compare Groups" and roles["categorical"] and roles["numeric"]:
        charts.append({
            "title": f"{roles['numeric'][0]} by {roles['categorical'][0]}",
            "kind": "group_bar",
            "x": roles["categorical"][0],
            "y": roles["numeric"][0],
            "reason": "Helps compare numeric performance across groups."
        })
    if roles["datetime"] and roles["numeric"]:
        charts.append({
            "title": f"{roles['numeric'][0]} over time",
            "kind": "time_line",
            "x": roles["datetime"][0],
            "y": roles["numeric"][0],
            "reason": "Useful for trends and change over time."
        })
    return charts[:5]

# IMPROVED: use st.cache_data to prevent redundant re-analysis on tab switches
@st.cache_data(show_spinner=False)
def run_full_analysis_cached(df_hash, df_json, filename, user_goal):
    """Cached version. df_hash is used as the cache key."""
    df = pd.read_json(io.StringIO(df_json))
    return _run_full_analysis_inner(df, filename, user_goal)

def run_full_analysis(df, filename, user_goal):
    return _run_full_analysis_inner(df, filename, user_goal)

def _run_full_analysis_inner(df, filename, user_goal):
    roles = infer_column_roles(df)
    inferred_type = infer_dataset_type(df, roles)
    mode = choose_analysis_mode(user_goal, inferred_type, roles)
    plan = build_analysis_plan(df, roles, mode)
    quality = summarize_quality(df)
    numeric_summary = summarize_numeric(df, roles["numeric"])
    categorical_summary = summarize_categorical(df, roles["categorical"])
    corr_data = compute_correlations(df, roles["numeric"])
    time_summary = summarize_time(df, roles["datetime"], roles["numeric"])
    insights = generate_insight_cards(
        df=df,
        roles=roles,
        quality=quality,
        numeric_summary=numeric_summary,
        categorical_summary=categorical_summary,
        corr_data=corr_data,
        time_summary=time_summary,
        mode=mode
    )
    charts = suggest_charts(df, roles, mode, corr_data, time_summary)
    quality_score = max(0, round(100 - quality["missing_pct"] - (quality["duplicate_rows"] / max(len(df), 1) * 100), 1))
    return {
        "filename": filename,
        "analysis_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset_type": inferred_type,
        "mode": mode,
        "plan": plan,
        "roles": roles,
        "quality": quality,
        "quality_score": quality_score,
        "numeric_summary": numeric_summary,
        "categorical_summary": categorical_summary,
        "correlations": corr_data,
        "time_summary": time_summary,
        "insights": insights,
        "suggested_charts": charts,
    }

# -----------------------------------------------------------------------------
# DATA QUALITY TRANSFORMS
# -----------------------------------------------------------------------------
def snapshot_df_state(df):
    total_cells = max(len(df) * max(len(df.columns), 1), 1)
    missing_cells = int(df.isna().sum().sum())
    missing_pct = round((missing_cells / total_cells) * 100, 1)
    return {
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "missing_cells": missing_cells,
        "missing_pct": missing_pct,
        "duplicate_rows": int(df.duplicated().sum()),
        "numeric_cols": int(len(df.select_dtypes(include=np.number).columns)),
    }

def build_transform_details(action_name, before_df, after_df, extra=None):
    extra = extra or {}
    before = snapshot_df_state(before_df)
    after = snapshot_df_state(after_df)
    common_cols = [c for c in before_df.columns if c in after_df.columns]
    dropped_cols = [c for c in before_df.columns if c not in after_df.columns]
    dtype_changed_cols = []
    missing_changed_cols = []
    value_changed_cols = []
    column_rows = []
    for col in common_cols:
        before_dtype = str(before_df[col].dtype)
        after_dtype = str(after_df[col].dtype)
        before_missing = int(before_df[col].isna().sum())
        after_missing = int(after_df[col].isna().sum())
        changed_parts = []
        if before_dtype != after_dtype:
            dtype_changed_cols.append(col)
            changed_parts.append(f"type: {before_dtype} → {after_dtype}")
        if before_missing != after_missing:
            missing_changed_cols.append(col)
            changed_parts.append(f"missing: {before_missing} → {after_missing}")
        try:
            if len(before_df) == len(after_df) and not before_df[col].equals(after_df[col]):
                value_changed_cols.append(col)
        except Exception:
            pass
        if changed_parts:
            column_rows.append({
                "Column": col,
                "Change": " | ".join(changed_parts),
                "Before": before_dtype,
                "After": after_dtype,
            })
    for col in dropped_cols:
        column_rows.append({
            "Column": col,
            "Change": "dropped",
            "Before": str(before_df[col].dtype),
            "After": "removed",
        })
    affected_columns = list(dict.fromkeys(dtype_changed_cols + missing_changed_cols + value_changed_cols + dropped_cols))
    removed_rows = max(before["rows"] - after["rows"], 0)
    filled_cells = max(before["missing_cells"] - after["missing_cells"], 0)
    if action_name == "Remove Duplicates":
        summary = f"Removed {removed_rows} duplicate rows."
    elif action_name == "Fill Missing Values":
        summary = f"Filled {filled_cells} missing values across {max(len(missing_changed_cols), 1) if filled_cells else 0} columns."
    elif action_name == "Drop High-Missing Columns":
        summary = f"Dropped {len(dropped_cols)} high-missing columns."
    elif action_name == "Convert Numeric-like":
        summary = f"Converted {len(dtype_changed_cols)} columns to numeric types."
    elif action_name == "Infer Dates":
        summary = f"Inferred date types for {len(dtype_changed_cols)} columns."
    elif action_name == "Standardize Nulls":
        summary = f"Standardized null-like text values across {len(value_changed_cols) or len(missing_changed_cols) or 0} columns."
    elif action_name == "Trim Text":
        summary = f"Trimmed whitespace in {len(value_changed_cols) or 0} text columns."
    else:
        summary = f"Applied {action_name.lower()}."
    notes = []
    if removed_rows:
        notes.append(f"Rows: {before['rows']} → {after['rows']}")
    if before["missing_cells"] != after["missing_cells"]:
        notes.append(f"Missing cells: {before['missing_cells']} → {after['missing_cells']}")
    if dropped_cols:
        notes.append(f"Dropped columns: {', '.join(dropped_cols[:6])}")
    if dtype_changed_cols:
        notes.append(f"Type changes: {', '.join(dtype_changed_cols[:6])}")
    preview_cols = affected_columns[:6] if affected_columns else list(after_df.columns[:6])
    preview_before = before_df[preview_cols].head(8).copy() if preview_cols else pd.DataFrame()
    preview_after = after_df[[c for c in preview_cols if c in after_df.columns]].head(8).copy() if preview_cols else pd.DataFrame()
    return {
        "action": action_name,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "before": before,
        "after": after,
        "summary": summary,
        "notes": notes,
        "affected_columns": affected_columns[:12],
        "column_rows": column_rows[:20],
        "preview_before": preview_before,
        "preview_after": preview_after,
        "extra": extra,
    }

def record_transform(action_name, before_df, after_df, extra=None):
    details = build_transform_details(action_name, before_df, after_df, extra=extra)
    st.session_state.latest_fix = details
    # Only the most recent entry needs before_df (for Undo Last Step).
    # Clear before_df from all previous entries to avoid storing N full DataFrames.
    for entry in st.session_state.transform_history:
        entry["before_df"] = None
    st.session_state.transform_history.append({
        "action": action_name,
        "timestamp": details["timestamp"],
        "before_df": clone_df(before_df),  # Only this latest entry keeps it
        "details": details,
    })

def apply_trim_whitespace(df):
    out = df.copy()
    for col in out.select_dtypes(include=["object"]).columns:
        out[col] = out[col].apply(lambda x: x.strip() if isinstance(x, str) else x)
    return out

def apply_standardize_nulls(df):
    out = df.copy()
    for col in out.select_dtypes(include=["object"]).columns:
        out[col] = out[col].replace(NULL_LIKE_VALUES, np.nan)
        out[col] = out[col].replace([v.upper() for v in NULL_LIKE_VALUES], np.nan)
    return out

def apply_remove_duplicates(df):
    return df.drop_duplicates().reset_index(drop=True)

def apply_convert_numeric_like(df):
    out = df.copy()
    for col in out.select_dtypes(include=["object"]).columns:
        sample = out[col].dropna().astype(str)
        if len(sample) == 0:
            continue
        cleaned = sample.str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.replace("%", "", regex=False)
        converted = pd.to_numeric(cleaned, errors="coerce")
        if converted.notna().mean() >= 0.8 and converted.notna().sum() >= 2:
            full_clean = out[col].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.replace("%", "", regex=False)
            out[col] = pd.to_numeric(full_clean, errors="coerce")
    return out

def apply_infer_dates(df):
    out = df.copy()
    for col in out.select_dtypes(include=["object"]).columns:
        sample = out[col].dropna().astype(str)
        if len(sample) == 0:
            continue
        parsed = pd.to_datetime(sample, errors="coerce")
        has_date_name = any(token in str(col).lower() for token in ["date", "time", "day", "month", "year"])
        looks_dateish = sample.str.contains(r"[-/:]", regex=True).mean() > 0.5
        if parsed.notna().mean() >= 0.7 and (has_date_name or looks_dateish):
            out[col] = pd.to_datetime(out[col], errors="coerce")
    return out

def apply_fill_missing(df):
    out = df.copy()
    for col in out.columns:
        if out[col].isna().sum() == 0:
            continue
        if pd.api.types.is_numeric_dtype(out[col]):
            out[col] = out[col].fillna(out[col].median())
        else:
            mode = out[col].mode(dropna=True)
            fill_value = mode.iloc[0] if len(mode) else "Unknown"
            out[col] = out[col].fillna(fill_value)
    return out

def apply_drop_high_missing(df, threshold=0.5):
    cols_to_drop = [c for c in df.columns if df[c].isna().mean() >= threshold]
    return df.drop(columns=cols_to_drop), cols_to_drop

def apply_quick_fix_bundle(df):
    fixed = clone_df(df)
    fixed = apply_trim_whitespace(fixed)
    fixed = apply_standardize_nulls(fixed)
    fixed = apply_remove_duplicates(fixed)
    fixed = apply_convert_numeric_like(fixed)
    fixed = apply_infer_dates(fixed)
    fixed = apply_fill_missing(fixed)
    return fixed

def rerun_analysis():
    if st.session_state.working_df is not None and st.session_state.current_file:
        st.session_state.analysis = run_full_analysis(
            st.session_state.working_df,
            st.session_state.current_file,
            st.session_state.user_goal
        )
        st.session_state.analysis_time = datetime.now()

# -----------------------------------------------------------------------------
# IMPROVED: PLOTLY INTERACTIVE CHARTS
# -----------------------------------------------------------------------------
def render_chart_plotly(df, spec):
    """Returns a Plotly figure for interactive rendering."""
    kind = spec.get("kind")
    x = spec.get("x")
    y = spec.get("y")
    title = spec.get("title", "")

    try:
        if kind == "category_bar" and x:
            vc = df[x].astype(str).fillna("Missing").value_counts().head(10).reset_index()
            vc.columns = [x, "Count"]
            # Use a fixed solid color palette — avoids near-white bars from continuous scales
            fig = px.bar(vc, x=x, y="Count", title=title, text="Count",
                         color_discrete_sequence=["#1f3e7c"] * len(vc))
            fig.update_traces(textposition="outside", marker_line_width=0)
            fig.update_layout(showlegend=False)

        elif kind == "histogram" and x:
            s = pd.to_numeric(df[x], errors="coerce").dropna()
            fig = px.histogram(s, x=x, nbins=25, title=title,
                               color_discrete_sequence=["#4472C4"],
                               marginal="box")
            fig.update_layout(bargap=0.05)

        elif kind == "scatter" and x and y:
            temp = df[[x, y]].copy()
            temp[x] = pd.to_numeric(temp[x], errors="coerce")
            temp[y] = pd.to_numeric(temp[y], errors="coerce")
            temp = temp.dropna()
            # Add color by categorical column if available
            roles = infer_column_roles(df)
            color_col = roles["categorical"][0] if roles["categorical"] else None
            if color_col and color_col not in [x, y]:
                temp2 = df[[x, y, color_col]].copy()
                temp2[x] = pd.to_numeric(temp2[x], errors="coerce")
                temp2[y] = pd.to_numeric(temp2[y], errors="coerce")
                temp2 = temp2.dropna()
                fig = px.scatter(temp2, x=x, y=y, color=color_col, title=title,
                                 trendline="ols", trendline_scope="overall",
                                 opacity=0.7)
            else:
                fig = px.scatter(temp, x=x, y=y, title=title,
                                 trendline="ols", opacity=0.7,
                                 color_discrete_sequence=["#7E57C2"])

        elif kind == "group_bar" and x and y:
            temp = df[[x, y]].copy()
            temp[y] = pd.to_numeric(temp[y], errors="coerce")
            temp = temp.dropna()
            grouped = temp.groupby(x)[y].mean().sort_values(ascending=False).head(10).reset_index()
            grouped.columns = [x, f"Avg {y}"]
            fig = px.bar(grouped, x=x, y=f"Avg {y}", title=title,
                         color=f"Avg {y}", color_continuous_scale="Oranges",
                         text=f"Avg {y}")
            fig.update_traces(texttemplate="%{text:.2f}", textposition="outside")
            fig.update_layout(coloraxis_showscale=False)

        elif kind == "box_plot" and x:
            roles = infer_column_roles(df)
            group_col = roles["categorical"][0] if roles["categorical"] and roles["categorical"][0] != x else None
            if group_col:
                temp = df[[x, group_col]].copy()
                temp[x] = pd.to_numeric(temp[x], errors="coerce")
                temp = temp.dropna()
                fig = px.box(temp, x=group_col, y=x, title=title,
                             color=group_col, points="outliers")
            else:
                s = pd.to_numeric(df[x], errors="coerce").dropna()
                fig = px.box(s, y=x, title=title,
                             color_discrete_sequence=["#26A69A"], points="outliers")

        elif kind == "time_line" and x and y:
            temp = df[[x, y]].copy()
            temp[x] = pd.to_datetime(temp[x], errors="coerce")
            temp[y] = pd.to_numeric(temp[y], errors="coerce")
            temp = temp.dropna(subset=[x])
            if len(temp):
                period_code, period_label = infer_time_period(temp[x])
                temp["_period"] = temp[x].dt.to_period(period_code).dt.to_timestamp()
                grouped_count = temp.groupby("_period").size()
                sparse_periods = (grouped_count < 5).sum() > max(len(grouped_count) * 0.3, 1)

                if sparse_periods or temp[y].dropna().empty:
                    grouped = grouped_count.reset_index()
                    grouped.columns = [x, "Count"]
                    dynamic_title = format_time_series_title("Records", period_label)
                    fig = px.line(grouped, x=x, y="Count",
                                  title=dynamic_title,
                                  markers=True, color_discrete_sequence=["#E91E63"])
                    fig.update_traces(line_width=2.5)
                    fig.update_layout(yaxis_title="Records")
                else:
                    temp_valid = temp.dropna(subset=[y])
                    grouped = temp_valid.groupby("_period")[y].mean().reset_index()
                    grouped.columns = [x, f"Avg {y}"]
                    dynamic_title = title or format_time_series_title(y, period_label, agg_label="Average")
                    fig = px.line(grouped, x=x, y=f"Avg {y}", title=dynamic_title,
                                  markers=True, color_discrete_sequence=["#E91E63"])
                    fig.update_traces(line_width=2.5)
            else:
                fig = go.Figure()
                fig.add_annotation(text="Not enough valid date/metric values.",
                                   xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)

        elif kind == "correlation_heatmap":
            corr_data = spec.get("corr_data", {})
            matrix = corr_data.get("matrix", {})
            if matrix:
                corr_df = pd.DataFrame(matrix)
                fig = px.imshow(corr_df, text_auto=".2f", title=title,
                                color_continuous_scale="RdBu_r", zmin=-1, zmax=1,
                                aspect="auto")
            else:
                fig = go.Figure()
                fig.add_annotation(text="Not enough numeric columns for a correlation heatmap.",
                                   xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)

        else:
            fig = go.Figure()
            fig.add_annotation(text="Unsupported chart configuration.",
                               xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)

        fig.update_layout(
            margin=dict(t=50, b=40, l=40, r=20),
            height=420,
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        return fig

    except Exception as exc:
        fig = go.Figure()
        fig.add_annotation(text=f"Chart error: {exc}",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        return fig

def fig_to_png_bytes(fig):
    """Convert Plotly figure to PNG bytes for download. Requires kaleido."""
    try:
        import kaleido  # noqa: F401 — explicit check before calling to_image
        return fig.to_image(format="png", width=1000, height=500, scale=2)
    except ImportError:
        return None  # kaleido not installed — download button hidden gracefully
    except Exception:
        return None

def _kaleido_available():
    """Returns True if kaleido is installed."""
    try:
        import kaleido  # noqa: F401
        return True
    except ImportError:
        return False

def show_chart_with_download(fig, chart_title):
    """Display a Plotly chart and offer a PNG download button."""
    st.plotly_chart(fig, use_container_width=True)
    if not _kaleido_available():
        st.caption("💡 Install kaleido (`pip install kaleido`) to enable PNG chart downloads.")
        return
    png_bytes = fig_to_png_bytes(fig)
    if png_bytes:
        safe_name = re.sub(r"[^\w\-]", "_", chart_title.lower())[:50]
        st.download_button(
            label="⬇ Download Chart as PNG",
            data=png_bytes,
            file_name=f"{safe_name}.png",
            mime="image/png",
            key=f"dl_{safe_name}_{id(fig)}",
        )

# -----------------------------------------------------------------------------
# TIME-SERIES HELPERS
# -----------------------------------------------------------------------------
def infer_time_period(dt_series):
    valid = pd.to_datetime(dt_series, errors="coerce").dropna()
    if valid.empty:
        return "M", "Month"
    span_days = max(int((valid.max() - valid.min()).days), 0)
    unique_points = int(valid.nunique())
    if span_days >= 730 or unique_points > 365:
        return "Y", "Year"
    if span_days >= 60 or unique_points > 31:
        return "M", "Month"
    if span_days >= 2 or unique_points > 1:
        return "D", "Day"
    return "H", "Hour"

def format_time_series_title(metric_name, period_label, agg_label="Average"):
    if metric_name == "Records":
        return f"Records per {period_label}"
    return f"{agg_label} {metric_name} per {period_label}"

# -----------------------------------------------------------------------------
# AI HELPERS
# -----------------------------------------------------------------------------
def get_gemini_api_key():
    if st.session_state.get("ai_provider", "Local Only") != "Gemini API":
        return None
    token = st.session_state.get("gemini_api_key_input", "").strip()
    if token:
        return token
    try:
        token = st.secrets.get("GEMINI_API_KEY")
    except Exception:
        token = None
    if not token:
        try:
            token = st.secrets.get("GOOGLE_API_KEY")
        except Exception:
            token = None
    return token or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

def get_gemini_model():
    return st.session_state.get("gemini_model_choice") or os.getenv("GEMINI_MODEL") or GEMINI_MODEL_OPTIONS[0]

def get_ai_provider():
    return st.session_state.get("ai_provider", "Local Only")

def get_ai_model_name():
    provider = get_ai_provider()
    if provider == "Gemini API":
        return get_gemini_model()
    return "Local Analyst Mode"

def extract_gemini_text(payload):
    try:
        parts = payload.get("candidates", [])[0].get("content", {}).get("parts", [])
        chunks = [part.get("text", "") for part in parts if isinstance(part, dict)]
        return "\n".join([c for c in chunks if c]).strip()
    except Exception:
        return ""

def extract_gemini_finish_reason(payload):
    """Returns the finishReason string from a Gemini response, e.g. STOP or MAX_TOKENS."""
    try:
        return payload.get("candidates", [])[0].get("finishReason", "STOP")
    except Exception:
        return "STOP"

def gemini_chat_request(user_message, max_tokens=220, temperature=0.2, _attempt=1):
    import time as _time
    token = get_gemini_api_key()
    model = get_gemini_model()
    if not token:
        return False, {"error": "Please enter a Gemini API key."}
    payload = {
        "contents": [{"parts": [{"text": user_message}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-goog-api-key": token,
            "Content-Type": "application/json",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return True, payload
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        # Auto-retry once on 503 (Gemini overload) after a short wait
        if exc.code == 503 and _attempt == 1:
            _time.sleep(2)
            return gemini_chat_request(user_message, max_tokens=max_tokens,
                                       temperature=temperature, _attempt=2)
        return False, {"error": f"Gemini HTTP error: {detail}"}
    except Exception as exc:
        return False, {"error": f"Connection failed: {exc}"}

def call_selected_provider(user_message, max_tokens=220, temperature=0.2):
    provider = get_ai_provider()
    if provider == "Gemini API":
        ok, payload = gemini_chat_request(user_message, max_tokens=max_tokens, temperature=temperature)
        return ok, payload, extract_gemini_text(payload) if ok else ""
    return False, {"error": "Local Only mode selected."}, ""

def test_ai_connection():
    provider = get_ai_provider()
    if provider == "Local Only":
        return True, "Local Analyst Mode is active. No external API key is required."
    ok, payload, answer = call_selected_provider("Reply with only the word OK.", max_tokens=8, temperature=0.1)
    if not ok:
        return False, payload.get("error", "Connection failed.")
    if not answer:
        return False, f"{provider} responded, but the response format was unexpected."
    return True, f"Connected successfully to {provider} using {get_ai_model_name()}"

def build_ai_context(analysis):
    top_insights = analysis["insights"][:5]
    compact = {
        "filename": analysis["filename"],
        "dataset_type": analysis["dataset_type"],
        "mode": analysis["mode"],
        "rows": analysis["quality"]["rows"],
        "cols": analysis["quality"]["cols"],
        "quality_score": analysis["quality_score"],
        "roles": analysis["roles"],
        "top_insights": [
            {"title": item["title"], "evidence": item["evidence"], "action": item["action"]}
            for item in top_insights
        ],
        "top_correlations": analysis["correlations"]["top_pairs"][:3],
    }
    return json.dumps(compact, indent=2)

def get_ai_runtime_status():
    provider = get_ai_provider()
    model = get_ai_model_name()
    token_ready = bool(get_gemini_api_key()) if provider == "Gemini API" else False
    if provider == "Local Only":
        return {"mode": "Local Analyst Mode", "reason": "The app is using built-in local answers. Switch to Gemini API in the sidebar to enable external AI.", "token_ready": False, "model": model}
    if not token_ready:
        return {"mode": provider, "reason": f"{provider} is selected, but the API key is missing.", "token_ready": False, "model": model}
    if st.session_state.get("ai_connection_ok"):
        return {"mode": provider, "reason": st.session_state.get("ai_connection_message") or f"{provider} is ready with model: {model}", "token_ready": True, "model": model}
    return {"mode": provider, "reason": f"{provider} credentials are present. Use Test AI Connection in the sidebar for a full check. Model: {model}", "token_ready": True, "model": model}

def clean_ai_markdown(text):
    if not text:
        return ""
    cleaned = str(text).strip()
    if cleaned.count("**") % 2 != 0:
        cleaned += "**"
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"(\n\d+\.\s*\*\*[^\n\*]+)$", "", cleaned).strip()
    return cleaned

def generate_ai_report(analysis, style="Balanced"):
    prompt = (
        "You are a senior data analyst. Write a COMPLETE markdown report for this dataset. "
        "Every section must be fully written — do not stop early, do not leave any section unfinished. "
        "Use ## headings, bullet points, and short paragraphs. "
        "Do not mention missing internal context. "
        "The report must end with a complete section — no truncated bullet points or half-finished sentences.\n\n"
        f"Preferred style: {style}.\n\n"
        f"CONTEXT:\n{build_ai_context(analysis)}\n\n"
        "Write ALL of the following sections in full:\n"
        "## 1. Executive Summary\n"
        "## 2. Dataset Snapshot\n"
        "## 3. Key Insights\n"
        "## 4. Data Quality Risks\n"
        "## 5. Recommended Next Steps\n"
        "## 6. Project Demo Angle\n\n"
        "Important: complete every section before finishing. Do not truncate."
    )
    ok, payload, answer = call_selected_provider(prompt, max_tokens=4000, temperature=0.2)
    if not ok:
        raw_error = payload.get("error", "")
        try:
            err_obj = json.loads(raw_error) if isinstance(raw_error, str) and raw_error.strip().startswith("{") else None
            if err_obj and isinstance(err_obj, dict):
                msg = err_obj.get("error", {}).get("message", "")
                friendly = msg[:120] if msg else "AI report generation failed."
            else:
                friendly = str(raw_error)[:120] if raw_error else "AI report generation failed."
        except Exception:
            friendly = str(raw_error)[:120] if raw_error else "AI report generation failed."
        return None, friendly
    # Detect if Gemini hit the token ceiling before finishing
    finish_reason = extract_gemini_finish_reason(payload)
    answer = clean_ai_markdown(answer)
    if not answer:
        return None, f"{get_ai_provider()} returned an empty report."
    if finish_reason == "MAX_TOKENS":
        answer += (
            "\n\n---\n> ⚠️ **Report was cut short** — the model hit its output limit before completing all sections. "
            "Try switching to a shorter Report Style (e.g. *Student Report*) or reduce the dataset size."
        )
    return answer, None

def build_answer_support(question, analysis):
    q = question.lower()
    quality = analysis["quality"]
    charts = analysis["suggested_charts"]
    insights = analysis["insights"]
    evidence = [
        f"Rows: {quality.get('rows', 0)}",
        f"Columns: {quality.get('cols', 0)}",
        f"Missing: {quality['missing_pct']}%",
        f"Duplicate rows: {quality['duplicate_rows']}",
        f"Mode: {analysis['mode']}",
    ]
    if insights:
        evidence.append(f"Top insight: {insights[0]['title']}")
    if any(word in q for word in ["chart", "visual", "graph"]) and charts:
        evidence.append(f"Top chart suggestion: {charts[0]['title']}")
    if any(word in q for word in ["clean", "missing", "duplicate", "quality"]):
        next_action = "Open Clean Data, review the Quality Center, and apply the most relevant cleanup action."
    elif any(word in q for word in ["chart", "visual", "graph"]):
        next_action = "Open Explore Charts and build or review a chart to validate the pattern visually."
    elif any(word in q for word in ["recruiter", "report", "summary"]):
        next_action = "Open Export Report and download the polished summary that fits your audience."
    elif any(word in q for word in ["group", "segment"]):
        next_action = "Use comparison charts and summary insights to explore meaningful groups in the dataset."
    else:
        next_action = "Review the top insight cards, then validate the strongest claim with a chart or clean-up action."
    return evidence[:6], next_action

def local_ai_answer(question, analysis):
    q = question.lower()
    insights = analysis["insights"]
    quality = analysis["quality"]
    roles = analysis["roles"]
    if any(word in q for word in ["clean", "missing", "quality", "duplicate"]):
        if quality["missing_pct"] > 0 or quality["duplicate_rows"] > 0:
            return (
                f"Data quality needs attention. Missing values: {quality['missing_pct']}%. "
                f"Duplicate rows: {quality['duplicate_rows']}. "
                "Start by standardizing nulls, trimming text, and removing duplicates in Clean Data."
            )
        return "Data quality looks strong overall. No major missingness or duplicate risk was detected."
    if any(word in q for word in ["insight", "important", "pattern", "summary"]):
        if insights:
            first = insights[0]
            return (
                f"The strongest current insight is: {first['title']}. "
                f"Evidence: {first['evidence']} "
                f"Why it matters: {first['why_it_matters']}"
            )
        return "No major insight stands out yet. Try checking the Data and Charts sections for better context."
    if any(word in q for word in ["chart", "visual", "graph"]):
        charts = analysis["suggested_charts"]
        if charts:
            best = charts[0]
            return f"The best first chart is '{best['title']}' because {best['reason']}"
        return "This dataset needs more usable numeric or categorical structure before chart suggestions become meaningful."
    if any(word in q for word in ["compare", "group", "segment"]):
        if roles["categorical"] and roles["numeric"]:
            return (
                f"A strong comparison path is to compare '{roles['numeric'][0]}' across '{roles['categorical'][0]}'. "
                "Use a comparison chart next to validate the difference visually."
            )
        return "Group comparison is limited because the dataset does not have a strong mix of categorical and numeric fields."
    return (
        f"This dataset looks like {analysis['dataset_type']} and the chosen mode is {analysis['mode']}. "
        "A good next step is to review the top insight cards, compare a few charts, then export a report."
    )

def ask_provider(question, analysis, answer_style="Simple"):
    provider = get_ai_provider()
    if provider == "Local Only":
        return local_ai_answer(question, analysis), "local", "Local Analyst Mode answered this question without an external API call."
    prompt = (
        "You are an analyst assistant. Answer only from the structured context below. "
        f"Write in a {answer_style.lower()} tone. Be concise, practical, and avoid unsupported claims. "
        "Use markdown bullet points where helpful. Finish cleanly and do not leave unfinished numbered items or headings.\n\n"
        f"CONTEXT:\n{build_ai_context(analysis)}\n\n"
        f"QUESTION:\n{question}"
    )
    ok, payload, answer = call_selected_provider(prompt, max_tokens=420, temperature=0.2)
    if not ok:
        raw_error = payload.get("error", "")
        try:
            err_obj = json.loads(raw_error) if isinstance(raw_error, str) and raw_error.strip().startswith("{") else None
            if err_obj and isinstance(err_obj, dict):
                code = err_obj.get("error", {}).get("code", "")
                msg  = err_obj.get("error", {}).get("message", "")
                status = err_obj.get("error", {}).get("status", "")
                if code == 503 or status == "UNAVAILABLE":
                    friendly = "Gemini is busy — answered locally instead."
                elif code == 429:
                    friendly = "Rate limit reached — answered locally instead."
                elif code in (401, 403):
                    friendly = "API key invalid or unauthorised — answered locally instead."
                elif msg:
                    friendly = msg[:120]
                else:
                    friendly = f"{provider} error (code {code}) — answered locally instead."
            else:
                friendly = str(raw_error)[:120] if raw_error else f"{provider} request failed — answered locally instead."
        except Exception:
            friendly = str(raw_error)[:120] if raw_error else f"{provider} request failed — answered locally instead."
        return local_ai_answer(question, analysis), "fallback", friendly
    answer = clean_ai_markdown(answer)
    if not answer:
        return local_ai_answer(question, analysis), "fallback", f"{provider} returned an empty response — answered locally instead."
    return answer, provider.lower(), f"{provider} · {get_ai_model_name()}"

# -----------------------------------------------------------------------------
# REPORTING
# -----------------------------------------------------------------------------
def get_pinned_insight_objects(analysis):
    pinned = []
    pinned_ids = set(st.session_state.pinned_insights)
    for insight in analysis["insights"]:
        if insight["id"] in pinned_ids:
            pinned.append(insight)
    return pinned

def generate_markdown_report(analysis, style="Analyst Report"):
    quality = analysis["quality"]
    roles = analysis["roles"]
    pinned = get_pinned_insight_objects(analysis)
    chosen_insights = pinned if pinned else analysis["insights"][:5]
    if style == "Student Report":
        intro = (
            f"This report explains the uploaded file **{analysis['filename']}** in simple terms. "
            f"The dataset has **{quality['rows']:,} rows** and **{quality['cols']} columns**. "
            "The focus is on easy-to-understand patterns, data quality, and next steps."
        )
    elif style == "Executive Summary":
        intro = (
            f"This executive summary highlights the main signals from **{analysis['filename']}**. "
            f"Overall quality score: **{analysis['quality_score']}%**. "
            f"Analysis mode used: **{analysis['mode']}**."
        )
    else:
        intro = (
            f"This analyst report summarizes **{analysis['filename']}** using an automated analysis workflow. "
            f"The dataset contains **{quality['rows']:,} rows** and **{quality['cols']} columns**, "
            f"with a quality score of **{analysis['quality_score']}%**."
        )
    top_corr = analysis["correlations"]["top_pairs"][:3]
    corr_section = "\n".join(
        [f"- {item['col_a']} vs {item['col_b']}: correlation = {item['corr']}" for item in top_corr]
    ) if top_corr else "- No strong numeric relationships detected."
    ai_notes = ""
    if st.session_state.ai_history:
        recent_ai = st.session_state.ai_history[-3:]
        blocks = []
        for item in recent_ai:
            blocks.append(f"**Q:** {item['question']}\n\n**A:** {item['answer']}")
        ai_notes = "\n\n## AI Q&A Highlights\n\n" + "\n\n---\n\n".join(blocks)
    report = f"""# Automated CSV Analyst Report

**File:** {analysis['filename']}  
**Generated:** {analysis['analysis_time']}  
**Dataset Type:** {analysis['dataset_type']}  
**Analysis Mode:** {analysis['mode']}  

---

## Overview

{intro}

### Schema Summary
- Numeric columns: {len(roles['numeric'])}
- Categorical columns: {len(roles['categorical'])}
- Datetime columns: {len(roles['datetime'])}
- ID-like columns: {len(roles['id_like'])}
- Text-like columns: {len(roles['text_like'])}

### Data Quality
- Missing values: {quality['missing_pct']}%
- Duplicate rows: {quality['duplicate_rows']}
- Constant columns: {len(quality['constant_cols'])}
- High-missing columns: {len(quality['high_missing_cols'])}

---

## Key Insights

{chr(10).join([f"{i+1}. **{item['title']}** — {item['evidence']} Action: {item['action']}" for i, item in enumerate(chosen_insights)])}

---

## Relationship Summary

{corr_section}

---

## Recommended Next Steps

1. Review the most important insight cards and validate them with charts.
2. Fix data quality issues before drawing final conclusions.
3. Compare performance across meaningful categories or time periods.
4. Export this report for sharing or presentation.

{ai_notes}

---

*Generated by Automated CSV Analyst*
"""
    return report

# -----------------------------------------------------------------------------
# UI HELPERS
# -----------------------------------------------------------------------------
def metric_row(analysis):
    quality = analysis["quality"]
    roles = analysis["roles"]
    # Two rows of metrics — stack gracefully on mobile via CSS
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Rows", f"{quality['rows']:,}")
    with col2:
        st.metric("Columns", quality["cols"])
    with col3:
        st.metric("Quality", f"{analysis['quality_score']}%")
    col4, col5, _ = st.columns(3)
    with col4:
        st.metric("Numeric cols", len(roles["numeric"]))
    with col5:
        st.metric("Missing", f"{quality['missing_pct']}%")

def render_insight_card(insight):
    pinned = insight["id"] in st.session_state.pinned_insights
    with st.container(border=True):
        top_left, top_right = st.columns([5, 1])
        with top_left:
            st.markdown(f"### {insight['title']}")
            st.caption(
                f"Type: {insight['type']} · Severity: {insight['severity'].title()} · "
                f"Confidence: {int(insight['confidence'] * 100)}%"
            )
        with top_right:
            if st.button("⭐" if pinned else "☆", key=f"pin_{insight['id']}", use_container_width=True):
                if pinned:
                    st.session_state.pinned_insights = [x for x in st.session_state.pinned_insights if x != insight["id"]]
                else:
                    st.session_state.pinned_insights.append(insight["id"])
                st.rerun()
        st.write(f"**Evidence:** {insight['evidence']}")
        st.write(f"**Why it matters:** {insight['why_it_matters']}")
        st.write(f"**Recommended action:** {insight['action']}")

def maybe_show_empty():
    st.info("Upload a CSV in Overview and click Analyze Dataset to begin.")

def build_suitability_hints(analysis):
    hints = []
    roles = analysis["roles"]
    quality = analysis["quality"]
    if len(roles["numeric"]) >= 2:
        hints.append("This dataset is a good fit for correlation analysis.")
    if roles["datetime"]:
        hints.append(f"Time-series analysis is available using {roles['datetime'][0]}.")
    if roles["categorical"] and roles["numeric"]:
        hints.append(f"Group comparison charts will work well using {roles['categorical'][0]} and {roles['numeric'][0]}.")
    if not hints:
        hints.append("Start with the Quality Center and basic charts before using advanced analysis.")
    return hints

def render_sidebar_ai_config():
    with st.sidebar:
        st.markdown("## AI Mode")
        st.selectbox("Choose provider", ["Local Only", "Gemini API"], key="ai_provider")
        provider = get_ai_provider()
        if provider == "Gemini API":
            st.text_input("Gemini API Key", key="gemini_api_key_input", type="password", placeholder="Paste your Gemini key")
            st.selectbox("Gemini Model", GEMINI_MODEL_OPTIONS, key="gemini_model_choice")
            with st.expander("How to get Gemini API key"):
                st.markdown("1. Open Google AI Studio\n2. Sign in with your Google account\n3. Open API Keys\n4. Create or copy your Gemini API key\n5. Paste it here")
            st.caption("Recommended: gemini-2.5-flash for fast, free-tier friendly answers.")
        test_label = "Test AI Connection" if provider != "Local Only" else "Check Local Mode"
        if st.button(test_label, use_container_width=True):
            ok, msg = test_ai_connection()
            st.session_state.ai_connection_ok = ok
            st.session_state.ai_connection_message = msg
        if st.session_state.get("ai_connection_message"):
            if st.session_state.get("ai_connection_ok"):
                st.success(st.session_state.get("ai_connection_message"))
            else:
                st.warning(st.session_state.get("ai_connection_message"))

# -----------------------------------------------------------------------------
# MAIN APP
# -----------------------------------------------------------------------------
def main():
    init_state()
    render_sidebar_ai_config()

    # ── Mobile-responsive CSS ─────────────────────────────────────────────────
    st.markdown("""
    <style>
    /* Stack all Streamlit column blocks on narrow screens */
    @media (max-width: 768px) {

        /* Force every column group to stack vertically */
        [data-testid="column"] {
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 100% !important;
        }

        /* Remove horizontal gap between stacked columns */
        [data-testid="stHorizontalBlock"] {
            flex-direction: column !important;
            gap: 0.5rem !important;
        }

        /* Tab bar: allow horizontal scroll instead of wrapping */
        [data-testid="stTabs"] > div:first-child {
            overflow-x: auto !important;
            white-space: nowrap !important;
            -webkit-overflow-scrolling: touch;
        }

        /* Tab buttons: shrink font so they fit */
        [data-testid="stTabs"] button {
            font-size: 12px !important;
            padding: 6px 10px !important;
        }

        /* Title: smaller on mobile */
        h1 { font-size: 1.4rem !important; }
        h2 { font-size: 1.15rem !important; }
        h3 { font-size: 1rem !important; }

        /* Metric cards: 2-across grid instead of 4-across */
        [data-testid="metric-container"] {
            min-width: 0 !important;
        }

        /* Sidebar: full width overlay (Streamlit default on mobile) */
        section[data-testid="stSidebar"] {
            width: 80vw !important;
            min-width: 260px !important;
        }

        /* Chat input: more thumb-friendly */
        [data-testid="stChatInput"] textarea {
            font-size: 16px !important;
        }

        /* Plotly charts: allow horizontal scroll on very small screens */
        .js-plotly-plot {
            overflow-x: auto !important;
        }

        /* Buttons: full width for easy tapping */
        [data-testid="stButton"] > button {
            width: 100% !important;
            min-height: 44px !important;
        }

        /* Download buttons: same */
        [data-testid="stDownloadButton"] > button {
            width: 100% !important;
            min-height: 44px !important;
        }

        /* File uploader: full width */
        [data-testid="stFileUploadDropzone"] {
            min-height: 80px !important;
        }

        /* Dataframes: horizontal scroll on overflow */
        [data-testid="stDataFrame"] {
            overflow-x: auto !important;
        }

        /* Expanders: slightly larger tap target */
        [data-testid="stExpander"] summary {
            min-height: 44px !important;
            display: flex !important;
            align-items: center !important;
        }

        /* Reduce page horizontal padding on mobile */
        .block-container {
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }

        /* Quick-prompt chip buttons in Ask AI */
        div[data-testid="stButton"] > button[kind="secondary"] {
            font-size: 13px !important;
        }
    }
    </style>
    """, unsafe_allow_html=True)

    st.title("📊 Automated CSV Analyst")
    st.caption("A Streamlit-based data analysis tool that helps you upload CSV files, clean data, explore visual insights, ask AI-powered questions, and export polished reports.")
    st.divider()

    tab_setup, tab_data, tab_clean, tab_insights, tab_ai, tab_report = st.tabs([
        "1️⃣ Overview",
        "2️⃣ Data Preview",
        "3️⃣ Clean Data",
        "4️⃣ Explore Charts",
        "5️⃣ Ask AI",
        "6️⃣ Export Report",
    ])

    # -------------------------------------------------------------------------
    # SETUP
    # -------------------------------------------------------------------------
    with tab_setup:
        st.subheader("Overview")

        st.markdown("### AI Configuration")
        st.caption("Choose your AI provider from the sidebar. The setup below shows the active connection status.")
        ai_status = get_ai_runtime_status()
        status_left, status_right = st.columns([1.25, 1])
        with status_left:
            st.info(f"**AI mode:** {ai_status['mode']}\n\n**Model:** {get_ai_model_name()}")
            if st.session_state.get("ai_connection_message"):
                if st.session_state.get("ai_connection_ok"):
                    st.success(st.session_state.get("ai_connection_message"))
                else:
                    st.warning(st.session_state.get("ai_connection_message"))
            else:
                st.caption(ai_status["reason"])
        with status_right:
            st.markdown("**Quick demo path**")
            st.markdown("1. Upload your CSV\n2. Review quality and apply fixes\n3. Explore charts and insights\n4. Ask AI questions and export your report")
        st.markdown("---")
        left, right = st.columns([2, 1])
        if st.button("Reset Session", use_container_width=True):
                for key in ["original_df", "working_df", "analysis", "current_file", "analysis_time", "transform_history", "pinned_insights", "ai_history", "latest_fix", "quick_fix_preview", "ai_report_md"]:
                    st.session_state[key] = None if key in {"original_df", "working_df", "analysis", "current_file", "analysis_time", "latest_fix", "quick_fix_preview", "ai_report_md"} else []
                st.session_state.user_goal = "Auto"
                st.success("Session reset. Upload a new file to start fresh.")
                st.rerun()
        with left:
            uploaded_file = st.file_uploader("Choose a CSV file", type=["csv"], help="Upload any CSV file to begin analysis.")
        with right:
            user_goal = st.selectbox("Analysis Goal", USER_GOALS, index=USER_GOALS.index(st.session_state.user_goal))
            st.session_state.user_goal = user_goal

        if uploaded_file is not None:
            new_file = st.session_state.current_file != uploaded_file.name or st.session_state.original_df is None
            if new_file:
                try:
                    df = read_csv_safely(uploaded_file)
                    st.session_state.original_df = clone_df(df)
                    st.session_state.working_df = clone_df(df)
                    st.session_state.current_file = uploaded_file.name
                    st.session_state.transform_history = []
                    st.session_state.pinned_insights = []
                    st.session_state.ai_history = []
                    st.session_state.latest_fix = None
                    st.session_state.quick_fix_preview = None
                    st.session_state.analysis = None
                    st.success(f"Loaded **{uploaded_file.name}** successfully.")
                except Exception as exc:
                    st.error(str(exc))

            if st.session_state.working_df is not None:
                preview_roles = infer_column_roles(st.session_state.working_df)
                preview_type = infer_dataset_type(st.session_state.working_df, preview_roles)
                preview_mode = choose_analysis_mode(st.session_state.user_goal, preview_type, preview_roles)
                preview_plan = build_analysis_plan(st.session_state.working_df, preview_roles, preview_mode)

                c1, c2, c3 = st.columns(3)
                with c1:
                    st.info(f"**Detected Dataset Type**\n\n{preview_type}")
                with c2:
                    st.info(f"**Suggested Mode**\n\n{preview_mode}")
                with c3:
                    st.info(f"**Planned Modules**\n\n{', '.join(preview_plan['modules'])}")

                if st.button("▶️ Analyze Dataset", use_container_width=True):
                    with st.spinner("Analyzing dataset..."):
                        st.session_state.analysis = run_full_analysis(
                            st.session_state.working_df,
                            st.session_state.current_file,
                            st.session_state.user_goal
                        )
                        st.session_state.analysis_time = datetime.now()
                    st.success("Analysis complete. Follow the demo path: Clean Data → Explore Charts → Ask AI → Export Report.")

        elif st.session_state.working_df is not None:
            st.info(f"Current file: **{st.session_state.current_file}**")
            if st.button("Clear File"):
                for key in ["original_df", "working_df", "analysis", "current_file", "analysis_time",
                            "transform_history", "pinned_insights", "ai_history", "latest_fix",
                            "quick_fix_preview"]:
                    if key in st.session_state:
                        st.session_state[key] = [] if key in ["transform_history", "pinned_insights", "ai_history"] else None
                st.session_state.user_goal = "Auto"
                st.rerun()

        if st.session_state.analysis is not None:
            st.markdown("---")
            metric_row(st.session_state.analysis)

    # -------------------------------------------------------------------------
    # DATA — IMPROVED with column drill-down
    # -------------------------------------------------------------------------
    with tab_data:
        if st.session_state.working_df is None:
            maybe_show_empty()
        else:
            df = st.session_state.working_df
            st.subheader("Data Preview")
            st.dataframe(df.head(MAX_PREVIEW_ROWS), use_container_width=True, height=420)

            st.subheader("Column Profile")
            info = pd.DataFrame({
                "Column": df.columns,
                "Type": [str(t) for t in df.dtypes.values],
                "Non-Null": df.count().values,
                "Null": df.isna().sum().values,
                "Null %": ((df.isna().sum() / len(df)) * 100).round(2).values if len(df) else 0,
                "Unique": df.nunique(dropna=True).values
            })
            st.dataframe(info, use_container_width=True, height=360)

            if st.session_state.analysis is not None:
                roles = st.session_state.analysis["roles"]
                st.markdown("### Inferred Roles")

                def _render_role_block(title, items):
                    st.write(f"**{title}**")
                    if items:
                        st.caption(", ".join(str(x) for x in items[:8]))
                    else:
                        st.caption("None detected")

                c1, c2, c3 = st.columns(3)
                with c1:
                    _render_role_block("Numeric", roles["numeric"])
                    _render_role_block("Datetime", roles["datetime"])
                with c2:
                    _render_role_block("Categorical", roles["categorical"])
                    _render_role_block("Boolean", roles["boolean"])
                with c3:
                    _render_role_block("ID-like", roles["id_like"])
                    _render_role_block("Text-like", roles["text_like"])

            # IMPROVED: Column-level drill-down
            st.markdown("---")
            st.subheader("🔎 Column Drill-Down")
            col_pick = st.selectbox("Select a column to explore", df.columns.tolist(), key="drilldown_col")
            if col_pick:
                s = df[col_pick]
                d1, d2, d3 = st.columns(3)
                d1.metric("Non-Null Count", int(s.notna().sum()))
                d2.metric("Null Count", int(s.isna().sum()))
                d3.metric("Unique Values", int(s.nunique(dropna=True)))

                if pd.api.types.is_numeric_dtype(s):
                    sn = pd.to_numeric(s, errors="coerce").dropna()
                    d4, d5 = st.columns(2)
                    d4.metric("Mean", f"{sn.mean():.3f}")
                    d5.metric("Median", f"{sn.median():.3f}")
                    d6, d7 = st.columns(2)
                    d6.metric("Std Dev", f"{sn.std():.3f}")
                    d7.metric("Skewness", f"{sn.skew():.3f}")

                    fig_dd = px.histogram(sn, x=col_pick, nbins=20, title=f"Distribution of {col_pick}",
                                         color_discrete_sequence=["#4472C4"], marginal="box")
                    fig_dd.update_layout(height=350, margin=dict(t=50, b=30, l=30, r=20))
                    show_chart_with_download(fig_dd, f"drilldown_{col_pick}_histogram")
                else:
                    vc = s.astype(str).value_counts().head(15).reset_index()
                    vc.columns = [col_pick, "Count"]
                    fig_dd = px.bar(vc, x=col_pick, y="Count", title=f"Top values in {col_pick}",
                                    color="Count", color_continuous_scale="Blues")
                    fig_dd.update_layout(height=350, margin=dict(t=50, b=30, l=30, r=20), coloraxis_showscale=False)
                    show_chart_with_download(fig_dd, f"drilldown_{col_pick}_bar")

    # -------------------------------------------------------------------------
    # CLEAN
    # -------------------------------------------------------------------------
    with tab_clean:
        if st.session_state.working_df is None:
            maybe_show_empty()
        else:
            df = st.session_state.working_df
            analysis = st.session_state.analysis or run_full_analysis(df, st.session_state.current_file, st.session_state.user_goal)

            st.subheader("Quality Center")
            metric_row(analysis)

            q = analysis["quality"]
            latest_fix = st.session_state.latest_fix or (
                st.session_state.transform_history[-1]["details"] if st.session_state.transform_history else None
            )
            col_a, col_b = st.columns([1.05, 1])

            with col_a:
                st.markdown("### Data quality findings")
                st.caption("Findings update automatically after each applied fix.")

                needs_attention = []
                observations = []

                if q["missing_pct"] > 0:
                    needs_attention.append(f"Missing values: {q['missing_pct']}%")
                if q["duplicate_rows"] > 0:
                    needs_attention.append(f"Duplicate rows: {q['duplicate_rows']}")
                if q["high_missing_cols"]:
                    observations.append(f"Sparse columns found: {len(q['high_missing_cols'])}")
                if q["constant_cols"]:
                    observations.append(f"Constant columns found: {len(q['constant_cols'])}")

                if not needs_attention and not observations:
                    st.success("Your dataset looks clean overall.")
                else:
                    if needs_attention:
                        st.markdown("**Needs attention**")
                        for item in needs_attention:
                            st.warning(item)
                    if observations:
                        st.markdown("**Observations**")
                        if q["constant_cols"]:
                            constant_detail = ", ".join(q["constant_cols"][:5])
                            st.info(
                                f"Constant columns found: {len(q['constant_cols'])}" + "\n\n" +
                                constant_detail + "\n\n" +
                                "These columns have the same value in every row, so they may add little analytical value."
                            )
                        elif q["high_missing_cols"]:
                            st.info(f"Sparse columns found: {', '.join(q['high_missing_cols'][:5])}")

                st.markdown("**Recommended next step**")
                if not needs_attention:
                    st.success("No major cleanup is needed. Continue to Visual Explorer or Ask AI.")
                else:
                    st.info("Start with the recommended cleanup on the right, then continue to charts or Ask AI.")

                if latest_fix:
                    st.markdown("### Recent change")
                    st.success(latest_fix["summary"])
                    meta_left, meta_right = st.columns(2)
                    meta_left.caption(f"Action: {latest_fix['action']}")
                    meta_right.caption(f"Time: {latest_fix['timestamp']}")
                    if latest_fix["notes"]:
                        with st.expander("View details"):
                            for note in latest_fix["notes"]:
                                st.caption(f"• {note}")

            with col_b:
                st.markdown("### Cleaning actions")
                st.caption("Use the recommended path first. Open manual tools only when you need more control.")

                st.markdown("**Recommended path**")
                preview_col, apply_col = st.columns(2)
                with preview_col:
                    if st.button("Preview Cleanup", use_container_width=True):
                        st.session_state.quick_fix_preview = build_quick_fix_preview(clone_df(st.session_state.working_df))
                with apply_col:
                    if st.button("Apply Recommended Cleanup", use_container_width=True, type="primary"):
                        before_df = clone_df(st.session_state.working_df)
                        after_df = apply_quick_fix_bundle(before_df)
                        record_transform("Quick Fix Recommended", before_df, after_df)
                        st.session_state.working_df = after_df
                        st.session_state.quick_fix_preview = None
                        rerun_analysis()
                        st.rerun()

                cleaned_csv = st.session_state.working_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "Download Cleaned Data",
                    data=cleaned_csv,
                    file_name=f"cleaned_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    use_container_width=True
                )

                if st.session_state.quick_fix_preview:
                    with st.expander("Preview of recommended cleanup", expanded=True):
                        st.markdown("\n".join(f"- {point}" for point in st.session_state.quick_fix_preview))

                st.markdown("**Manual tools**")
                with st.expander("Text cleanup"):
                    t1, t2 = st.columns(2)
                    with t1:
                        if st.button("Trim Text", use_container_width=True):
                            before_df = clone_df(st.session_state.working_df)
                            after_df = apply_trim_whitespace(before_df)
                            record_transform("Trim Text", before_df, after_df)
                            st.session_state.working_df = after_df
                            rerun_analysis()
                            st.rerun()
                    with t2:
                        if st.button("Standardize Nulls", use_container_width=True):
                            before_df = clone_df(st.session_state.working_df)
                            after_df = apply_standardize_nulls(before_df)
                            record_transform("Standardize Nulls", before_df, after_df)
                            st.session_state.working_df = after_df
                            rerun_analysis()
                            st.rerun()
                    st.caption("Clean text fields and turn NA/null-like entries into proper missing values.")

                with st.expander("Structure cleanup"):
                    s1, s2 = st.columns(2)
                    with s1:
                        if st.button("Remove Duplicates", use_container_width=True):
                            before_df = clone_df(st.session_state.working_df)
                            after_df = apply_remove_duplicates(before_df)
                            record_transform("Remove Duplicates", before_df, after_df)
                            st.session_state.working_df = after_df
                            rerun_analysis()
                            st.rerun()
                    with s2:
                        if st.button("Convert Number-like Text", use_container_width=True):
                            before_df = clone_df(st.session_state.working_df)
                            after_df = apply_convert_numeric_like(before_df)
                            record_transform("Convert Numeric-like", before_df, after_df)
                            st.session_state.working_df = after_df
                            rerun_analysis()
                            st.rerun()
                    d1, d2 = st.columns(2)
                    with d1:
                        if st.button("Detect Date Columns", use_container_width=True):
                            before_df = clone_df(st.session_state.working_df)
                            after_df = apply_infer_dates(before_df)
                            record_transform("Infer Dates", before_df, after_df)
                            st.session_state.working_df = after_df
                            rerun_analysis()
                            st.rerun()
                    with d2:
                        st.empty()
                    st.caption("Use these when numbers or dates are stored as text, or when duplicate rows need removal.")

                with st.expander("Missing data"):
                    m1, m2 = st.columns(2)
                    with m1:
                        if st.button("Fill Empty Cells", use_container_width=True):
                            before_df = clone_df(st.session_state.working_df)
                            after_df = apply_fill_missing(before_df)
                            record_transform("Fill Missing Values", before_df, after_df)
                            st.session_state.working_df = after_df
                            rerun_analysis()
                            st.rerun()
                    with m2:
                        if st.button("Drop Sparse Columns", use_container_width=True):
                            before_df = clone_df(st.session_state.working_df)
                            after_df, dropped = apply_drop_high_missing(before_df, threshold=0.5)
                            record_transform("Drop High-Missing Columns", before_df, after_df, extra={"dropped": dropped})
                            st.session_state.working_df = after_df
                            rerun_analysis()
                            if dropped:
                                st.toast(f"Dropped: {', '.join(dropped[:5])}", icon="🗑️")
                            st.rerun()
                    st.caption("Fill gaps automatically or remove columns where too much data is missing.")

                with st.expander("What do these tools do?"):
                    st.caption("Trim Text — removes extra spaces from text columns.")
                    st.caption("Standardize Nulls — turns values like NA, null, or blanks into proper missing values.")
                    st.caption("Remove Duplicates — deletes repeated rows so results are not counted twice.")
                    st.caption("Convert Number-like Text — turns number-like text such as '123' into real numeric values.")
                    st.caption("Detect Date Columns — identifies text columns that should behave like dates.")
                    st.caption("Fill Empty Cells — fills missing values using simple sensible rules.")
                    st.caption("Drop Sparse Columns — removes columns where more than half the values are missing.")

            if latest_fix:
                st.markdown("---")
                m1, m2 = st.columns(2)
                m1.metric("Rows", latest_fix["after"]["rows"], latest_fix["after"]["rows"] - latest_fix["before"]["rows"])
                m2.metric("Missing Cells", latest_fix["after"]["missing_cells"], latest_fix["after"]["missing_cells"] - latest_fix["before"]["missing_cells"])
                m3, m4 = st.columns(2)
                m3.metric("Duplicate Rows", latest_fix["after"]["duplicate_rows"], latest_fix["after"]["duplicate_rows"] - latest_fix["before"]["duplicate_rows"])
                m4.metric("Columns", latest_fix["after"]["columns"], latest_fix["after"]["columns"] - latest_fix["before"]["columns"])
                if latest_fix["column_rows"]:
                    st.markdown("#### Changed Columns")
                    st.dataframe(pd.DataFrame(latest_fix["column_rows"]), use_container_width=True, height=220)
                preview_left, preview_right = st.columns(2)
                with preview_left:
                    st.markdown("#### Before")
                    if not latest_fix["preview_before"].empty:
                        st.dataframe(latest_fix["preview_before"], use_container_width=True, height=220)
                    else:
                        st.caption("No preview available.")
                with preview_right:
                    st.markdown("#### After")
                    if not latest_fix["preview_after"].empty:
                        st.dataframe(latest_fix["preview_after"], use_container_width=True, height=220)
                    else:
                        st.caption("No preview available.")

            st.markdown("---")
            hist_left, hist_right = st.columns([1, 3])
            with hist_left:
                st.markdown("### Transform History")
            with hist_right:
                if st.session_state.transform_history and st.button("Undo Last Step"):
                    last = st.session_state.transform_history.pop()
                    st.session_state.working_df = last["before_df"]
                    st.session_state.latest_fix = st.session_state.transform_history[-1]["details"] if st.session_state.transform_history else None
                    rerun_analysis()
                    st.rerun()

            if st.session_state.transform_history:
                for idx, item in enumerate(reversed(st.session_state.transform_history[-6:]), start=1):
                    details = item["details"]
                    with st.container(border=True):
                        st.markdown(f"**{details['action']}**")
                        st.caption(f"Step {len(st.session_state.transform_history) - idx + 1} · {details['timestamp']}")
                        st.write(details["summary"])
                        if details["affected_columns"]:
                            st.caption("Affected columns: " + ", ".join(details["affected_columns"][:8]))
            else:
                st.caption("No cleaning steps applied yet.")

    # -------------------------------------------------------------------------
    # INSIGHTS — chart exploration and key visuals
    # -------------------------------------------------------------------------
    with tab_insights:
        if st.session_state.analysis is None:
            maybe_show_empty()
        else:
            analysis = st.session_state.analysis
            df = st.session_state.working_df

            st.subheader("Explore Charts & Insights")
            metric_row(analysis)

            left, right = st.columns([1.35, 1])

            with left:
                st.markdown("### Insight Cards")
                if not analysis["insights"]:
                    st.info("No major insights were generated for this dataset.")
                else:
                    for insight in analysis["insights"]:
                        render_insight_card(insight)
                        st.markdown("")

            with right:
                st.markdown("### Suggested Charts")
                for idx, spec in enumerate(analysis["suggested_charts"]):
                    with st.container(border=True):
                        st.write(f"**{spec['title']}**")
                        st.caption(spec["reason"])
                        if st.button("Show Chart", key=f"suggest_chart_{idx}", use_container_width=True):
                            fig = render_chart_plotly(df, spec)
                            show_chart_with_download(fig, spec["title"])

                # Correlation heatmap shortcut
                if len(analysis["roles"]["numeric"]) >= 2:
                    with st.container(border=True):
                        st.write("**Correlation Heatmap**")
                        st.caption("Shows relationships between all numeric columns.")
                        if st.button("Show Heatmap", key="corr_heatmap_btn", use_container_width=True):
                            fig = render_chart_plotly(df, {
                                "kind": "correlation_heatmap",
                                "title": "Correlation Heatmap",
                                "corr_data": analysis["correlations"]
                            })
                            show_chart_with_download(fig, "correlation_heatmap")

                st.markdown("### Chart Builder")
                roles = analysis["roles"]
                chart_kind = st.selectbox(
                    "Chart Type",
                    ["category_bar", "histogram", "scatter", "group_bar", "box_plot", "time_line"],
                    format_func=lambda x: x.replace("_", " ").title()
                )
                x_options = list(df.columns)
                y_options = [""] + list(df.columns)
                selected_x = st.selectbox("X / Primary Column", x_options)
                selected_y = st.selectbox("Y / Secondary Column", y_options)

                if st.button("Build Chart", use_container_width=True):
                    spec = {
                        "title": f"{chart_kind.replace('_', ' ').title()}",
                        "kind": chart_kind,
                        "x": selected_x,
                        "y": selected_y or None
                    }
                    fig = render_chart_plotly(df, spec)
                    show_chart_with_download(fig, spec["title"])

            # ------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # ASK AI  —  Chatbot window design
    # -------------------------------------------------------------------------
    with tab_ai:
        if st.session_state.analysis is None:
            maybe_show_empty()
        else:
            analysis = st.session_state.analysis
            ai_status = get_ai_runtime_status()

            # ── Top status bar ────────────────────────────────────────────────
            status_a, status_b = st.columns(2)
            status_a.metric("AI Mode", ai_status["mode"])
            status_b.metric("Dataset Type", analysis["dataset_type"])
            status_c, status_d = st.columns(2)
            status_c.metric("Analysis Mode", analysis["mode"])
            status_d.metric("Rows / Columns", f"{len(st.session_state.working_df):,} / {len(st.session_state.working_df.columns)}")

            # ── Inline controls row ───────────────────────────────────────────
            ctrl_l, ctrl_r = st.columns(2)
            with ctrl_l:
                st.selectbox("Response Style", ["Simple", "Analyst", "Executive-report"], key="answer_style")
                model_label = get_gemini_model() or "Local Only"
                st.text_input("Model", value=model_label, disabled=True)
            with ctrl_r:
                key_label = "✅ API key found" if ai_status["token_ready"] else "⚠️ No API key"
                st.text_input("API Key Status", value=key_label, disabled=True)

            if ai_status["mode"] == "Gemini API":
                st.success(ai_status["reason"])
            else:
                st.warning(ai_status["reason"])

            # ── Quick-prompt chips ────────────────────────────────────────────
            prompts = [
                "What is the most important insight here?",
                "What should I clean first?",
                "Which chart should I use next?",
                "How can I compare groups in this dataset?",
                "How would you explain this dataset simply?",
                "What are the major risks in this data?",
            ]
            chip_cols = st.columns(2)
            for i, prompt in enumerate(prompts):
                with chip_cols[i % 2]:
                    if st.button(prompt, key=f"chip_{i}", use_container_width=True):
                        st.session_state["_pending_question"] = prompt

            st.divider()

            # ── Process any pending question BEFORE rendering history ─────────
            # This ensures the full answer is in history before any chat bubble
            # is drawn, so nothing is ever shown half-finished.
            pending = st.session_state.pop("_pending_question", None)
            if pending:
                with st.spinner("Thinking…"):
                    _answer, _source, _reason = ask_provider(
                        pending, analysis,
                        st.session_state.get("answer_style", "Analyst")
                    )
                    _evidence, _next_action = build_answer_support(pending, analysis)
                st.session_state.ai_history.append({
                    "question": pending,
                    "answer": _answer,
                    "source": _source,
                    "reason": _reason,
                    "evidence": _evidence,
                    "next_action": _next_action,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })
                st.session_state.last_question = pending
                st.rerun()

            # ── Render full chat history from session state ───────────────────
            for item in st.session_state.ai_history:
                with st.chat_message("user"):
                    st.markdown(item["question"])
                with st.chat_message("assistant"):
                    st.markdown(clean_ai_markdown(item["answer"]))
                    with st.expander("Why this answer · Next action", expanded=False):
                        exp_l, exp_r = st.columns(2)
                        with exp_l:
                            st.markdown("**Evidence used**")
                            for ev in item.get("evidence", []):
                                st.caption(f"• {ev}")
                        with exp_r:
                            st.markdown("**Suggested next action**")
                            st.info(item.get("next_action", "—"))
                    _src = item["source"]
                    if _src == "gemini api":
                        src_badge = "🟢 Gemini API"
                    elif _src == "fallback":
                        src_badge = "🟡 Local fallback"
                    else:
                        src_badge = "⚪ Local"
                    st.caption(f"🕐 {item['time']}  ·  {src_badge}")
                    _reason = item.get("reason", "")
                    if _reason and _src == "fallback":
                        st.caption(f"ℹ️ {_reason}")

            # ── Chat input box (typed messages) ───────────────────────────────
            user_input = st.chat_input("Ask about insights, data quality, charts, or anything else…")
            if user_input and user_input.strip():
                with st.spinner("Thinking…"):
                    _answer, _source, _reason = ask_provider(
                        user_input.strip(), analysis,
                        st.session_state.get("answer_style", "Analyst")
                    )
                    _evidence, _next_action = build_answer_support(user_input.strip(), analysis)
                st.session_state.ai_history.append({
                    "question": user_input.strip(),
                    "answer": _answer,
                    "source": _source,
                    "reason": _reason,
                    "evidence": _evidence,
                    "next_action": _next_action,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })
                st.session_state.last_question = user_input.strip()
                st.rerun()

            # ── Clear conversation button ─────────────────────────────────────
            if st.session_state.ai_history:
                if st.button("🗑️ Clear conversation", key="clear_chat"):
                    st.session_state.ai_history = []
                    st.rerun()

    # -------------------------------------------------------------------------
    # REPORT
    # -------------------------------------------------------------------------
    with tab_report:
        if st.session_state.analysis is None:
            maybe_show_empty()
        else:
            analysis = st.session_state.analysis
            st.subheader("Export Report")

            left, right = st.columns([1, 2])
            with left:
                style = st.selectbox("Report Style", REPORT_STYLES, index=1)
                report_engine = st.radio("Report Mode", ["Structured Report", "Gemini Summary"], index=0)
                pinned_count = len(get_pinned_insight_objects(analysis))
                st.info(f"Pinned insights: {pinned_count}")
                st.caption("If no insights are pinned, the top insights are used automatically.")

                if report_engine == "Gemini Summary":
                    if st.button("Generate Gemini Summary", use_container_width=True):
                        ai_report, error = generate_ai_report(analysis, style=style)
                        if error:
                            st.session_state.ai_report_md = ""
                            st.warning(error)
                        else:
                            st.session_state.ai_report_md = ai_report
                            st.success("AI report generated successfully.")

            local_report_md = generate_markdown_report(analysis, style=style)
            report_md = st.session_state.ai_report_md if report_engine == "Gemini Summary" and st.session_state.ai_report_md else local_report_md

            with right:
                st.markdown("### Preview")
                preview_tab1, preview_tab2 = st.tabs(["Rendered", "Markdown"])
                with preview_tab1:
                    st.markdown(report_md)
                with preview_tab2:
                    st.text_area("Markdown Preview", value=report_md, height=420)

            st.download_button(
                "📥 Download Markdown Report",
                data=report_md,
                file_name=f"automated_csv_analyst_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                mime="text/markdown",
                use_container_width=True
            )

    st.divider()
    st.caption("Automated CSV Analyst · Designed & Built By Tarun P")

if __name__ == "__main__":
    main()
