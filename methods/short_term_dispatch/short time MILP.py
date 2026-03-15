#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
short_time_absorption_milp_final.py

Short-term MILP dispatch (30-min) for renewable curtailment absorption
with three co-existing flexibility assets:
- BESS (battery)
- Power-to-Heat (electric heater + hot-water tank TES) with HEAT DEMAND + DISCHARGE
- Power-to-H2 (electrolyzer + H2 storage + H2 sales)

Key assumptions:
- NO grid import/export (no electricity buying/selling).
- Curtailment availability for the PROJECT is absorb_need_mw (alpha-scaled).
- We also preserve REGION curtailment curve as absorb_need_mw_region (unscaled).
- Objective uses time-varying H2 sale revenue + penalties/costs, and minimizes unused curtailment.
- Heat demand is an exogenous time series heat_demand_mw_th (MW_th).
  TES can discharge to serve demand; unmet demand is allowed via slack with penalty.

NEW (Engineering electrolyzer constraints):
- Ramp rate limit (engineering version: allow big jump at start/stop via big-M term)
- Min up time / min down time (in STEPS)
- Start/stop costs already included (proxy for degradation)

NEW (Feasibility verification):
- Optional strict 100% curtailment absorption check:
  enforce P_curt_use_mw == absorb_need_mw and Unused == 0 and no power-balance slack
  -> if solver returns Optimal/Feasible, then 100% absorption is feasible under constraints.

NEW (Alpha scenarios):
- Run multiple alpha (accessible share) scenarios in one run.
- For each alpha, create sub-folder alpha_<tag>/ and save dispatch + kpi summary.
- Also write alpha_kpi_summary.csv at outdir root.

Solver: PuLP + GUROBI (via gurobipy)
- This script will try to use PuLP's GUROBI interface first.
- If GUROBI is not available in your PuLP build, it will raise an error.

Author: Yuqi HAN
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pulp


# =========================
# Utilities
# =========================

