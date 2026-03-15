#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eco2mix_curtailment_risk_pipeline.py
===================================

Goal (what problem this code helps with)
----------------------------------------
We want to answer (with only eco2mix time-series data):

  "When are wind/solar most likely to be curtailed ('écrêtés') because the system
   cannot absorb them (grid saturation, low demand, low flexibility)?"

eco2mix (conso/production/exchanges) does NOT directly contain the official
curtailment variable for 2024, so we build a *curtailment-risk proxy*:

  - Identify "high VRE" windows in France using simple, interpretable rules
    (PV1/W1/PV2/W2), inspired by your original scripts.
  - Combine them into a targeted union of time steps ("union_hours").
  - Use the same union_hours to:
      (i) quantify how much wind+solar energy happens in those risky windows,
     (ii) allocate a *national* curtailment number (e.g., 4 TWh/yr) to a region
          like Hauts-de-France based on wind+solar energy share in union_hours.
  - Additionally, for the region we compute a second proxy:
      export_mw = - (Ech. physiques)  (positive means net export out of the region).
    Very large export is an indicator of "the region is pushing power out" and may
    be close to local / interconnection limits, hence higher curtailment risk.

What you get at the end
-----------------------
1) Clean "true national" time-series (France file aggregated across all regions).
2) Thresholds:
   - Wind P75 (national)
   - Solar P75 (national, only daylight 08:00–17:59 and solar>0)
   - PV2 daily solar energy P80 (Apr–Sep)
   - W2 daily mean wind power P80 (all year)
3) Windows (defined on national data):
   - PV1: Apr–Sep AND 10:00–16:59 AND solar >= P75_solar
   - W1 : (weekend OR night 22:00–06:59) AND wind >= P75_wind
   - PV2: "sunny spells" = consecutive >=2 days with daily solar energy >= P80
   - W2 : "windy spells" = consecutive >=2 days with daily mean wind >= P80
   - union_hours_targeted = PV1 ∪ W1 ∪ (W2 all-day) ∪ (PV2 only 08:00–17:59)
4) Event statistics:
   - For PV1/W1 we report event blocks (continuous half-hour segments).
   - For PV2/W2 we report multi-day spell blocks (continuous day segments).
5) Energy accounting:
   - France total wind+solar energy, and wind+solar energy within union_hours
   - Hauts-de-France total wind+solar energy, and within union_hours
   - Shares sB (full-year) and sC (union_hours)
6) Candidate "saturation risk" timestamps for the region using export proxy:
   - export >= P99(export)
   - export >= max_export - margin (default margin=100 MW)

Outputs
-------
In an ./outputs folder (beside this script), you will get:
- summary.txt                     : all printed results saved
- union_hours_fr_defined.csv       : the targeted timestamps (dt_key list)
- pv1_w1_event_blocks.csv          : PV1/W1 continuous blocks in hours
- pv2_w2_spell_blocks.csv          : PV2/W2 continuous day spells
- pv2_w2_event_days.csv            : PV2 & W2 day flags (bool per day)
- energy_shares.csv                : sB, sC and energy totals
- region_export_risk_points.csv    : candidate "very high export" timestamps (region)

Usage
-----
Put this script in the same folder as:
  - eco2mix-France-cons-def.csv
  - eco2mix-regional-cons-def.csv   (Hauts-de-France)

Then run:
  python eco2mix_curtailment_risk_pipeline.py

Notes on DST (summer time)
-------------------------
eco2mix timestamps contain timezone offsets (+01:00 / +02:00). In 2024 there are:
- exact duplicate rows around the DST change (same offset), and
- missing half-hours for the "other" 02:00–02:30 repetition in October.
This script:
- keeps the full timestamp string (including offset) as the unique key (dt_key),
- drops exact duplicate rows before any aggregation,
- reports the number of duplicates and missing/irregular time steps it detects.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

import numpy as np
import pandas as pd


# ----------------------------
# User-adjustable constants
# ----------------------------

FR_FILE_STEM = "eco2mix-France-cons-def.csv"
REG_FILE_STEM = "eco2mix-regional-cons-def.csv"

DT_COL = "Date - Heure"
REGION_COL = "Région"

# Columns we care about (MW)
CONS_COL = "Consommation (MW)"
WIND_COL = "Eolien (MW)"
SOLAR_COL = "Solaire (MW)"
EXCH_COL = "Ech. physiques (MW)"  # "physical exchanges"

# We'll define export_mw = -EXCH_COL so that positive = net export outward.
# (This sign convention is purely for our proxy, not an official RTE definition.)

SPRING_SUMMER_MONTHS = {4, 5, 6, 7, 8, 9}

# PV1 definition (same spirit as your script)
PV1_START_HOUR = 10
PV1_END_HOUR_EXCLUSIVE = 16   # matches your original code: 10:00–15:59 (hour < 16)