def _detect_csv_sep(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        line = f.readline()
    return ";" if line.count(";") > line.count(",") else ","


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def timestamp_run_id() -> str:
    return pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")


def parse_alphas(alpha_str: str) -> List[float]:
    """
    Parse comma/space separated alphas, e.g. "0.005,0.0066,0.01"
    Returns sorted unique list.
    """
    if not alpha_str:
        raise ValueError("Empty --alphas string")

    raw: List[float] = []
    for tok in alpha_str.replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        raw.append(float(tok))

    if not raw:
        raise ValueError("No valid alpha values found in --alphas")

    alphas = sorted(set(raw))
    for a in alphas:
        if not (0.0 < a <= 1.0):
            raise ValueError(f"alpha must be in (0,1], got {a}")
    return alphas


def alpha_tag(alpha: float) -> str:
    """
    Convert alpha to a filesystem-friendly tag:
    0.005 -> '0p005', 0.0066 -> '0p0066', 0.01 -> '0p01', 1.0 -> '1'
    """
    s = f"{alpha:.6f}".rstrip("0").rstrip(".")
    return s.replace(".", "p")


def _pick_first_existing(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    for c in names:
        if c in df.columns:
            return c
    return None


def _tz_localize_safe(dt: pd.Series, tz: str) -> pd.Series:
    """
    Localize naive datetimes to tz, robustly handling DST.
    """
    try:
        return dt.dt.tz_localize(tz, ambiguous="infer", nonexistent="shift_forward")
    except Exception:
        return dt.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")


def _parse_dt_series_to_tz(s: pd.Series, tz: str) -> pd.Series:
    """
    Robust datetime parsing for mixed tz-aware + naive strings.

    - tz-aware rows: parse with utc=True then convert to tz
    - naive rows: parse as local time in tz (DST-safe), convert to UTC then back to tz
    """
    s2 = s.astype(str).str.strip()
    tz_mask = s2.str.contains(r"(?:Z|[+-]\d{2}:?\d{2}|UTC|CET|CEST)$", case=False, regex=True)

    out = pd.Series(pd.NaT, index=s2.index, dtype="datetime64[ns, UTC]")

    if tz_mask.any():
        dt_aware_utc = pd.to_datetime(s2[tz_mask], errors="coerce", utc=True)
        out.loc[tz_mask] = dt_aware_utc

    if (~tz_mask).any():
        dt_naive = pd.to_datetime(s2[~tz_mask], errors="coerce")
        dt_local = _tz_localize_safe(dt_naive, tz)
        out.loc[~tz_mask] = dt_local.dt.tz_convert("UTC")

    return out.dt.tz_convert(tz)


def find_curve_csv(user_path: Optional[str], base_dir: Path) -> Path:
    """
    Robust resolver for region_absorb_need_curve_30min.csv
    """
    if user_path:
        p = Path(user_path)
        if p.is_absolute() and p.exists():
            return p
        cand = (base_dir / p).resolve()
        if cand.exists():
            return cand

    c1 = base_dir / "region_absorb_need_curve_30min.csv"
    if c1.exists():
        return c1.resolve()

    hits = list(base_dir.rglob("region_absorb_need_curve_30min.csv"))
    if hits:
        hits.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return hits[0].resolve()

    raise FileNotFoundError(
        "Cannot locate region_absorb_need_curve_30min.csv. Provide --curve_csv or place it in working dir."
    )


def find_prices_csv(user_path: Optional[str], base_dir: Path) -> Optional[Path]:
    if user_path:
        p = Path(user_path)
        if p.is_absolute() and p.exists():
            return p
        cand = (base_dir / p).resolve()
        if cand.exists():
            return cand

    patterns = ["prices_*.csv", "*h2*price*.csv", "*price*.csv"]
    hits: List[Path] = []
    for pat in patterns:
        hits.extend(list(base_dir.glob(pat)))
    if hits:
        hits.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return hits[0].resolve()

    hits = []
    for pat in patterns:
        hits.extend(list(base_dir.rglob(pat)))
    if hits:
        hits.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return hits[0].resolve()

    return None


# =========================
# Read inputs
# =========================

def read_curve_csv_30min(curve_csv: Path, tz: str = "Europe/Paris") -> pd.DataFrame:
    sep = _detect_csv_sep(curve_csv)
    df = pd.read_csv(curve_csv, sep=sep, low_memory=False)
    df.columns = [c.strip() for c in df.columns]

    dt_col = _pick_first_existing(df, ["dt", "Date - Heure", "Datetime", "Datetime (Local)", "timestamp", "time"])
    if dt_col is None:
        raise ValueError(f"[curve] Missing datetime column in {curve_csv}. Columns={list(df.columns)[:50]}")

    absorb_col = _pick_first_existing(df, ["absorb_need_mw_region", "absorb_need_mw", "AbsorbNeedMW", "absorb_mw"])
    if absorb_col is None:
        raise ValueError(
            f"[curve] Missing absorb need column in {curve_csv}. "
            f"Expected absorb_need_mw_region (preferred) or absorb_need_mw."
        )

    df["dt"] = _parse_dt_series_to_tz(df[dt_col], tz)
    df = df.loc[~df["dt"].isna()].copy()

    df[absorb_col] = pd.to_numeric(df[absorb_col], errors="coerce").fillna(0.0)

    # optional heat demand column (MW_th)
    heat_col = _pick_first_existing(df, ["heat_demand_mw_th", "heat_demand", "D_th_mw_th"])
    if heat_col is not None:
        df[heat_col] = pd.to_numeric(df[heat_col], errors="coerce").fillna(0.0)
        df["heat_demand_mw_th"] = df[heat_col]
    else:
        df["heat_demand_mw_th"] = 0.0

    out = (
        df[["dt", absorb_col, "heat_demand_mw_th"]]
        .rename(columns={absorb_col: "absorb_need_mw"})
        .sort_values("dt")
        .reset_index(drop=True)
    )

    return out


def read_h2_prices(prices_csv: Path, tz: str) -> pd.DataFrame:
    sep = _detect_csv_sep(prices_csv)
    dfp = pd.read_csv(prices_csv, sep=sep, low_memory=False)
    dfp.columns = [c.strip() for c in dfp.columns]

    dt_col = _pick_first_existing(dfp, ["dt", "Datetime (Local)", "Datetime", "Date - Heure", "timestamp", "time"])
    if dt_col is None:
        raise ValueError(f"[prices] Missing datetime column in {prices_csv}. Columns={list(dfp.columns)[:50]}")

    h2_col = _pick_first_existing(dfp, ["h2_price_eur_per_kg", "H2_price_eur_per_kg", "h2_sell_eur_per_kg"])
    if h2_col is None:
        raise ValueError(f"[prices] Missing H2 price column in {prices_csv}. Columns={list(dfp.columns)[:50]}")

    dfp["dt"] = _parse_dt_series_to_tz(dfp[dt_col], tz)
    dfp = dfp.loc[~dfp["dt"].isna()].copy()
    dfp["h2_price_eur_per_kg"] = pd.to_numeric(dfp[h2_col], errors="coerce")

    dfp = dfp.sort_values("dt").reset_index(drop=True)
    dfp["h2_price_eur_per_kg"] = dfp["h2_price_eur_per_kg"].ffill().bfill()

    return dfp[["dt", "h2_price_eur_per_kg"]]


def merge_curve_with_h2_price(curve_df: pd.DataFrame, price_df: pd.DataFrame, tolerance_min: int = 15) -> pd.DataFrame:
    a = curve_df[["dt", "absorb_need_mw", "heat_demand_mw_th"]].copy()
    b = price_df.sort_values("dt").reset_index(drop=True).copy()

    tol = pd.Timedelta(minutes=int(tolerance_min))
    merged = pd.merge_asof(a, b, on="dt", direction="nearest", tolerance=tol)

    if merged["h2_price_eur_per_kg"].isna().any():
        miss = int(merged["h2_price_eur_per_kg"].isna().sum())
        print(f"[merge][WARN] Missing H2 prices after merge_asof(tol={tolerance_min}min): {miss} rows. Using ffill/bfill.", flush=True)
        merged["h2_price_eur_per_kg"] = merged["h2_price_eur_per_kg"].ffill().bfill()

    return merged


# =========================
# Model params
# =========================

@dataclass(frozen=True)
class BatteryParams:
    power_mw: float
    energy_mwh: float
    eta_ch: float
    eta_dis: float
    soc_init_mwh: float
    soc_min_frac: float
    soc_max_frac: float


@dataclass(frozen=True)
class ThermalParams:
    heater_cap_mw: float
    storage_cap_mwh_th: float
    eta_p2h: float
    discharge_cap_mw_th: float
    loss_frac_per_hour: float
    soc_init_mwh_th: float
    soc_min_frac: float
    soc_max_frac: float


@dataclass(frozen=True)
class ElectrolyzerParams:
    cap_mw: float
    min_load_frac: float
    specific_energy_mwh_per_kg: float  # MWh_e per kg H2
    var_om_eur_per_mwh: float
    start_cost_eur: float
    stop_cost_eur: float
    ramp_frac_per_step: float     # max delta(P) per step as fraction of cap_mw
    min_up_steps: int             # min consecutive ON steps after a start
    min_down_steps: int           # min consecutive OFF steps after a stop


@dataclass(frozen=True)
class H2StorageParams:
    cap_kg: float
    soc_init_kg: float
    soc_min_frac: float
    soc_max_frac: float


@dataclass(frozen=True)
class EconomicParams:
    dt_h: float
    penalty_unused_eur_per_mwh: float
    penalty_power_balance_slack_eur_per_mwh: float
    penalty_spill_eur_per_mwh: float
    h2_price_default_eur_per_kg: float
    penalty_unmet_heat_eur_per_mwh_th: float


@dataclass(frozen=True)
class SolveParams:
    rolling_horizon_hours: float
    overlap_hours: float
    time_limit_s: int
    mip_gap: float
    threads: int
    cbc_path: Optional[str]


def _cbc_solver(msg: bool, time_limit_s: int, gap: float, threads: int, cbc_path: Optional[str]):
    """
    Kept name for compatibility; returns a GUROBI solver via PuLP.
    """
    if not hasattr(pulp, "GUROBI"):
        raise RuntimeError(
            "PuLP does not expose GUROBI() interface in this environment. "
            "Please ensure PuLP has GUROBI support, and gurobipy is installed and licensed."
        )

    try:
        return pulp.GUROBI(msg=msg, timeLimit=time_limit_s, gapRel=gap, threads=threads)
    except TypeError:
        pass

    try:
        return pulp.GUROBI(
            msg=msg,
            timeLimit=time_limit_s,
            options=[
                ("MIPGap", gap),
                ("Threads", threads),
            ],
        )
    except TypeError:
        pass

    solver = pulp.GUROBI(msg=msg)
    try:
        solver.options = [
            ("TimeLimit", time_limit_s),
            ("MIPGap", gap),
            ("Threads", threads),
        ]
    except Exception:
        pass
    return solver


# =========================
# MILP window solve
# =========================

def build_and_solve_window(
    dt_index: pd.DatetimeIndex,
    absorb_need_mw: np.ndarray,           # PROJECT (alpha-scaled)
    absorb_need_mw_region: np.ndarray,    # REGION (unscaled, for reporting)
    heat_demand_mw_th: np.ndarray,
    h2_price: np.ndarray,
    batt: BatteryParams,
    th: ThermalParams,
    el: ElectrolyzerParams,
    h2s: H2StorageParams,
    econ: EconomicParams,
    solve: SolveParams,
    state_init: Dict[str, float],
    msg: bool = False,
    verify_full_absorption: bool = False,
) -> Tuple[pd.DataFrame, Dict[str, float], str]:

    n = len(dt_index)
    T = list(range(n))
    dt_h = econ.dt_h

    # Initial states
    soc_b0 = float(state_init.get("batt_soc_mwh", batt.soc_init_mwh))
    soc_th0 = float(state_init.get("th_soc_mwh_th", th.soc_init_mwh_th))
    soc_h20 = float(state_init.get("h2_soc_kg", h2s.soc_init_kg))
    y_el0 = int(round(state_init.get("y_el_on", 0)))
    p_el0 = float(state_init.get("el_power_mw", 0.0))

    prob = pulp.LpProblem("CurtailmentAbsorption_MultiFlex", pulp.LpMinimize)

    # Curtailment used and unused
    P_curt = pulp.LpVariable.dicts("P_curt_use_mw", T, lowBound=0)
    Unused = pulp.LpVariable.dicts("Unused_curt_mw", T, lowBound=0)

    # Battery
    y_bch = pulp.LpVariable.dicts("y_batt_ch", T, 0, 1, cat=pulp.LpBinary)
    y_bdis = pulp.LpVariable.dicts("y_batt_dis", T, 0, 1, cat=pulp.LpBinary)
    P_bch = pulp.LpVariable.dicts("P_batt_ch_mw", T, lowBound=0)
    P_bdis = pulp.LpVariable.dicts("P_batt_dis_mw", T, lowBound=0)
    SOC_b = pulp.LpVariable.dicts("SOC_batt_mwh", T, lowBound=0)

    # Power-to-Heat (heater + TES) WITH DISCHARGE + UNMET SLACK
    y_th_ch = pulp.LpVariable.dicts("y_th_charge", T, 0, 1, cat=pulp.LpBinary)
    y_th_dis = pulp.LpVariable.dicts("y_th_discharge", T, 0, 1, cat=pulp.LpBinary)
    P_p2h = pulp.LpVariable.dicts("P_p2h_mw", T, lowBound=0)                      # MW_e
    Q_th_dis = pulp.LpVariable.dicts("Q_th_dis_mw_th", T, lowBound=0)             # MW_th
    SOC_th = pulp.LpVariable.dicts("SOC_th_mwh_th", T, lowBound=0)                # MWh_th
    Slack_unmet_th = pulp.LpVariable.dicts("Slack_unmet_th_mw_th", T, lowBound=0) # MW_th

    # Electrolyzer (MILP via on/off)
    y_el_on = pulp.LpVariable.dicts("y_el_on", T, 0, 1, cat=pulp.LpBinary)
    y_el_start = pulp.LpVariable.dicts("y_el_start", T, 0, 1, cat=pulp.LpBinary)
    y_el_stop = pulp.LpVariable.dicts("y_el_stop", T, 0, 1, cat=pulp.LpBinary)
    P_el = pulp.LpVariable.dicts("P_el_mw", T, lowBound=0)

    # H2 production/storage/sales
    H2_prod = pulp.LpVariable.dicts("H2_prod_kg", T, lowBound=0)
    H2_sale = pulp.LpVariable.dicts("H2_sale_kg", T, lowBound=0)
    SOC_h2 = pulp.LpVariable.dicts("SOC_h2_kg", T, lowBound=0)

    # Power balance absolute feasibility slacks
    P_def = pulp.LpVariable.dicts("P_pb_deficit_mw", T, lowBound=0)
    P_sur = pulp.LpVariable.dicts("P_pb_surplus_mw", T, lowBound=0)

    # ---- Constraints ----

    # Curtailment cap & unused definition (PROJECT curve)
    for t in T:
        prob += P_curt[t] <= float(absorb_need_mw[t]), f"curt_cap_{t}"
        prob += Unused[t] >= float(absorb_need_mw[t]) - P_curt[t], f"unused_def_{t}"
        if verify_full_absorption:
            prob += P_curt[t] == float(absorb_need_mw[t]), f"curt_fullabs_{t}"
            prob += Unused[t] == 0, f"unused_zero_{t}"

    # Battery mode and caps
    for t in T:
        prob += y_bch[t] + y_bdis[t] <= 1, f"batt_mode_{t}"
        prob += P_bch[t] <= batt.power_mw * y_bch[t], f"batt_ch_cap_{t}"
        prob += P_bdis[t] <= batt.power_mw * y_bdis[t], f"batt_dis_cap_{t}"

    # Battery SOC dynamics
    soc_b_min = batt.soc_min_frac * batt.energy_mwh
    soc_b_max = batt.soc_max_frac * batt.energy_mwh
    for t in T:
        if t == 0:
            prob += SOC_b[t] == soc_b0 + batt.eta_ch * P_bch[t] * dt_h - (1.0 / batt.eta_dis) * P_bdis[t] * dt_h, f"batt_soc_{t}"
        else:
            prob += SOC_b[t] == SOC_b[t-1] + batt.eta_ch * P_bch[t] * dt_h - (1.0 / batt.eta_dis) * P_bdis[t] * dt_h, f"batt_soc_{t}"
        prob += SOC_b[t] >= soc_b_min, f"batt_soc_min_{t}"
        prob += SOC_b[t] <= soc_b_max, f"batt_soc_max_{t}"

    # Thermal: charge/discharge mutual exclusivity + caps
    for t in T:
        prob += y_th_ch[t] + y_th_dis[t] <= 1, f"th_mode_{t}"
        prob += P_p2h[t] <= th.heater_cap_mw * y_th_ch[t], f"p2h_cap_{t}"
        prob += Q_th_dis[t] <= th.discharge_cap_mw_th * y_th_dis[t], f"tes_dis_cap_{t}"

    # Thermal SOC dynamics WITH discharge + loss
    th_min = th.soc_min_frac * th.storage_cap_mwh_th
    th_max = th.soc_max_frac * th.storage_cap_mwh_th
    loss_step = max(0.0, th.loss_frac_per_hour) * dt_h  # fraction per step

    for t in T:
        q_ch_mw_th = th.eta_p2h * P_p2h[t]  # MW_th
        if t == 0:
            prob += SOC_th[t] == (1.0 - loss_step) * soc_th0 + q_ch_mw_th * dt_h - Q_th_dis[t] * dt_h, f"th_soc_{t}"
        else:
            prob += SOC_th[t] == (1.0 - loss_step) * SOC_th[t-1] + q_ch_mw_th * dt_h - Q_th_dis[t] * dt_h, f"th_soc_{t}"

        prob += SOC_th[t] >= th_min, f"th_soc_min_{t}"
        prob += SOC_th[t] <= th_max, f"th_soc_max_{t}"

    # Heat demand satisfaction (allow unmet via slack)
    for t in T:
        prob += Q_th_dis[t] + Slack_unmet_th[t] >= float(heat_demand_mw_th[t]), f"heat_balance_{t}"

    # Electrolyzer capacity + min load when on
    for t in T:
        prob += P_el[t] <= el.cap_mw * y_el_on[t], f"el_cap_{t}"
        prob += P_el[t] >= el.cap_mw * el.min_load_frac * y_el_on[t], f"el_minload_{t}"

    # Electrolyzer start/stop
    for t in T:
        if t == 0:
            prob += y_el_start[t] >= y_el_on[t] - y_el0, f"el_start_{t}"
            prob += y_el_stop[t] >= y_el0 - y_el_on[t], f"el_stop_{t}"
        else:
            prob += y_el_start[t] >= y_el_on[t] - y_el_on[t-1], f"el_start_{t}"
            prob += y_el_stop[t] >= y_el_on[t-1] - y_el_on[t], f"el_stop_{t}"

    # Ramp constraints (engineering version: allow big jump at start/stop via big-M)
    ramp_mw = max(0.0, el.ramp_frac_per_step) * el.cap_mw
    M = el.cap_mw  # big-M scale (capacity)
    for t in T:
        if t == 0:
            prob += P_el[t] - p_el0 <= ramp_mw + M * y_el_start[t], "el_ramp_up_0"
            prob += p_el0 - P_el[t] <= ramp_mw + M * y_el_stop[t], "el_ramp_dn_0"
        else:
            prob += P_el[t] - P_el[t-1] <= ramp_mw + M * y_el_start[t], f"el_ramp_up_{t}"
            prob += P_el[t-1] - P_el[t] <= ramp_mw + M * y_el_stop[t], f"el_ramp_dn_{t}"

    # Min up / min down (in steps)
    U = max(0, int(el.min_up_steps))
    D = max(0, int(el.min_down_steps))

    if U > 0:
        for t in range(n):
            if t + U <= n:
                prob += pulp.lpSum([y_el_on[k] for k in range(t, t + U)]) >= U * y_el_start[t], f"el_min_up_{t}"
            else:
                prob += pulp.lpSum([y_el_on[k] for k in range(t, n)]) >= (n - t) * y_el_start[t], f"el_min_up_tail_{t}"

    if D > 0:
        for t in range(n):
            if t + D <= n:
                prob += pulp.lpSum([(1 - y_el_on[k]) for k in range(t, t + D)]) >= D * y_el_stop[t], f"el_min_dn_{t}"
            else:
                prob += pulp.lpSum([(1 - y_el_on[k]) for k in range(t, n)]) >= (n - t) * y_el_stop[t], f"el_min_dn_tail_{t}"

    # Power balance
    for t in T:
        prob += (
            P_curt[t] + P_bdis[t] + P_def[t]
            == P_bch[t] + P_el[t] + P_p2h[t] + P_sur[t]
        ), f"power_balance_{t}"
        if verify_full_absorption:
            prob += P_def[t] == 0, f"pb_def_zero_{t}"
            prob += P_sur[t] == 0, f"pb_sur_zero_{t}"

    # H2 production from electrolyzer
    for t in T:
        prob += H2_prod[t] == (P_el[t] * dt_h) / max(el.specific_energy_mwh_per_kg, 1e-9), f"h2_prod_{t}"

    # H2 storage balance + bounds
    h2_min = h2s.soc_min_frac * h2s.cap_kg
    h2_max = h2s.soc_max_frac * h2s.cap_kg
    for t in T:
        if t == 0:
            prob += SOC_h2[t] == soc_h20 + H2_prod[t] - H2_sale[t], f"h2_soc_{t}"
        else:
            prob += SOC_h2[t] == SOC_h2[t-1] + H2_prod[t] - H2_sale[t], f"h2_soc_{t}"
        prob += SOC_h2[t] >= h2_min, f"h2_soc_min_{t}"
        prob += SOC_h2[t] <= h2_max, f"h2_soc_max_{t}"

    # ---- Objective ----
    total_unused = pulp.lpSum([Unused[t] * econ.penalty_unused_eur_per_mwh * dt_h for t in T])
    total_el_om = pulp.lpSum([P_el[t] * el.var_om_eur_per_mwh * dt_h for t in T])
    total_el_startstop = pulp.lpSum([y_el_start[t] * el.start_cost_eur + y_el_stop[t] * el.stop_cost_eur for t in T])
    total_pb_def = pulp.lpSum([P_def[t] * econ.penalty_power_balance_slack_eur_per_mwh * dt_h for t in T])
    total_pb_sur = pulp.lpSum([P_sur[t] * econ.penalty_spill_eur_per_mwh * dt_h for t in T])
    total_unmet_heat = pulp.lpSum([Slack_unmet_th[t] * econ.penalty_unmet_heat_eur_per_mwh_th * dt_h for t in T])
    total_h2_rev = pulp.lpSum([H2_sale[t] * float(h2_price[t]) for t in T])

    prob += total_unused + total_el_om + total_el_startstop + total_pb_def + total_pb_sur + total_unmet_heat - total_h2_rev

    # ---- Solve ----
    solver = _cbc_solver(
        msg=msg,
        time_limit_s=solve.time_limit_s,
        gap=solve.mip_gap,
        threads=solve.threads,
        cbc_path=solve.cbc_path,
    )
    prob.solve(solver)

    try:
        solver.close()
    except Exception:
        pass

    status_str = pulp.LpStatus.get(prob.status, str(prob.status))

    # 允许 TIME LIMIT 下仍然有可行解的情况：
    # - PuLP 有时会把这种情况标成 Not Solved
    # - 但只要目标值能取到（prob.objective 有值），就说明有 incumbent 可以导出变量值
    obj_val = None
    try:
        obj_val = pulp.value(prob.objective)
    except Exception:
        obj_val = None

    if status_str not in ("Optimal", "Feasible"):
        if obj_val is None:
            raise RuntimeError(f"Solver status = {status_str} (no incumbent solution)")
        else:
            print(f"[WARN] Solver status = {status_str} but incumbent exists (obj={obj_val:.6f}). Accepting solution.",
                  flush=True)
            # 继续往下收集结果，不 raise

    # ---- Collect results ----
    rows = []
    for t in T:
        rows.append({
            "dt": dt_index[t],
            "absorb_need_mw_region": float(absorb_need_mw_region[t]),
            "absorb_need_mw": float(absorb_need_mw[t]),  # project (alpha-scaled)
            "heat_demand_mw_th": float(heat_demand_mw_th[t]),
            "h2_price_eur_per_kg": float(h2_price[t]),

            "P_curt_use_mw": float(pulp.value(P_curt[t]) or 0.0),
            "Unused_curt_mw": float(pulp.value(Unused[t]) or 0.0),

            "P_batt_ch_mw": float(pulp.value(P_bch[t]) or 0.0),
            "P_batt_dis_mw": float(pulp.value(P_bdis[t]) or 0.0),
            "SOC_batt_mwh": float(pulp.value(SOC_b[t]) or 0.0),

            "P_p2h_mw": float(pulp.value(P_p2h[t]) or 0.0),
            "Q_th_dis_mw_th": float(pulp.value(Q_th_dis[t]) or 0.0),
            "Slack_unmet_th_mw_th": float(pulp.value(Slack_unmet_th[t]) or 0.0),
            "SOC_th_mwh_th": float(pulp.value(SOC_th[t]) or 0.0),

            "P_el_mw": float(pulp.value(P_el[t]) or 0.0),
            "y_el_on": int(round(float(pulp.value(y_el_on[t]) or 0.0))),
            "y_el_start": int(round(float(pulp.value(y_el_start[t]) or 0.0))),
            "y_el_stop": int(round(float(pulp.value(y_el_stop[t]) or 0.0))),

            "H2_prod_kg": float(pulp.value(H2_prod[t]) or 0.0),
            "H2_sale_kg": float(pulp.value(H2_sale[t]) or 0.0),
            "SOC_h2_kg": float(pulp.value(SOC_h2[t]) or 0.0),

            "P_pb_deficit_mw": float(pulp.value(P_def[t]) or 0.0),
            "P_pb_surplus_mw": float(pulp.value(P_sur[t]) or 0.0),
        })

    out = pd.DataFrame(rows)

    last = out.iloc[-1]
    next_state = {
        "batt_soc_mwh": float(last["SOC_batt_mwh"]),
        "th_soc_mwh_th": float(last["SOC_th_mwh_th"]),
        "h2_soc_kg": float(last["SOC_h2_kg"]),
        "y_el_on": int(last["y_el_on"]),
        "el_power_mw": float(last["P_el_mw"]),
    }

    return out, next_state, status_str


# =========================
# Rolling horizon
# =========================

def rolling_horizon_dispatch(
    df: pd.DataFrame,
    batt: BatteryParams,
    th: ThermalParams,
    el: ElectrolyzerParams,
    h2s: H2StorageParams,
    econ: EconomicParams,
    solve: SolveParams,
    msg: bool = False,
    outdir: Optional[Path] = None,
    verify_full_absorption: bool = False,
) -> Tuple[pd.DataFrame, str]:

    df = df.copy().sort_values("dt").reset_index(drop=True)
    idx = pd.DatetimeIndex(df["dt"])

    absorb = df["absorb_need_mw"].astype(float).to_numpy()
    absorb_region = df["absorb_need_mw_region"].astype(float).to_numpy()

    heat_demand = df["heat_demand_mw_th"].astype(float).to_numpy() \
        if "heat_demand_mw_th" in df.columns else np.zeros(len(df), dtype=float)

    if "h2_price_eur_per_kg" in df.columns:
        h2p = df["h2_price_eur_per_kg"].astype(float).to_numpy()
    else:
        h2p = np.full(len(df), econ.h2_price_default_eur_per_kg, dtype=float)

    steps_per_hour = int(round(1.0 / econ.dt_h))
    win_steps = int(round(solve.rolling_horizon_hours * steps_per_hour))
    ov_steps = int(round(solve.overlap_hours * steps_per_hour))

    if win_steps <= 0:
        raise ValueError("rolling_horizon_hours too small")
    if ov_steps < 0 or ov_steps >= win_steps:
        raise ValueError("overlap_hours must be in [0, rolling_horizon_hours)")

    keep_steps = win_steps - ov_steps
    n = len(idx)

    state = {
        "batt_soc_mwh": batt.soc_init_mwh,
        "th_soc_mwh_th": th.soc_init_mwh_th,
        "h2_soc_kg": h2s.soc_init_kg,
        "y_el_on": 0,
        "el_power_mw": 0.0,
    }

    all_parts: List[pd.DataFrame] = []
    t0 = 0
    win_id = 0
    last_status = "NA"

    partial_path = None
    if outdir is not None:
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        partial_path = outdir / "dispatch_partial.csv"
        if partial_path.exists():
            partial_path.unlink()

    t_start_all = time.perf_counter()

    while t0 < n:
        win_id += 1
        t1 = min(t0 + win_steps, n)

        print(f"[RH] window {win_id} START | t0={t0} t1={t1} steps={t1-t0} | {idx[t0]} -> {idx[t1-1]}", flush=True)
        t_start_win = time.perf_counter()

        window_df, state_next, status = build_and_solve_window(
            dt_index=idx[t0:t1],
            absorb_need_mw=absorb[t0:t1],
            absorb_need_mw_region=absorb_region[t0:t1],
            heat_demand_mw_th=heat_demand[t0:t1],
            h2_price=h2p[t0:t1],
            batt=batt,
            th=th,
            el=el,
            h2s=h2s,
            econ=econ,
            solve=solve,
            state_init=state,
            msg=msg,
            verify_full_absorption=verify_full_absorption,
        )
        last_status = status

        dt_win = time.perf_counter() - t_start_win
        pb_mwh = float((window_df["P_pb_deficit_mw"] + window_df["P_pb_surplus_mw"]).sum() * econ.dt_h)
        pb_max = float((window_df["P_pb_deficit_mw"] + window_df["P_pb_surplus_mw"]).max())
        unmet_heat_mwh = float(window_df["Slack_unmet_th_mw_th"].sum() * econ.dt_h)
        print(f"[RH] window {win_id} SOLVED | status={status} | time={dt_win:.2f}s | PB_slack_MWh={pb_mwh:.6f} | PB_max_MW={pb_max:.6f} | UnmetHeat_MWh_th={unmet_heat_mwh:.6f}", flush=True)

        if t1 == n:
            kept = window_df
            all_parts.append(kept)
            if partial_path is not None:
                kept.to_csv(partial_path, index=False, mode="a", header=not partial_path.exists())
            break

        kept = window_df.iloc[:keep_steps].copy()
        all_parts.append(kept)

        if partial_path is not None:
            kept.to_csv(partial_path, index=False, mode="a", header=not partial_path.exists())

        # carry state at cut
        cut_row = window_df.iloc[keep_steps - 1]
        state = {
            "batt_soc_mwh": float(cut_row["SOC_batt_mwh"]),
            "th_soc_mwh_th": float(cut_row["SOC_th_mwh_th"]),
            "h2_soc_kg": float(cut_row["SOC_h2_kg"]),
            "y_el_on": int(cut_row["y_el_on"]),
            "el_power_mw": float(cut_row["P_el_mw"]),
        }
        t0 += keep_steps

    out = pd.concat(all_parts, ignore_index=True)
    out = out.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)

    t_total = time.perf_counter() - t_start_all
    print(f"[RH] DONE | windows={win_id} | total_time={t_total/60:.1f} min | status={last_status}", flush=True)

    return out, last_status


# =========================
# KPIs & Summary
# =========================

def compute_kpis(dispatch: pd.DataFrame, econ: EconomicParams, alpha: float) -> Dict[str, float]:
    dt_h = econ.dt_h

    # project accessible energy (scaled)
    e_project_total_mwh = float((dispatch["absorb_need_mw"] * dt_h).sum())
    e_absorb_mwh = float((dispatch["P_curt_use_mw"] * dt_h).sum())
    e_unused_calc_mwh = max(e_project_total_mwh - e_absorb_mwh, 0.0)
    e_unused_model_mwh = float((dispatch["Unused_curt_mw"] * dt_h).sum())
    absorption_rate = 100.0 * e_absorb_mwh / max(e_project_total_mwh, 1e-9)

    # region energy (unscaled)
    e_region_total_mwh = float((dispatch["absorb_need_mw_region"] * dt_h).sum()) \
        if "absorb_need_mw_region" in dispatch.columns else float("nan")

    # H2
    h2_prod_kg = float(dispatch["H2_prod_kg"].sum())
    h2_sale_kg = float(dispatch["H2_sale_kg"].sum())
    h2_rev = float((dispatch["H2_sale_kg"] * dispatch["h2_price_eur_per_kg"]).sum())

    # PB slack
    pb_def_mwh = float((dispatch["P_pb_deficit_mw"] * dt_h).sum())
    pb_sur_mwh = float((dispatch["P_pb_surplus_mw"] * dt_h).sum())

    # Heat
    unmet_heat_mwh_th = float((dispatch["Slack_unmet_th_mw_th"] * dt_h).sum())
    heat_demand_mwh_th = float((dispatch["heat_demand_mw_th"] * dt_h).sum())
    heat_served_mwh_th = float((dispatch["Q_th_dis_mw_th"] * dt_h).sum())

    # switching
    n_start = int(dispatch["y_el_start"].sum()) if "y_el_start" in dispatch.columns else 0
    n_stop = int(dispatch["y_el_stop"].sum()) if "y_el_stop" in dispatch.columns else 0

    return {
        "alpha": float(alpha),
        "E_region_total_MWh": e_region_total_mwh,
        "E_project_total_MWh": e_project_total_mwh,
        "E_absorbed_MWh": e_absorb_mwh,
        "E_unused_calc_MWh": e_unused_calc_mwh,
        "E_unused_model_MWh": e_unused_model_mwh,
        "Absorption_rate_%": absorption_rate,
        "H2_prod_kg": h2_prod_kg,
        "H2_sale_kg": h2_sale_kg,
        "H2_revenue_EUR": h2_rev,
        "PB_deficit_MWh": pb_def_mwh,
        "PB_surplus_MWh": pb_sur_mwh,
        "Heat_demand_MWh_th": heat_demand_mwh_th,
        "Heat_served_MWh_th": heat_served_mwh_th,
        "Unmet_heat_MWh_th": unmet_heat_mwh_th,
        "EL_starts": float(n_start),
        "EL_stops": float(n_stop),
    }


def write_summary(outdir: Path, kpis: Dict[str, float], args: argparse.Namespace, curve_csv: Path, prices_csv: Optional[Path], status: str) -> None:
    lines = []
    lines.append("=== MULTI-FLEX MILP DISPATCH SUMMARY (PuLP + GUROBI) ===")
    lines.append(f"Solve status: {status}")
    lines.append("")
    lines.append("=== INPUT ===")
    lines.append(f"Curve CSV : {curve_csv}")
    lines.append(f"H2 Price CSV: {prices_csv if prices_csv else 'None (use curve column or default)'}")
    lines.append(f"dt_h: {args.dt_h}")
    lines.append(f"tz  : {args.tz}")
    lines.append(f"alpha: {kpis.get('alpha', 'NA')}")
    lines.append("")
    lines.append("=== THERMAL ===")
    lines.append(f"p2h_cap_mw: {args.p2h_cap_mw}")
    lines.append(f"p2h_eta: {args.p2h_eta}")
    lines.append(f"tes_cap_mwh_th: {args.tes_cap_mwh_th}")
    lines.append(f"tes_discharge_cap_mw_th: {args.tes_discharge_cap_mw_th}")
    lines.append(f"tes_loss_frac_per_hour: {args.tes_loss_frac_per_hour}")
    lines.append(f"penalty_unmet_heat_eur_per_mwh_th: {args.penalty_unmet_heat_eur_per_mwh_th}")
    lines.append("")
    lines.append("=== ELECTROLYZER (ENGINEERING) ===")
    lines.append(f"el_cap_mw: {args.el_cap_mw}")
    lines.append(f"el_min_load_frac: {args.el_min_load_frac}")
    lines.append(f"el_ramp_frac_per_step: {args.el_ramp_frac_per_step}")
    lines.append(f"el_min_up_steps: {args.el_min_up_steps}")
    lines.append(f"el_min_down_steps: {args.el_min_down_steps}")
    lines.append("")
    lines.append("=== SOLVE ===")
    lines.append(f"rolling_horizon_hours: {args.rolling_horizon_hours}")
    lines.append(f"overlap_hours        : {args.overlap_hours}")
    lines.append(f"time_limit_s         : {args.time_limit_s}")
    lines.append(f"mip_gap              : {args.mip_gap}")
    lines.append(f"threads              : {args.threads}")
    lines.append(f"verify_full_absorption: {bool(args.verify_full_absorption)}")
    lines.append("")
    lines.append("=== KPIs ===")
    for k, v in kpis.items():
        try:
            lines.append(f"{k}: {float(v):,.6f}")
        except Exception:
            lines.append(f"{k}: {v}")

    (outdir / "kpi_summary.txt").write_text("\n".join(lines), encoding="utf-8")


# =========================
# CLI
# =========================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()

    # IO
    p.add_argument("--curve_csv", type=str, default=None, help="Path to region_absorb_need_curve_30min.csv")
    p.add_argument("--prices_csv", type=str, default=None, help="Optional: CSV containing time-varying H2 price.")
    p.add_argument("--outdir", type=str, default=None, help="Output directory (default: ./outputs/dispatch_milp/<run_id>)")
    p.add_argument("--tz", type=str, default="Europe/Paris")
    p.add_argument("--merge_tolerance_min", type=int, default=15, help="merge_asof tolerance in minutes (default 15)")

    # alpha scenarios
    p.add_argument(
        "--alphas",
        type=str,
        default="0.005,0.0066,0.01",
        help="Comma-separated accessible curtailment share(s). "
             "Example: '0.005,0.0066,0.01' for 0.5%, 0.66%, 1%. "
             "NOTE: 1% is 0.01, 100% is 1.0."
    )

    # time step
    p.add_argument("--dt_h", type=float, default=0.5)

    # Battery
    p.add_argument("--batt_power_mw", type=float, default=3.0)
    p.add_argument("--batt_energy_mwh", type=float, default=6.0)
    p.add_argument("--batt_eta_ch", type=float, default=0.95)
    p.add_argument("--batt_eta_dis", type=float, default=0.95)
    p.add_argument("--batt_soc_init_mwh", type=float, default=-1.0, help="If <0, set to 50% of batt_energy_mwh")
    p.add_argument("--batt_soc_min_frac", type=float, default=0.05)
    p.add_argument("--batt_soc_max_frac", type=float, default=0.95)

    # Power-to-Heat (TES)
    p.add_argument("--p2h_cap_mw", type=float, default=5.0, help="Electric heater cap (MW_e)")
    p.add_argument("--tes_cap_mwh_th", type=float, default=200.0, help="Thermal storage cap (MWh_th)")
    p.add_argument("--p2h_eta", type=float, default=0.98, help="Electric->heat efficiency or COP")
    p.add_argument("--tes_discharge_cap_mw_th", type=float, default=-1.0,
                   help="TES discharge cap (MW_th). If <0, set to p2h_cap_mw * p2h_eta")
    p.add_argument("--tes_loss_frac_per_hour", type=float, default=0.0,
                   help="Thermal loss fraction per hour (e.g. 0.001 = 0.1%/h)")
    p.add_argument("--tes_soc_init_mwh_th", type=float, default=0.0)
    p.add_argument("--tes_soc_min_frac", type=float, default=0.0)
    p.add_argument("--tes_soc_max_frac", type=float, default=1.0)

    # Electrolyzer
    p.add_argument("--el_cap_mw", type=float, default=5.0)
    p.add_argument("--el_min_load_frac", type=float, default=0.2)
    p.add_argument("--el_specific_energy_mwh_per_kg", type=float, default=0.050)  # ~50 kWh/kg
    p.add_argument("--el_var_om_eur_per_mwh", type=float, default=2.0)
    p.add_argument("--el_start_cost_eur", type=float, default=2000.0)
    p.add_argument("--el_stop_cost_eur", type=float, default=500.0)

    # Engineering constraints (steps are your time resolution steps, default 30-min)
    p.add_argument("--el_ramp_frac_per_step", type=float, default=0.30,
                   help="Max ramp per step as fraction of el_cap_mw (start/stop jump allowed)")
    p.add_argument("--el_min_up_steps", type=int, default=1,
                   help="Min consecutive ON steps after each start (1 step = dt_h hours)")
    p.add_argument("--el_min_down_steps", type=int, default=1,
                   help="Min consecutive OFF steps after each stop (1 step = dt_h hours)")

    # H2 storage
    p.add_argument("--h2_storage_kg", type=float, default=20_000.0)
    p.add_argument("--h2_soc_init_kg", type=float, default=0.0)
    p.add_argument("--h2_soc_min_frac", type=float, default=0.0)
    p.add_argument("--h2_soc_max_frac", type=float, default=1.0)

    # Economics
    p.add_argument("--h2_price_default_eur_per_kg", type=float, default=3.0)
    p.add_argument("--penalty_unused_eur_per_mwh", type=float, default=2000.0)
    p.add_argument("--penalty_power_balance_slack_eur_per_mwh", type=float, default=1e6)
    p.add_argument("--penalty_spill_eur_per_mwh", type=float, default=1e4)
    p.add_argument("--penalty_unmet_heat_eur_per_mwh_th", type=float, default=200.0,
                   help="Penalty for unmet heat demand (EUR/MWh_th)")

    # Solve
    p.add_argument("--rolling_horizon_hours", type=float, default=168.0)
    p.add_argument("--overlap_hours", type=float, default=24.0)
    p.add_argument("--time_limit_s", type=int, default=300)
    p.add_argument("--mip_gap", type=float, default=0.01)
    p.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 1))
    p.add_argument("--cbc_path", type=str, default=None, help="(kept for compatibility; not used when GUROBI is available)")
    p.add_argument("--solver_msg", action="store_true", help="Show solver log")

    # Strict feasibility check
    p.add_argument("--verify_full_absorption", action="store_true",
                   help="Enforce strict 100% absorption constraints (feasibility validation)")

    return p.parse_args()


def main() -> int:
    args = parse_args()
    cwd = Path.cwd().resolve()

    curve_csv = find_curve_csv(args.curve_csv, base_dir=cwd)
    curve_df = read_curve_csv_30min(curve_csv, tz=args.tz)

    prices_csv = find_prices_csv(args.prices_csv, base_dir=cwd)
    merged_df = curve_df

    if prices_csv is not None:
        price_df = read_h2_prices(prices_csv, tz=args.tz)
        merged_df = merge_curve_with_h2_price(curve_df, price_df, tolerance_min=args.merge_tolerance_min)
        print(f"[IO] merged curve + H2 prices OK | curve={curve_csv.name} | prices={prices_csv.name} | rows={len(merged_df)}", flush=True)
    else:
        print("[IO] no prices_csv found; will use default H2 price (or existing column if you merged externally).", flush=True)

    # outdir root
    if args.outdir:
        outdir_root = Path(args.outdir)
        if not outdir_root.is_absolute():
            outdir_root = (cwd / outdir_root).resolve()
    else:
        outdir_root = (cwd / "outputs" / "dispatch_milp" / timestamp_run_id()).resolve()
    ensure_dir(outdir_root)

    # preserve REGION curve (unscaled)
    merged_df = merged_df.copy()
    merged_df["absorb_need_mw"] = merged_df["absorb_need_mw"].astype(float)
    merged_df["absorb_need_mw_region"] = merged_df["absorb_need_mw"]

    # battery init soc default
    batt_soc_init = args.batt_soc_init_mwh
    if batt_soc_init < 0:
        batt_soc_init = 0.5 * args.batt_energy_mwh

    # thermal discharge cap default
    tes_dis_cap = args.tes_discharge_cap_mw_th
    if tes_dis_cap < 0:
        tes_dis_cap = args.p2h_cap_mw * args.p2h_eta

    batt = BatteryParams(
        power_mw=args.batt_power_mw,
        energy_mwh=args.batt_energy_mwh,
        eta_ch=args.batt_eta_ch,
        eta_dis=args.batt_eta_dis,
        soc_init_mwh=float(batt_soc_init),
        soc_min_frac=args.batt_soc_min_frac,
        soc_max_frac=args.batt_soc_max_frac,
    )

    th = ThermalParams(
        heater_cap_mw=args.p2h_cap_mw,
        storage_cap_mwh_th=args.tes_cap_mwh_th,
        eta_p2h=args.p2h_eta,
        discharge_cap_mw_th=float(tes_dis_cap),
        loss_frac_per_hour=float(args.tes_loss_frac_per_hour),
        soc_init_mwh_th=args.tes_soc_init_mwh_th,
        soc_min_frac=args.tes_soc_min_frac,
        soc_max_frac=args.tes_soc_max_frac,
    )

    el = ElectrolyzerParams(
        cap_mw=args.el_cap_mw,
        min_load_frac=args.el_min_load_frac,
        specific_energy_mwh_per_kg=args.el_specific_energy_mwh_per_kg,
        var_om_eur_per_mwh=args.el_var_om_eur_per_mwh,
        start_cost_eur=args.el_start_cost_eur,
        stop_cost_eur=args.el_stop_cost_eur,
        ramp_frac_per_step=float(args.el_ramp_frac_per_step),
        min_up_steps=int(args.el_min_up_steps),
        min_down_steps=int(args.el_min_down_steps),
    )

    h2s = H2StorageParams(
        cap_kg=args.h2_storage_kg,
        soc_init_kg=args.h2_soc_init_kg,
        soc_min_frac=args.h2_soc_min_frac,
        soc_max_frac=args.h2_soc_max_frac,
    )

    econ = EconomicParams(
        dt_h=args.dt_h,
        penalty_unused_eur_per_mwh=args.penalty_unused_eur_per_mwh,
        penalty_power_balance_slack_eur_per_mwh=args.penalty_power_balance_slack_eur_per_mwh,
        penalty_spill_eur_per_mwh=args.penalty_spill_eur_per_mwh,
        h2_price_default_eur_per_kg=args.h2_price_default_eur_per_kg,
        penalty_unmet_heat_eur_per_mwh_th=args.penalty_unmet_heat_eur_per_mwh_th,
    )

    solve = SolveParams(
        rolling_horizon_hours=args.rolling_horizon_hours,
        overlap_hours=args.overlap_hours,
        time_limit_s=args.time_limit_s,
        mip_gap=args.mip_gap,
        threads=args.threads,
        cbc_path=args.cbc_path,
    )

    alphas = parse_alphas(args.alphas)
    print(f"[RUN] Will solve for alphas={alphas}", flush=True)
    print(f"[OUT] Root output directory: {outdir_root}", flush=True)

    kpi_rows: List[Dict[str, object]] = []

    for alpha in alphas:
        tag = alpha_tag(alpha)
        outdir = ensure_dir(outdir_root / f"alpha_{tag}")

        # Build alpha-scaled DF
        df_alpha = merged_df.copy()
        df_alpha["absorb_need_mw"] = alpha * df_alpha["absorb_need_mw_region"]  # project curve

        # quick feasibility check for strict mode
        P_sink_max = args.batt_power_mw + args.el_cap_mw + args.p2h_cap_mw  # MW
        abs_max_proj = float(df_alpha["absorb_need_mw"].max())
        if getattr(args, "verify_full_absorption", False) and abs_max_proj > P_sink_max + 1e-6:
            scale = abs_max_proj / max(P_sink_max, 1e-9)
            print(f"[VERIFY][FAIL][alpha={alpha}] Full absorption IMPOSSIBLE by power cap.", flush=True)
            print(f"  absorb_need_max(project) = {abs_max_proj:.3f} MW", flush=True)
            print(f"  sink_power_max           = {P_sink_max:.3f} MW", flush=True)
            print(f"  Need ~{scale:.1f}x larger power capacities (or reduce alpha).", flush=True)

        # diagnostics
        print(f"\n===== SOLVE alpha={alpha} (tag={tag}) =====", flush=True)
        print(f"[IO] dt range: {df_alpha['dt'].min()} -> {df_alpha['dt'].max()} | dt_h={args.dt_h}", flush=True)
        print(f"[IO] absorb_need_mw_region: min={df_alpha['absorb_need_mw_region'].min():.3f} max={df_alpha['absorb_need_mw_region'].max():.3f}", flush=True)
        print(f"[IO] absorb_need_mw_project: min={df_alpha['absorb_need_mw'].min():.3f} max={df_alpha['absorb_need_mw'].max():.3f}", flush=True)

        # solve
        dispatch, status = rolling_horizon_dispatch(
            df=df_alpha,
            batt=batt,
            th=th,
            el=el,
            h2s=h2s,
            econ=econ,
            solve=solve,
            msg=args.solver_msg,
            outdir=outdir,
            verify_full_absorption=bool(args.verify_full_absorption),
        )

        dispatch_path = outdir / "dispatch_timeseries.csv"
        dispatch.to_csv(dispatch_path, index=False)

        kpis = compute_kpis(dispatch, econ=econ, alpha=alpha)
        write_summary(outdir, kpis, args, curve_csv=curve_csv, prices_csv=prices_csv, status=status)

        # Add to KPI aggregation
        row = dict(kpis)
        row["status"] = status
        row["alpha_tag"] = tag
        row["dispatch_csv"] = str(dispatch_path)
        kpi_rows.append(row)

        # console summary
        pb_total_mwh = float(kpis.get("PB_deficit_MWh", 0.0)) + float(kpis.get("PB_surplus_MWh", 0.0))
        pb_max_mw = float((dispatch["P_pb_deficit_mw"] + dispatch["P_pb_surplus_mw"]).max())
        pb_steps = int(((dispatch["P_pb_deficit_mw"] + dispatch["P_pb_surplus_mw"]) > 1e-9).sum())

        print(f"[OK][alpha={alpha}] Dispatch saved: {dispatch_path}", flush=True)
        print(f"[OK][alpha={alpha}] Summary saved : {outdir / 'kpi_summary.txt'}", flush=True)
        print(f"[OK][alpha={alpha}] Absorption rate (project-accessible): {kpis['Absorption_rate_%']:.2f}%", flush=True)
        print(f"[OK][alpha={alpha}] E_project_total_MWh={kpis['E_project_total_MWh']:.2f} | E_absorbed_MWh={kpis['E_absorbed_MWh']:.2f} | E_unused_calc_MWh={kpis['E_unused_calc_MWh']:.2f}", flush=True)
        print(f"[DIAG][alpha={alpha}] Power-balance slack: total={pb_total_mwh:.6f} MWh | max={pb_max_mw:.6f} MW | steps={pb_steps}/{len(dispatch)}", flush=True)

        if args.verify_full_absorption:
            max_unused = float(dispatch["Unused_curt_mw"].max())
            max_pb = float((dispatch["P_pb_deficit_mw"] + dispatch["P_pb_surplus_mw"]).max())
            print(f"[VERIFY][alpha={alpha}] strict full absorption: max_unused_mw={max_unused:.6e}, max_pb_slack_mw={max_pb:.6e}", flush=True)

    # Write aggregated KPI table
    if kpi_rows:
        kpi_df = pd.DataFrame(kpi_rows)
        agg_path = outdir_root / "alpha_kpi_summary.csv"
        kpi_df.to_csv(agg_path, index=False)
        print(f"\n[ALL DONE] Aggregated KPI saved: {agg_path}", flush=True)

    print(f"[ALL DONE] Results written under: {outdir_root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