# Solar daylight filter used to compute P75 solar (same as your script)
DAYLIGHT_START_HOUR = 8
DAYLIGHT_END_HOUR_EXCLUSIVE = 18  # 08:00–17:59

# W1 "night" definition (same spirit as your script)
NIGHT_START_HOUR = 22
NIGHT_END_HOUR_EXCLUSIVE = 6

# Export extreme threshold options (region-level proxy)
EXPORT_PCTL = 99
EXPORT_NEAR_MAX_MARGIN_MW = 100


# ----------------------------
# Helper: find files next to script
# ----------------------------
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
OUTPUT_DIR = PROJECT_DIR / "outputs_eco2mix_curtailment_risk"

def find_local_file(filename: str) -> str:  # find file in common folders
    p = Path(filename)  # parse path
    if p.exists():  # if user gave a valid relative/absolute path
        return str(p.resolve())  # return resolved path

    candidates = [  # candidate locations
        DATA_DIR / filename,  # data/filename
        OUTPUT_DIR / filename,  # outputs/filename
        SCRIPT_DIR / filename,  # scripts/filename
        PROJECT_DIR / filename,  # project root
    ]
    for c in candidates:  # iterate candidates
        if c.exists():  # if found
            return str(c.resolve())  # return resolved

    raise FileNotFoundError(  # raise with helpful info
        f"Cannot find '{filename}'.\n"
        f"Looked in:\n"
        f"  - {DATA_DIR / filename}\n"
        f"  - {OUTPUT_DIR / filename}\n"
        f"  - {SCRIPT_DIR / filename}\n"
        f"  - {PROJECT_DIR / filename}\n"
    )

# Data prep utilities
# ----------------------------

def _to_numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    """Convert selected columns to numeric (float), safely."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def add_time_features(df: pd.DataFrame, dt_col: str = DT_COL) -> pd.DataFrame:
    """
    Add features derived from the timestamp.

    We keep two parallel representations:
    - dt_key  : the full string (including +01:00/+02:00), used for unique matching.
    - dt_local: naive local datetime parsed from first 19 characters (YYYY-MM-DDTHH:MM:SS),
                used for hour/month/day/weekend logic.

    Why this design?
    - DST in eco2mix can create ambiguity if we drop timezone.
    - We want to *avoid merging* different real instants that share the same local clock time.
    """
    df = df.copy()

    # Full, unique key (safe for matching between files)
    df["dt_key"] = df[dt_col].astype(str).str.strip()

    # Parse local (naive) datetime for time-of-day logic (hour, weekday, etc.)
    s19 = df[dt_col].astype(str).str.slice(0, 19).str.replace("T", " ", regex=False)
    df["dt_local"] = pd.to_datetime(s19, format="%Y-%m-%d %H:%M:%S", errors="coerce")

    df["date"] = df["dt_local"].dt.date
    df["month"] = df["dt_local"].dt.month
    df["hour"] = df["dt_local"].dt.hour
    df["weekday"] = df["dt_local"].dt.weekday  # Mon=0 ... Sun=6
    df["is_weekend"] = df["weekday"] >= 5

    # Night = 22:00–23:59 OR 00:00–06:59
    df["is_night"] = (df["hour"] >= NIGHT_START_HOUR) | (df["hour"] < NIGHT_END_HOUR_EXCLUSIVE)

    return df


def drop_exact_duplicates(df: pd.DataFrame, subset: List[str]) -> Tuple[pd.DataFrame, int]:
    """
    Drop exact duplicates. We do this BEFORE aggregation to avoid double-counting energy.

    Returns (df_dedup, n_duplicates_removed)
    """
    before = len(df)
    df2 = df.drop_duplicates(subset=subset, keep="first")
    removed = before - len(df2)
    return df2, removed


def report_time_gaps(dt_local: pd.Series) -> Dict[str, int]:
    """
    Diagnose missing or irregular time steps in a 30-min series.

    We assume:
    - typical step = 30 minutes,
    but DST and data issues can create duplicates or missing steps.

    Return a dict with counts of observed deltas.
    """
    dt_local = dt_local.dropna().sort_values()
    deltas = dt_local.diff().dropna().dt.total_seconds() / 60.0  # minutes

    rounded = deltas.round().astype(int)  # round to nearest minute
    return rounded.value_counts().to_dict()


@dataclass
class Eco2mixSeries:
    """A container for a prepared eco2mix time series (already aggregated if needed)."""
    name: str
    df: pd.DataFrame   # must contain dt_key, dt_local, date, month, hour, is_weekend, is_night, ...
    step_h: float      # assumed integration step (hours) for energy conversion


def read_and_prepare_eco2mix(
    path: str,
    name: str,
    aggregate_regions_to_national: bool,
    region_filter: Optional[str] = None,
) -> Eco2mixSeries:
    """
    Read an eco2mix CSV and prepare it for analysis.

    Parameters
    ----------
    aggregate_regions_to_national:
        - True for the France file (it contains 12 regions; we aggregate to national totals).
        - False for a single region file (already one region).

    region_filter:
        If the file contains multiple regions and you only want one, set it here.

    Returns
    -------
    Eco2mixSeries with:
      - cleaned numeric columns,
      - duplicates removed,
      - optional region aggregation,
      - time features added,
      - step_h set to 0.5 (eco2mix is 30 min).
    """
    df = pd.read_csv(path, sep=";", encoding="utf-8-sig", low_memory=False)

    # Convert important MW columns to numeric
    df = _to_numeric(df, [CONS_COL, WIND_COL, SOLAR_COL, EXCH_COL])

    # Optional: filter to one region if the file contains multiple regions
    if region_filter is not None and REGION_COL in df.columns:
        df = df[df[REGION_COL].astype(str) == region_filter].copy()

    # Drop exact duplicates (common around DST)
    if REGION_COL in df.columns and not aggregate_regions_to_national:
        # For a regional file with a region column: duplicates should be unique on timestamp
        df, ndup = drop_exact_duplicates(df, subset=[DT_COL])
    elif REGION_COL in df.columns and aggregate_regions_to_national:
        # For the France file (multiple regions): duplicates should be unique per (region, timestamp)
        df, ndup = drop_exact_duplicates(df, subset=[REGION_COL, DT_COL])
    else:
        df, ndup = drop_exact_duplicates(df, subset=[DT_COL])

    # Aggregate across regions if requested (France file)
    if aggregate_regions_to_national:
        if REGION_COL not in df.columns:
            raise KeyError(
                f"{name}: expected a region column '{REGION_COL}' to aggregate, but it is missing."
            )

        # Sum across regions for each time step (national total)
        # min_count=1 prevents "all-NaN" -> 0 artifacts
        sum_cols = [CONS_COL, WIND_COL, SOLAR_COL, EXCH_COL]
        df = df.groupby([DT_COL], as_index=False)[sum_cols].sum(min_count=1)

    # Add time features (dt_key, dt_local, hour, month, etc.)
    df = add_time_features(df, dt_col=DT_COL)

    # Step length (eco2mix is 30 min). We keep 0.5h for integration.
    step_h = 0.5

    # Diagnostics: time gaps in dt_local
    df.attrs["duplicates_removed"] = ndup
    df.attrs["gap_counts_minutes"] = report_time_gaps(df["dt_local"])

    return Eco2mixSeries(name=name, df=df, step_h=step_h)


# ----------------------------
# Percentiles and event detection
# ----------------------------

def percentile_inc(values: pd.Series, p: float) -> float:
    """
    Excel-like PERCENTILE.INC (linear interpolation, inclusive endpoints).
    Here p is in [0, 100].
    """
    arr = np.asarray(values.dropna(), dtype=float)
    if arr.size == 0:
        raise ValueError("Empty series for percentile.")
    return float(np.percentile(arr, p, method="linear"))


def find_consecutive_day_blocks(days: List[pd.Timestamp], min_len: int = 2) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """
    Given a list of days, find consecutive blocks of length >= min_len.

    Returns: list of (start_day, end_day), inclusive.
    """
    if not days:
        return []
    days = sorted(pd.to_datetime(days).normalize().unique())

    blocks: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    start = days[0]
    prev = days[0]
    for d in days[1:]:
        if (d - prev).days == 1:
            prev = d
        else:
            if (prev - start).days + 1 >= min_len:
                blocks.append((start, prev))
            start = d
            prev = d
    if (prev - start).days + 1 >= min_len:
        blocks.append((start, prev))
    return blocks


def find_consecutive_time_blocks(df: pd.DataFrame, mask: pd.Series, step_minutes: int = 30) -> List[Dict[str, object]]:
    """
    Identify continuous time blocks where mask is True.

    A block is continuous if the time difference between consecutive points is exactly step_minutes.

    Returns a list of dicts:
      - start_dt_key, end_dt_key
      - start_dt_local, end_dt_local
      - duration_hours
      - n_points
      - unique_days_count
    """
    if mask.sum() == 0:
        return []

    # Work on a sorted copy (by local time)
    sub = df.loc[mask, ["dt_key", "dt_local", "date"]].dropna().sort_values("dt_local").reset_index(drop=True)
    if sub.empty:
        return []

    blocks: List[Dict[str, object]] = []
    start_idx = 0

    # compute diffs to detect breaks
    diffs_min = sub["dt_local"].diff().dt.total_seconds().div(60).fillna(step_minutes)

    for i in range(1, len(sub)):
        if int(round(diffs_min.iloc[i])) != step_minutes:
            # close the block [start_idx, i-1]
            blk = sub.iloc[start_idx:i]
            blocks.append({
                "start_dt_key": blk["dt_key"].iloc[0],
                "end_dt_key": blk["dt_key"].iloc[-1],
                "start_dt_local": blk["dt_local"].iloc[0],
                "end_dt_local": blk["dt_local"].iloc[-1],
                "n_points": len(blk),
                "duration_hours": len(blk) * (step_minutes / 60.0),
                "unique_days_count": blk["date"].nunique(),
            })
            start_idx = i

    # final block
    blk = sub.iloc[start_idx:]
    blocks.append({
        "start_dt_key": blk["dt_key"].iloc[0],
        "end_dt_key": blk["dt_key"].iloc[-1],
        "start_dt_local": blk["dt_local"].iloc[0],
        "end_dt_local": blk["dt_local"].iloc[-1],
        "n_points": len(blk),
        "duration_hours": len(blk) * (step_minutes / 60.0),
        "unique_days_count": blk["date"].nunique(),
    })

    return blocks


@dataclass
class WindowDefinition:
    """All thresholds and identified windows based on national data."""
    p75_wind_mw: float
    p75_solar_mw: float
    pv2_daily_solar_p80_mwh: float
    w2_daily_mean_wind_p80_mw: float

    # hourly masks (dt_key sets)
    PV1_hours: Set[str]
    W1_hours: Set[str]

    # day sets
    PV2_days: Set[pd.Timestamp]
    W2_days: Set[pd.Timestamp]

    # blocks/spells (for reporting)
    PV2_blocks: List[Tuple[pd.Timestamp, pd.Timestamp]]
    W2_blocks: List[Tuple[pd.Timestamp, pd.Timestamp]]
    PV1_blocks: List[Dict[str, object]]
    W1_blocks: List[Dict[str, object]]

    union_hours: Set[str]


def build_windows_from_national(fr: Eco2mixSeries) -> WindowDefinition:
    """
    Build PV1/W1/PV2/W2 windows using the national aggregated series.

    This mirrors your original intent, but with correct national aggregation
    and safe timestamp keys.
    """
    df = fr.df.copy()
    step_h = fr.step_h

    # --- Thresholds
    p75_wind = percentile_inc(df[WIND_COL], 75)

    # Solar P75 is computed on daylight (08-18) and solar>0 (same as your script)
    solar_day = df.loc[
        (df["hour"] >= DAYLIGHT_START_HOUR) &
        (df["hour"] < DAYLIGHT_END_HOUR_EXCLUSIVE) &
        (df[SOLAR_COL] > 0),
        SOLAR_COL
    ]
    p75_solar = percentile_inc(solar_day, 75)

    # --- PV1 hours (Apr–Sep, 10:00–16:59, solar >= P75_solar)
    pv1_mask = (
        df["month"].isin(SPRING_SUMMER_MONTHS)
        & (df["hour"] >= PV1_START_HOUR)
        & (df["hour"] < PV1_END_HOUR_EXCLUSIVE)
        & (df[SOLAR_COL] >= p75_solar)
    )
    PV1_hours = set(df.loc[pv1_mask, "dt_key"].tolist())

    # --- W1 hours (wind >= P75_wind AND (weekend OR night))
    w1_mask = (df[WIND_COL] >= p75_wind) & (df["is_weekend"] | df["is_night"])
    W1_hours = set(df.loc[w1_mask, "dt_key"].tolist())

    # Continuous blocks (in half-hour steps) for PV1/W1
    PV1_blocks = find_consecutive_time_blocks(df, pv1_mask, step_minutes=30)
    W1_blocks = find_consecutive_time_blocks(df, w1_mask, step_minutes=30)

    # --- PV2: daily solar energy (MWh/day) in Apr–Sep
    df_ss = df[df["month"].isin(SPRING_SUMMER_MONTHS)].copy()
    daily_solar_mwh = df_ss.groupby("date")[SOLAR_COL].sum() * step_h
    pv2_thr = percentile_inc(daily_solar_mwh, 80)

    high_pv2_days = daily_solar_mwh[daily_solar_mwh >= pv2_thr].index
    high_pv2_days_ts = [pd.Timestamp(d) for d in high_pv2_days]
    PV2_blocks = find_consecutive_day_blocks(high_pv2_days_ts, min_len=2)

    PV2_days: Set[pd.Timestamp] = set()
    for start, end in PV2_blocks:
        cur = start
        while cur <= end:
            PV2_days.add(cur.normalize())
            cur += pd.Timedelta(days=1)

    # --- W2: daily mean wind power (MW) all year
    daily_wind_mean = df.groupby("date")[WIND_COL].mean()
    w2_thr = percentile_inc(daily_wind_mean, 80)

    high_w2_days = daily_wind_mean[daily_wind_mean >= w2_thr].index
    high_w2_days_ts = [pd.Timestamp(d) for d in high_w2_days]
    W2_blocks = find_consecutive_day_blocks(high_w2_days_ts, min_len=2)

    W2_days: Set[pd.Timestamp] = set()
    for start, end in W2_blocks:
        cur = start
        while cur <= end:
            W2_days.add(cur.normalize())
            cur += pd.Timedelta(days=1)

    # --- union_hours_targeted:
    #     PV1 ∪ W1 ∪ (W2 all-day) ∪ (PV2 only daylight 08:00–17:59)
    union_hours: Set[str] = set(PV1_hours) | set(W1_hours)

    # Add W2 all-day points
    w2_mask_all = df["date"].apply(lambda d: pd.Timestamp(d).normalize() in W2_days)
    union_hours |= set(df.loc[w2_mask_all, "dt_key"].tolist())

    # Add PV2 daylight points only
    pv2_mask_daylight = (
        df["date"].apply(lambda d: pd.Timestamp(d).normalize() in PV2_days)
        & (df["hour"] >= DAYLIGHT_START_HOUR)
        & (df["hour"] < DAYLIGHT_END_HOUR_EXCLUSIVE)
    )
    union_hours |= set(df.loc[pv2_mask_daylight, "dt_key"].tolist())

    return WindowDefinition(
        p75_wind_mw=p75_wind,
        p75_solar_mw=p75_solar,
        pv2_daily_solar_p80_mwh=pv2_thr,
        w2_daily_mean_wind_p80_mw=w2_thr,
        PV1_hours=PV1_hours,
        W1_hours=W1_hours,
        PV2_days=PV2_days,
        W2_days=W2_days,
        PV2_blocks=PV2_blocks,
        W2_blocks=W2_blocks,
        PV1_blocks=PV1_blocks,
        W1_blocks=W1_blocks,
        union_hours=union_hours,
    )


# ----------------------------
# Energy accounting utilities
# ----------------------------

def energy_mwh(df: pd.DataFrame, col_mw: str, step_h: float, mask: Optional[pd.Series] = None) -> float:
    """Convert a MW time-series into energy (MWh) using constant step_h."""
    if col_mw not in df.columns:
        raise KeyError(f"Missing column: {col_mw}")
    s = df[col_mw] if mask is None else df.loc[mask, col_mw]
    return float(np.nansum(s.values) * step_h)


def wind_solar_energy_stats(series: Eco2mixSeries, union_hours: Set[str]) -> Dict[str, float]:
    """
    Compute total wind+solar energy and wind+solar energy inside union_hours.
    Return energies in MWh and TWh (for convenience).
    """
    df = series.df
    step_h = series.step_h

    wind_total_mwh = energy_mwh(df, WIND_COL, step_h)
    solar_total_mwh = energy_mwh(df, SOLAR_COL, step_h)
    ws_total_mwh = wind_total_mwh + solar_total_mwh

    in_union = df["dt_key"].isin(union_hours)
    wind_union_mwh = energy_mwh(df, WIND_COL, step_h, mask=in_union)
    solar_union_mwh = energy_mwh(df, SOLAR_COL, step_h, mask=in_union)
    ws_union_mwh = wind_union_mwh + solar_union_mwh

    return {
        "wind_total_mwh": wind_total_mwh,
        "solar_total_mwh": solar_total_mwh,
        "ws_total_mwh": ws_total_mwh,
        "wind_union_mwh": wind_union_mwh,
        "solar_union_mwh": solar_union_mwh,
        "ws_union_mwh": ws_union_mwh,
        "wind_total_twh": wind_total_mwh / 1e6,
        "solar_total_twh": solar_total_mwh / 1e6,
        "ws_total_twh": ws_total_mwh / 1e6,
        "wind_union_twh": wind_union_mwh / 1e6,
        "solar_union_twh": solar_union_mwh / 1e6,
        "ws_union_twh": ws_union_mwh / 1e6,
    }


# ----------------------------
# Region export proxy (curtailment-risk signal)
# ----------------------------

def compute_region_export_risk_points(series: Eco2mixSeries, union_hours: Set[str]) -> pd.DataFrame:
    """
    Build a table of candidate "high export" points for a region, as a proxy for saturation.

    export_mw = - Ech. physiques (MW)
    - Very large export means the region is strongly pushing power out.
      If the exporting corridor is near its limit, curtailment becomes more likely.

    We return points satisfying:
      export >= P99(export)   OR   export >= max(export) - margin

    And we add whether each point is in union_hours.
    """
    df = series.df.copy()

    if EXCH_COL not in df.columns:
        raise KeyError(f"Missing exchange column: {EXCH_COL}")

    df["export_mw"] = -df[EXCH_COL]
    export = df["export_mw"].dropna()

    if export.empty:
        return pd.DataFrame()

    thr_pctl = np.percentile(export.values, EXPORT_PCTL, method="linear")
    max_export = float(export.max())
    thr_near_max = max_export - EXPORT_NEAR_MAX_MARGIN_MW

    mask = (df["export_mw"] >= thr_pctl) | (df["export_mw"] >= thr_near_max)
    out = df.loc[mask, ["dt_key", "dt_local", "export_mw", WIND_COL, SOLAR_COL, CONS_COL, EXCH_COL]].copy()
    out["in_union_hours"] = out["dt_key"].isin(union_hours)
    out = out.sort_values("export_mw", ascending=False).reset_index(drop=True)

    # Attach thresholds in metadata
    out.attrs["export_pctl_threshold_mw"] = float(thr_pctl)
    out.attrs["export_max_mw"] = max_export
    out.attrs["export_near_max_threshold_mw"] = float(thr_near_max)

    return out


# ----------------------------
# Reporting / saving outputs
# ----------------------------

def ensure_output_dir() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(base_dir, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def save_text(out_dir: str, filename: str, text: str) -> None:
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
        f.write(text)


def _block_stats(blocks: List[Dict[str, object]]) -> Dict[str, float]:
    """Compute basic statistics for a list of time blocks."""
    if not blocks:
        return {"n_blocks": 0, "avg_hours": 0.0, "median_hours": 0.0, "max_hours": 0.0}
    durations = np.array([b["duration_hours"] for b in blocks], dtype=float)
    return {
        "n_blocks": int(len(blocks)),
        "avg_hours": float(durations.mean()),
        "median_hours": float(np.median(durations)),
        "max_hours": float(durations.max()),
    }


def _spell_stats(spells: List[Tuple[pd.Timestamp, pd.Timestamp]]) -> Dict[str, float]:
    """Compute basic stats for multi-day spells (start,end inclusive)."""
    if not spells:
        return {"n_spells": 0, "avg_days": 0.0, "median_days": 0.0, "max_days": 0.0}
    lengths = np.array([(end - start).days + 1 for start, end in spells], dtype=float)
    return {
        "n_spells": int(len(spells)),
        "avg_days": float(lengths.mean()),
        "median_days": float(np.median(lengths)),
        "max_days": float(lengths.max()),
    }


def main() -> int:
    # ----------------------------
    # 1) Load + prepare data
    # ----------------------------
    fr_path = find_local_file(FR_FILE_STEM)
    reg_path = find_local_file(REG_FILE_STEM)

    # France: aggregate regions -> national total series
    fr = read_and_prepare_eco2mix(fr_path, name="France (aggregated)", aggregate_regions_to_national=True)

    # Region: in your dataset it's already Hauts-de-France, but we keep it generic
    reg_name = "Hauts-de-France"
    reg = read_and_prepare_eco2mix(reg_path, name=reg_name, aggregate_regions_to_national=False)

    # ----------------------------
    # 2) Build PV/W windows from national data
    # ----------------------------
    windows = build_windows_from_national(fr)

    # ----------------------------
    # 3) Compute energy + shares
    # ----------------------------
    fr_stats = wind_solar_energy_stats(fr, windows.union_hours)
    reg_stats = wind_solar_energy_stats(reg, windows.union_hours)

    # Shares:
    # sB: full-year share of region in France wind+solar energy
    # sC: share of region in France wind+solar energy *within union_hours*
    sB = reg_stats["ws_total_mwh"] / fr_stats["ws_total_mwh"] if fr_stats["ws_total_mwh"] > 0 else np.nan
    sC = reg_stats["ws_union_mwh"] / fr_stats["ws_union_mwh"] if fr_stats["ws_union_mwh"] > 0 else np.nan

    # ----------------------------
    # 4) Region export proxy points (optional but very informative)
    # ----------------------------
    export_points = compute_region_export_risk_points(reg, windows.union_hours)

    # ----------------------------
    # 5) Build a human-readable report
    # ----------------------------
    lines: List[str] = []
    lines.append("=== INPUT FILES ===")
    lines.append(f"France file  : {fr_path}")
    lines.append(f"Region file  : {reg_path}")
    lines.append("")

    lines.append("=== DATA QUALITY CHECKS (DST / duplicates / gaps) ===")
    lines.append(f"[France] duplicates removed before aggregation: {fr.df.attrs.get('duplicates_removed', 0)}")
    lines.append(f"[France] observed time-step deltas (minutes): {fr.df.attrs.get('gap_counts_minutes')}")
    lines.append(f"[Region] duplicates removed: {reg.df.attrs.get('duplicates_removed', 0)}")
    lines.append(f"[Region] observed time-step deltas (minutes): {reg.df.attrs.get('gap_counts_minutes')}")
    lines.append("Note: eco2mix is a 30-min series. DST can create duplicates and missing slots.")
    lines.append("")

    lines.append("=== NATIONAL THRESHOLDS (computed on aggregated France series) ===")
    lines.append(f"Wind P75 (MW)                           : {windows.p75_wind_mw:,.1f}")
    lines.append(f"Solar P75 (MW) [08:00–17:59 & solar>0]  : {windows.p75_solar_mw:,.1f}")
    lines.append(f"PV2 daily solar energy P80 (MWh/day)     : {windows.pv2_daily_solar_p80_mwh:,.1f}")
    lines.append(f"W2 daily mean wind P80 (MW)             : {windows.w2_daily_mean_wind_p80_mw:,.1f}")
    lines.append("")

    fr_step_h = fr.step_h
    lines.append("=== WINDOW COVERAGE (defined by FRANCE) ===")
    lines.append(f"PV1 hours (h/year)   : {len(windows.PV1_hours) * fr_step_h:,.1f}")
    lines.append(f"W1 hours  (h/year)   : {len(windows.W1_hours) * fr_step_h:,.1f}")
    lines.append(f"PV2 event-days       : {len(windows.PV2_days)}")
    lines.append(f"W2 event-days        : {len(windows.W2_days)}")
    lines.append(f"Union hours (h/year) : {len(windows.union_hours) * fr_step_h:,.1f}")
    lines.append("")

    lines.append("=== EVENT STATISTICS (continuous blocks) ===")
    pv1_bs = _block_stats(windows.PV1_blocks)
    w1_bs = _block_stats(windows.W1_blocks)
    pv2_ss = _spell_stats(windows.PV2_blocks)
    w2_ss = _spell_stats(windows.W2_blocks)
    lines.append(f"PV1 blocks: n={pv1_bs['n_blocks']}, avg={pv1_bs['avg_hours']:.2f}h, median={pv1_bs['median_hours']:.2f}h, max={pv1_bs['max_hours']:.2f}h")
    lines.append(f"W1  blocks: n={w1_bs['n_blocks']}, avg={w1_bs['avg_hours']:.2f}h, median={w1_bs['median_hours']:.2f}h, max={w1_bs['max_hours']:.2f}h")
    lines.append(f"PV2 spells: n={pv2_ss['n_spells']}, avg={pv2_ss['avg_days']:.2f}d, median={pv2_ss['median_days']:.2f}d, max={pv2_ss['max_days']:.2f}d")
    lines.append(f"W2  spells: n={w2_ss['n_spells']}, avg={w2_ss['avg_days']:.2f}d, median={w2_ss['median_days']:.2f}d, max={w2_ss['max_days']:.2f}d")
    lines.append("")

    lines.append("=== ENERGY ACCOUNTING (wind+solar) ===")
    lines.append(f"[France] wind (TWh)           : {fr_stats['wind_total_twh']:.3f}")
    lines.append(f"[France] solar (TWh)          : {fr_stats['solar_total_twh']:.3f}")
    lines.append(f"[France] wind+solar (TWh)     : {fr_stats['ws_total_twh']:.3f}")
    lines.append(f"[France] in union (TWh)       : {fr_stats['ws_union_twh']:.3f}")
    lines.append("")
    lines.append(f"[{reg_name}] wind (TWh)       : {reg_stats['wind_total_twh']:.3f}")
    lines.append(f"[{reg_name}] solar (TWh)      : {reg_stats['solar_total_twh']:.3f}")
    lines.append(f"[{reg_name}] wind+solar (TWh) : {reg_stats['ws_total_twh']:.3f}")
    lines.append(f"[{reg_name}] in union (TWh)   : {reg_stats['ws_union_twh']:.3f}")
    lines.append("")
    lines.append("Shares (region / France):")
    lines.append(f"sB = full-year share                   : {sB*100:.2f}%")
    lines.append(f"sC = union-hours (risk windows) share  : {sC*100:.2f}%")
    lines.append("Interpretation: if you allocate a national curtailment number by energy share,")
    lines.append("               sC is often more realistic than sB because it focuses on risky windows.")
    lines.append("")

    lines.append("=== REGION EXPORT PROXY (saturation / curtailment risk signal) ===")
    if export_points.empty:
        lines.append("No export points computed (missing exchanges or all NaN).")
    else:
        thr_p99 = export_points.attrs.get("export_pctl_threshold_mw", np.nan)
        max_export = export_points.attrs.get("export_max_mw", np.nan)
        thr_nearmax = export_points.attrs.get("export_near_max_threshold_mw", np.nan)
        lines.append(f"export_mw = -{EXCH_COL}")
        lines.append(f"Threshold export P{EXPORT_PCTL} (MW)                    : {thr_p99:,.1f}")
        lines.append(f"Max export (MW)                                        : {max_export:,.1f}")
        lines.append(f"Near-max threshold (MW) = max - {EXPORT_NEAR_MAX_MARGIN_MW}       : {thr_nearmax:,.1f}")
        lines.append(f"Candidate points found                                 : {len(export_points)}")
        lines.append(f"Of which in union_hours                                : {int(export_points['in_union_hours'].sum())}")
        lines.append("")
        lines.append("Top 10 export points (most extreme):")
        top10 = export_points.head(10)
        for _, r in top10.iterrows():
            lines.append(
                f"  {r['dt_key']} | export={r['export_mw']:.0f} MW"
                f" | wind={r[WIND_COL]:.0f} MW | solar={r[SOLAR_COL]:.0f} MW"
                f" | conso={r[CONS_COL]:.0f} MW | in_union={bool(r['in_union_hours'])}"
            )

    lines.append("")
    lines.append("=== FINAL EXPLANATION (what we are doing end-to-end) ===")
    lines.append(
        "1) We load eco2mix-France and eco2mix-regional (Hauts-de-France) at 30-min resolution.\n"
        "   The France file is NOT a national total: it contains one row per region per timestamp.\n"
        "   We therefore (i) drop exact duplicate rows (DST), then (ii) sum all regions by timestamp\n"
        "   to obtain the true national time-series.\n"
        "\n"
        "2) Using the national time-series, we define *high-risk windows* where curtailment is more likely:\n"
        "   - PV1: strong solar during spring/summer mid-day\n"
        "   - W1 : strong wind during nights/weekends (typically low demand)\n"
        "   - PV2: multi-day sunny spells (>=2 consecutive high-solar days)\n"
        "   - W2 : multi-day windy spells (>=2 consecutive high-wind days)\n"
        "   Then we merge them into a targeted union_hours set (timestamps).\n"
        "\n"
        "3) We compute wind+solar energy (TWh) in:\n"
        "   - the full year, and\n"
        "   - the targeted union_hours.\n"
        "   This gives two shares:\n"
        "   - sB: Hauts-de-France share of national wind+solar over the whole year\n"
        "   - sC: Hauts-de-France share of national wind+solar inside the risky windows\n"
        "   If you want to allocate a national curtailment number (e.g., 4 TWh in 2025) to the region,\n"
        "   sC is usually a better allocator because it focuses exactly on the time steps where the\n"
        "   system is under the most stress.\n"
        "\n"
        "4) Finally, for the region we compute export_mw = -Ech. physiques (MW).\n"
        "   Very high export indicates the region is pushing power outward; if export approaches a corridor\n"
        "   limit, curtailment becomes more likely. We output the timestamps of extreme export (P99 or near-max)\n"
        "   and mark whether they are inside union_hours.\n"
    )

    report = "\n".join(lines)

    # ----------------------------
    # 6) Save outputs to ./outputs
    # ----------------------------
    out_dir = ensure_output_dir()

    save_text(out_dir, "summary.txt", report)

    pd.DataFrame({"dt_key": sorted(windows.union_hours)}).to_csv(
        os.path.join(out_dir, "union_hours_fr_defined.csv"),
        index=False,
        encoding="utf-8"
    )

    # PV1/W1 block table
    blk_rows = []
    for b in windows.PV1_blocks:
        blk_rows.append({"type": "PV1", **b})
    for b in windows.W1_blocks:
        blk_rows.append({"type": "W1", **b})
    pd.DataFrame(blk_rows).to_csv(os.path.join(out_dir, "pv1_w1_event_blocks.csv"), index=False, encoding="utf-8")

    # PV2/W2 spell blocks
    spell_rows = []
    for start, end in windows.PV2_blocks:
        spell_rows.append({"type": "PV2", "start_day": start.date(), "end_day": end.date(), "duration_days": (end-start).days+1})
    for start, end in windows.W2_blocks:
        spell_rows.append({"type": "W2", "start_day": start.date(), "end_day": end.date(), "duration_days": (end-start).days+1})
    pd.DataFrame(spell_rows).to_csv(os.path.join(out_dir, "pv2_w2_spell_blocks.csv"), index=False, encoding="utf-8")

    # PV2/W2 event-day flags
    all_days = sorted(set(pd.to_datetime(fr.df["date"]).dropna().dt.normalize().unique()))
    day_df = pd.DataFrame({"day": all_days})
    day_df["PV2_day"] = day_df["day"].isin(windows.PV2_days)
    day_df["W2_day"] = day_df["day"].isin(windows.W2_days)
    day_df.to_csv(os.path.join(out_dir, "pv2_w2_event_days.csv"), index=False, encoding="utf-8")

    # Energy shares
    shares_df = pd.DataFrame([{
        "region": reg_name,
        "fr_wind_twh": fr_stats["wind_total_twh"],
        "fr_solar_twh": fr_stats["solar_total_twh"],
        "fr_ws_twh": fr_stats["ws_total_twh"],
        "fr_ws_union_twh": fr_stats["ws_union_twh"],
        "region_wind_twh": reg_stats["wind_total_twh"],
        "region_solar_twh": reg_stats["solar_total_twh"],
        "region_ws_twh": reg_stats["ws_total_twh"],
        "region_ws_union_twh": reg_stats["ws_union_twh"],
        "sB_full_year_share": sB,
        "sC_union_hours_share": sC,
        "union_hours_hours_per_year": len(windows.union_hours) * fr_step_h,
    }])
    shares_df.to_csv(os.path.join(out_dir, "energy_shares.csv"), index=False, encoding="utf-8")

    # Region export risk points
    if not export_points.empty:
        export_points.to_csv(os.path.join(out_dir, "region_export_risk_points.csv"), index=False, encoding="utf-8")

    # Print report to console as well
    print(report)
    print(f"\n[Saved outputs to] {out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
