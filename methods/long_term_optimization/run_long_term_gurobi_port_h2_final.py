from __future__ import annotations

"""
Final Python + Gurobi long-term MILP for a port-based hydrogen project
=====================================================================

This version is designed to:
1) Keep the electrolyzer hot with a small grid keep-alive power P_keep,
   but P_keep does NOT produce hydrogen.
2) Force curtailed electricity to go to BESS first:
       u_t = p_ch_t
3) Produce hydrogen ONLY from BESS discharge:
       h_prod_t = kappa * p_dis_t
4) Avoid pathological "store forever and dump only at the single highest H2 price":
   - H2 selling flow cap
   - H2 selling ramp cap
   - finite tank buffer days
   - inventory holding cost
   - average residence time cap
   - daily max H2 delivery cap
5) Add fixed OPEX:
       fixed OPEX = 5% * overnight CAPEX per year

IMPORTANT
---------
To respect the user's request "only 20 minutes", this script by default runs
ONLY ONE scenario:
    F_PORT_LIST_MW = [100.0]
and sets:
    GUROBI_TIMELIMIT_S = 1200

If later you want more scenarios, edit F_PORT_LIST_MW manually.
"""

import os
import math
import argparse
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

try:
    import gurobipy as gp
    from gurobipy import GRB
except Exception as exc:
    raise ImportError(
        "gurobipy is required for this script. Install Gurobi and gurobipy first."
    ) from exc


# ============================================================
# 0) Utilities
# ============================================================

def find_file_by_stem(folder: str, stem: str, exts=("csv", "xlsx", "xls")) -> str:
    """Find file by exact stem.ext, prefix match in root, then recursive search."""
    folder = os.path.abspath(folder)

    for ext in exts:
        cand = os.path.join(folder, f"{stem}.{ext}")
        if os.path.exists(cand):
            return cand

    try:
        for fn in os.listdir(folder):
            full = os.path.join(folder, fn)
            if os.path.isfile(full) and fn.startswith(stem):
                return full
    except FileNotFoundError:
        pass

    for root, _, files in os.walk(folder):
        for fn in files:
            if fn.startswith(stem):
                return os.path.join(root, fn)

    raise FileNotFoundError(f"Cannot find file with stem='{stem}' in folder: {folder}")


def read_table(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext in [".xlsx", ".xls"]:
        return pd.read_excel(path)

    try:
        return pd.read_csv(path)
    except Exception:
        return pd.read_csv(path, sep=";")


def annuity_factor(r: float, n_years: float) -> float:
    if n_years <= 0:
        raise ValueError("n_years must be > 0")
    if r <= 0:
        return 1.0 / n_years
    return (r * (1 + r) ** n_years) / ((1 + r) ** n_years - 1)


# ============================================================
# 1) Configuration
# ============================================================

CONFIG: Dict[str, object] = {
    # time step
    "DT_H": 0.5,

    # --------------------------------------------------------
    # ONLY ONE scenario by default -> keeps total runtime ~20 min
    # --------------------------------------------------------
    "F_PORT_LIST_MW": [100.0],
    "RHO_LIST": [1.0],
    "C_COLLECT_LIST_EUR_PER_MWH": [10.0],

    # BESS technical
    "ETA_CH": math.sqrt(0.85),
    "ETA_DIS": math.sqrt(0.85),
    "P_B_UB_MW": 1000.0,
    "E_B_UB_MWH": 12000.0,

    # Electrolyzer technical
    "SEC_KWH_PER_KG": 55.0,
    "P_EL_UB_MW": 1500.0,
    "P_EL_MIN_MW_IF_BUILT": 0.0,

    # Keep-alive logic
    "KEEP_ALIVE_FRAC": 0.05,   # P_keep = 5% * P_EL_max

    # H2 tank / sales logic
    "H2_LOSS_PER_STEP": 0.0,
    "SELL_FLOW_FACTOR": 1.0,       # per-step cap
    "SELL_RAMP_FRAC": 0.20,        # per-step ramp as fraction of sell cap
    "BUFFER_DAYS_MAX": 5.0,        # tank size <= 5 days equivalent sell cap
    "TANK_V_UB": 2e6,
    "H2_CAPACITY_PER_V_KG": 40.0,  # if V is m^3
    "TANK_V_MIN_IF_BUILT": 0.0,

    # Minimum annual absorption target
    "MIN_ABSORPTION_SHARE": 0.05,

    # Additional anti-arbitrage economics / constraints
    "FIXED_OPEX_FRAC_OF_CAPEX": 0.05,          # 5% of overnight CAPEX / year
    "H2_HOLD_COST_EUR_PER_KG_PER_DAY": 0.02,  # inventory carrying cost
    "MAX_AVG_RESIDENCE_DAYS": 3.0,             # average residence time cap
    "DAILY_SELL_MAX_EQUIV_HOURS": 8.0,         # daily max delivery = 8h nameplate prod

    # Economics / annualization
    "DISCOUNT_RATE": 0.08,
    "USD_TO_EUR": 0.92,

    # CAPEX defaults
    "EL_CAPEX_EUR_PER_MW": 1_850_000.0,
    "EL_LIFETIME_Y": 15.0,
    "BESS_CAPEX_EUR_PER_MW": 150_000.0,
    "BESS_CAPEX_EUR_PER_MWH": 250_000.0,
    "BESS_LIFETIME_Y": 15.0,
    "TANK_LIFETIME_Y": 20.0,

    # Gurobi options
    "GUROBI_LOGTOCONSOLE": 1,
    "GUROBI_TIMELIMIT_S": 1200,   # 20 minutes total
    "GUROBI_MIPGAP": 0.01,
    "GUROBI_THREADS": 8,
    "GUROBI_MIPFOCUS": 1,
    "GUROBI_HEURISTICS": 0.2,

    # Output control
    "WRITE_MODEL_LP": False,
    "WRITE_IIS_IF_INFEASIBLE": True,
}


# ============================================================
# 2) Tank CAPEX fit from user's material model
# ============================================================

def fit_tank_capex_line(
    consumption_path: str,
    price_path: str,
    cost_frac_path: str
) -> Tuple[float, float, pd.DataFrame]:
    """
    Returns a linear fit:
        CAPEX_USD(V) ~= a_usd_per_V * V + b_usd_fixed
    """
    cons = read_table(consumption_path).copy()
    price = read_table(price_path).copy()
    frac = read_table(cost_frac_path).copy()

    cons = cons.rename(columns={cons.columns[0]: "item"})
    frac = frac.rename(columns={frac.columns[0]: "item"})

    size_row_label = str(cons.iloc[0, 0]).strip()
    if "Size of the tank" not in size_row_label:
        raise ValueError("First row of Material_consumption_700bar must be 'Size of the tank'.")

    size_cols = list(cons.columns[1:])
    sizes = cons.iloc[0, 1:].astype(float).to_numpy()

    price_map = {
        str(r["Material"]).strip(): float(r["Price (in USD per kg)"])
        for _, r in price.iterrows()
    }

    vessel_raw_costs = []
    for col in size_cols:
        raw_cost = 0.0
        for i in range(1, len(cons)):
            mat = str(cons.iloc[i]["item"]).strip()
            mass_kg = float(cons.iloc[i][col])
            if mat not in price_map:
                raise KeyError(f"Material '{mat}' from consumption file not found in price file.")
            raw_cost += mass_kg * price_map[mat]
        vessel_raw_costs.append(raw_cost)

    frac["item"] = frac["item"].astype(str).str.strip()
    vessel_material_share = float(frac.loc[frac["item"] == "Compressed Vessel", "Material"].iloc[0])
    vessel_processing_share = float(frac.loc[frac["item"] == "Compressed Vessel", "processing"].iloc[0])

    if vessel_material_share <= 0 or vessel_processing_share <= 0:
        raise ValueError("Invalid Cost_frac shares; they must be positive.")

    markup = 1.0 / (vessel_material_share * vessel_processing_share)
    total_costs_usd = np.array(vessel_raw_costs, dtype=float) * markup

    a_usd, b_usd = np.polyfit(sizes, total_costs_usd, 1)

    fit_df = pd.DataFrame({
        "size_V": sizes,
        "raw_vessel_material_cost_usd": vessel_raw_costs,
        "markup_factor": markup,
        "approx_total_tank_cost_usd": total_costs_usd,
    })
    return float(a_usd), float(b_usd), fit_df


# ============================================================
# 3) Data loading
# ============================================================

def load_timeseries(data_dir: str) -> pd.DataFrame:
    f_absorb = find_file_by_stem(data_dir, "region_absorb_need_curve_30min")
    f_price = find_file_by_stem(data_dir, "prices_2024_30min_elec_plus_h2_fixed2024_margin1.0")

    df_a = read_table(f_absorb)
    df_p = read_table(f_price)

    # use utc=True to avoid the warning you saw
    df_a["dt"] = pd.to_datetime(df_a["dt"], errors="coerce", utc=True)
    df_p["dt"] = pd.to_datetime(df_p["dt"], errors="coerce", utc=True)

    if "absorb_need_mw_region" in df_a.columns:
        absorb_col = "absorb_need_mw_region"
    elif "absorb_need_mw" in df_a.columns:
        absorb_col = "absorb_need_mw"
    else:
        num_cols = [c for c in df_a.columns if c != "dt" and pd.api.types.is_numeric_dtype(df_a[c])]
        if not num_cols:
            raise ValueError("No absorb-need column found in region_absorb_need_curve_30min.")
        absorb_col = num_cols[-1]

    if "grid_sell_eur_per_mwh" in df_p.columns:
        elec_col = "grid_sell_eur_per_mwh"
    else:
        num_cols = [c for c in df_p.columns if c != "dt" and pd.api.types.is_numeric_dtype(df_p[c])]
        if not num_cols:
            raise ValueError("No electricity price column found in price file.")
        elec_col = num_cols[0]

    if "h2_price_eur_per_kg" in df_p.columns:
        h2_col = "h2_price_eur_per_kg"
    else:
        num_cols = [c for c in df_p.columns if c != "dt" and pd.api.types.is_numeric_dtype(df_p[c])]
        if len(num_cols) < 2:
            raise ValueError("No hydrogen price column found in price file.")
        h2_col = num_cols[1]

    df = pd.merge(
        df_a[["dt", absorb_col]].rename(columns={absorb_col: "S_reg_mw"}),
        df_p[["dt", elec_col, h2_col]].rename(
            columns={elec_col: "elec_price_eur_per_mwh", h2_col: "h2_price_eur_per_kg"}
        ),
        on="dt",
        how="inner",
    ).sort_values("dt").reset_index(drop=True)

    df["S_reg_mw"] = pd.to_numeric(df["S_reg_mw"], errors="coerce").fillna(0.0).clip(lower=0.0)
    df["elec_price_eur_per_mwh"] = pd.to_numeric(df["elec_price_eur_per_mwh"], errors="coerce")
    df["h2_price_eur_per_kg"] = pd.to_numeric(df["h2_price_eur_per_kg"], errors="coerce")
    df = df.dropna(subset=["dt", "elec_price_eur_per_mwh", "h2_price_eur_per_kg"]).reset_index(drop=True)
    return df


# ============================================================
# 4) Result container
# ============================================================

@dataclass
class ScenarioResult:
    summary: Dict[str, float | str]
    dispatch: pd.DataFrame


# ============================================================
# 5) Gurobi model
# ============================================================

def solve_one_scenario(
    df: pd.DataFrame,
    data_dir: str,
    out_dir: str,
    cfg: Dict[str, object],
    *,
    F_port_mw: float,
    rho: float,
    c_collect_eur_per_mwh: float,
) -> ScenarioResult:
    DT = float(cfg["DT_H"])
    eta_ch = float(cfg["ETA_CH"])
    eta_dis = float(cfg["ETA_DIS"])
    sec_kwh_per_kg = float(cfg["SEC_KWH_PER_KG"])
    k_h2 = 1000.0 * DT / sec_kwh_per_kg  # kg per half-hour per MW

    p_b_ub = float(cfg["P_B_UB_MW"])
    e_b_ub = float(cfg["E_B_UB_MWH"])
    p_el_ub = float(cfg["P_EL_UB_MW"])
    p_el_min_if_built = float(cfg["P_EL_MIN_MW_IF_BUILT"])
    keep_frac = float(cfg["KEEP_ALIVE_FRAC"])

    h2_loss = float(cfg["H2_LOSS_PER_STEP"])
    sell_flow_factor = float(cfg["SELL_FLOW_FACTOR"])
    sell_ramp_frac = float(cfg["SELL_RAMP_FRAC"])
    buffer_days = float(cfg["BUFFER_DAYS_MAX"])

    gamma = float(cfg["H2_CAPACITY_PER_V_KG"])
    tank_v_ub = float(cfg["TANK_V_UB"])
    tank_v_min_if_built = float(cfg["TANK_V_MIN_IF_BUILT"])

    min_abs_share = float(cfg["MIN_ABSORPTION_SHARE"])

    fixed_opex_frac = float(cfg["FIXED_OPEX_FRAC_OF_CAPEX"])
    h2_hold_cost_per_kg_per_day = float(cfg["H2_HOLD_COST_EUR_PER_KG_PER_DAY"])
    max_avg_res_days = float(cfg["MAX_AVG_RESIDENCE_DAYS"])
    daily_sell_max_equiv_hours = float(cfg["DAILY_SELL_MAX_EQUIV_HOURS"])

    r = float(cfg["DISCOUNT_RATE"])
    usd_to_eur = float(cfg["USD_TO_EUR"])

    af_el = annuity_factor(r, float(cfg["EL_LIFETIME_Y"]))
    af_b = annuity_factor(r, float(cfg["BESS_LIFETIME_Y"]))
    af_t = annuity_factor(r, float(cfg["TANK_LIFETIME_Y"]))

    capex_el_per_mw = float(cfg["EL_CAPEX_EUR_PER_MW"])
    capex_b_p = float(cfg["BESS_CAPEX_EUR_PER_MW"])
    capex_b_e = float(cfg["BESS_CAPEX_EUR_PER_MWH"])

    # storage holding cost converted from €/kg-day to €/kg-step
    h2_hold_cost_per_kg_per_step = h2_hold_cost_per_kg_per_day * DT / 24.0

    cons_path = find_file_by_stem(data_dir, "Material_consumption_700bar")
    price_path = find_file_by_stem(data_dir, "Material_price_700bar")
    frac_path = find_file_by_stem(data_dir, "Cost_frac")
    a_usd_per_v, b_usd_fixed, tank_fit_df = fit_tank_capex_line(cons_path, price_path, frac_path)

    dt_index = df["dt"].copy()
    T = list(range(len(df)))
    S_reg = df["S_reg_mw"].to_numpy(dtype=float)
    price_e = df["elec_price_eur_per_mwh"].to_numpy(dtype=float)
    price_h2 = df["h2_price_eur_per_kg"].to_numpy(dtype=float)
    A = np.minimum(rho * S_reg, F_port_mw)
    accessible_energy_mwh = float(np.sum(A) * DT)

    # day groups for daily delivery caps
    day_labels = pd.to_datetime(dt_index, utc=True).dt.floor("D")
    day_to_indices: Dict[pd.Timestamp, List[int]] = {}
    for i, d in enumerate(day_labels):
        day_to_indices.setdefault(d, []).append(i)

    model = gp.Model(f"port_h2_Fport{int(F_port_mw)}_rho{rho}_collect{c_collect_eur_per_mwh}")

    model.Params.LogToConsole = int(cfg["GUROBI_LOGTOCONSOLE"])
    model.Params.MIPGap = float(cfg["GUROBI_MIPGAP"])
    model.Params.Threads = int(cfg["GUROBI_THREADS"])
    model.Params.MIPFocus = int(cfg["GUROBI_MIPFOCUS"])
    model.Params.Heuristics = float(cfg["GUROBI_HEURISTICS"])
    time_limit = cfg.get("GUROBI_TIMELIMIT_S", None)
    if time_limit:
        model.Params.TimeLimit = float(time_limit)

    # ------------------------
    # Capacity vars
    # ------------------------
    z_el = model.addVar(vtype=GRB.BINARY, name="z_el")
    P_el_max = model.addVar(lb=0.0, ub=p_el_ub, vtype=GRB.CONTINUOUS, name="P_el_max_MW")
    P_keep = model.addVar(lb=0.0, ub=p_el_ub, vtype=GRB.CONTINUOUS, name="P_keep_MW")

    P_b_max = model.addVar(lb=0.0, ub=min(p_b_ub, F_port_mw), vtype=GRB.CONTINUOUS, name="P_b_max_MW")
    E_b_max = model.addVar(lb=0.0, ub=e_b_ub, vtype=GRB.CONTINUOUS, name="E_b_max_MWh")

    z_tank = model.addVar(vtype=GRB.BINARY, name="z_tank")
    V_tank = model.addVar(lb=0.0, ub=tank_v_ub, vtype=GRB.CONTINUOUS, name="V_tank")

    # ------------------------
    # Time-step vars
    # ------------------------
    p_ch = model.addVars(T, lb=0.0, ub=min(p_b_ub, F_port_mw), vtype=GRB.CONTINUOUS, name="p_ch_MW")
    p_dis = model.addVars(T, lb=0.0, ub=min(p_b_ub, F_port_mw), vtype=GRB.CONTINUOUS, name="p_dis_MW")
    soc = model.addVars(T, lb=0.0, ub=e_b_ub, vtype=GRB.CONTINUOUS, name="soc_MWh")

    u = model.addVars(T, lb=0.0, ub=min(p_b_ub, F_port_mw), vtype=GRB.CONTINUOUS, name="u_absorb_MW")
    s = model.addVars(T, lb=0.0, ub=max(float(np.max(A)), 0.0), vtype=GRB.CONTINUOUS, name="s_unabsorbed_MW")

    h_sell = model.addVars(T, lb=0.0, vtype=GRB.CONTINUOUS, name="h_sell_kg")
    stock = model.addVars(T, lb=0.0, vtype=GRB.CONTINUOUS, name="h2_stock_kg")

    # BESS charge/discharge mode
    y_ch = model.addVars(T, vtype=GRB.BINARY, name="y_ch")
    y_dis = model.addVars(T, vtype=GRB.BINARY, name="y_dis")

    # ------------------------
    # Capacity logic
    # ------------------------
    model.addConstr(P_el_max <= p_el_ub * z_el, name="el_cap_build")
    if p_el_min_if_built > 0:
        model.addConstr(P_el_max >= p_el_min_if_built * z_el, name="el_cap_min_if_built")

    # Keep-alive does NOT produce H2
    model.addConstr(P_keep == keep_frac * P_el_max, name="keep_alive_link")

    model.addConstr(V_tank <= tank_v_ub * z_tank, name="tank_build_bigM")
    if tank_v_min_if_built > 0:
        model.addConstr(V_tank >= tank_v_min_if_built * z_tank, name="tank_build_minV")

    # per-step sell cap
    sell_cap_expr = sell_flow_factor * k_h2 * P_el_max
    # per-step ramp cap
    sell_ramp_expr = sell_ramp_frac * sell_flow_factor * k_h2 * P_el_max
    # finite tank buffer size
    tank_buffer_cap_expr = buffer_days * 48.0 * sell_flow_factor * k_h2 * P_el_max

    model.addConstr(gamma * V_tank <= tank_buffer_cap_expr, name="tank_buffer_days_limit")

    # ------------------------
    # Time-step constraints
    # ------------------------
    for t in T:
        # BESS mode
        model.addConstr(p_ch[t] <= P_b_max, name=f"ch_cap_{t}")
        model.addConstr(p_dis[t] <= P_b_max, name=f"dis_cap_{t}")
        model.addConstr(p_ch[t] <= p_b_ub * y_ch[t], name=f"ch_mode_{t}")
        model.addConstr(p_dis[t] <= p_b_ub * y_dis[t], name=f"dis_mode_{t}")
        model.addConstr(y_ch[t] + y_dis[t] <= 1, name=f"no_simul_ch_dis_{t}")

        # absorption accounting
        model.addConstr(u[t] == p_ch[t], name=f"u_eq_pch_{t}")
        model.addConstr(u[t] <= rho * float(S_reg[t]), name=f"u_le_rhoS_{t}")
        model.addConstr(u[t] <= F_port_mw, name=f"u_le_Fport_{t}")
        model.addConstr(s[t] == float(A[t]) - u[t], name=f"spill_balance_{t}")

        # SOC
        if t == 0:
            model.addConstr(
                soc[t] == eta_ch * p_ch[t] * DT - (1.0 / eta_dis) * p_dis[t] * DT,
                name="soc_init",
            )
        else:
            model.addConstr(
                soc[t] == soc[t - 1] + eta_ch * p_ch[t] * DT - (1.0 / eta_dis) * p_dis[t] * DT,
                name=f"soc_dyn_{t}",
            )
        model.addConstr(soc[t] <= E_b_max, name=f"soc_cap_{t}")

        # electrolyzer production power only from BESS discharge
        model.addConstr(p_dis[t] <= P_el_max, name=f"el_cap_t_{t}")

        # sell flow cap
        model.addConstr(h_sell[t] <= sell_cap_expr, name=f"h2_sell_cap_{t}")

        # tank capacity
        model.addConstr(stock[t] <= gamma * V_tank, name=f"tank_cap_{t}")

        # hydrogen production
        h_prod_t = k_h2 * p_dis[t]

        if t == 0:
            model.addConstr(stock[t] == h_prod_t - h_sell[t], name="stock_init")
        else:
            model.addConstr(
                stock[t] == (1.0 - h2_loss) * stock[t - 1] + h_prod_t - h_sell[t],
                name=f"stock_dyn_{t}",
            )

        # sell ramp
        if t > 0:
            model.addConstr(h_sell[t] - h_sell[t - 1] <= sell_ramp_expr, name=f"sell_ramp_up_{t}")
            model.addConstr(h_sell[t - 1] - h_sell[t] <= sell_ramp_expr, name=f"sell_ramp_dn_{t}")

    # end-of-year consistency
    model.addConstr(soc[T[-1]] == 0.0, name="soc_terminal_0")
    model.addConstr(stock[T[-1]] == 0.0, name="stock_terminal_0")

    # minimum absorption
    if accessible_energy_mwh > 0 and min_abs_share > 0:
        model.addConstr(
            gp.quicksum(u[t] * DT for t in T) >= min_abs_share * accessible_energy_mwh,
            name="minimum_absorption_target",
        )

    # average residence time cap:
    # sum(stock_t * dt) <= 24 * tau_days * sum(h_sell_t)
    if max_avg_res_days > 0:
        model.addConstr(
            gp.quicksum(stock[t] * DT for t in T)
            <= 24.0 * max_avg_res_days * gp.quicksum(h_sell[t] for t in T),
            name="max_avg_residence_time",
        )

    # daily sell cap
    hourly_h2_per_mw = 1000.0 / sec_kwh_per_kg  # kg/h per MW
    for d, idxs in day_to_indices.items():
        model.addConstr(
            gp.quicksum(h_sell[t] for t in idxs)
            <= daily_sell_max_equiv_hours * hourly_h2_per_mw * P_el_max,
            name=f"daily_sell_cap_{str(d)[:10]}",
        )

    # ------------------------
    # Objective
    # ------------------------
    revenue = gp.quicksum(price_h2[t] * h_sell[t] for t in T)
    keep_alive_elec_cost = gp.quicksum(price_e[t] * P_keep * DT for t in T)
    collect_cost = gp.quicksum(c_collect_eur_per_mwh * u[t] * DT for t in T)

    # overnight CAPEX
    capex_el_overnight = capex_el_per_mw * P_el_max
    capex_bess_overnight = capex_b_p * P_b_max + capex_b_e * E_b_max
    capex_tank_overnight = usd_to_eur * (a_usd_per_v * V_tank + b_usd_fixed * z_tank)
    capex_total_overnight = capex_el_overnight + capex_bess_overnight + capex_tank_overnight

    # annualized CAPEX
    capex_el_ann = af_el * capex_el_overnight
    capex_bess_ann = af_b * capex_bess_overnight
    capex_tank_ann = af_t * capex_tank_overnight

    # fixed OPEX = 5% of overnight CAPEX per year
    fixed_opex_ann = fixed_opex_frac * capex_total_overnight

    # inventory carrying cost
    h2_holding_cost = gp.quicksum(h2_hold_cost_per_kg_per_step * stock[t] for t in T)

    profit = (
        revenue
        - keep_alive_elec_cost
        - collect_cost
        - fixed_opex_ann
        - h2_holding_cost
        - capex_el_ann
        - capex_bess_ann
        - capex_tank_ann
    )
    model.setObjective(profit, GRB.MAXIMIZE)

    if bool(cfg.get("WRITE_MODEL_LP", False)):
        out_lp = os.path.join(
            out_dir,
            f"model_Fport{int(F_port_mw)}_rho{rho}_collect{c_collect_eur_per_mwh}.lp"
        )
        model.write(out_lp)

    model.optimize()

    status_code = model.Status
    status_map = {
        GRB.OPTIMAL: "OPTIMAL",
        GRB.TIME_LIMIT: "TIME_LIMIT",
        GRB.SUBOPTIMAL: "SUBOPTIMAL",
        GRB.INFEASIBLE: "INFEASIBLE",
        GRB.INF_OR_UNBD: "INF_OR_UNBD",
        GRB.UNBOUNDED: "UNBOUNDED",
        GRB.INTERRUPTED: "INTERRUPTED",
    }
    status_str = status_map.get(status_code, str(status_code))

    if status_code in (GRB.INFEASIBLE, GRB.INF_OR_UNBD) and bool(cfg.get("WRITE_IIS_IF_INFEASIBLE", True)):
        try:
            model.computeIIS()
            model.write(
                os.path.join(
                    out_dir,
                    f"iis_Fport{int(F_port_mw)}_rho{rho}_collect{c_collect_eur_per_mwh}.ilp"
                )
            )
        except Exception:
            pass

    if model.SolCount == 0:
        raise RuntimeError(f"No feasible solution returned. Gurobi status = {status_str}")

    # ------------------------
    # Extract
    # ------------------------
    P_el_max_v = float(P_el_max.X)
    P_keep_v = float(P_keep.X)
    P_b_max_v = float(P_b_max.X)
    E_b_max_v = float(E_b_max.X)
    V_tank_v = float(V_tank.X)
    z_tank_v = float(z_tank.X)
    z_el_v = float(z_el.X)

    p_ch_v = np.array([p_ch[t].X for t in T], dtype=float)
    p_dis_v = np.array([p_dis[t].X for t in T], dtype=float)
    soc_v = np.array([soc[t].X for t in T], dtype=float)
    u_v = np.array([u[t].X for t in T], dtype=float)
    s_v = np.array([s[t].X for t in T], dtype=float)
    h_sell_v = np.array([h_sell[t].X for t in T], dtype=float)
    stock_v = np.array([stock[t].X for t in T], dtype=float)

    p_el_prod_v = p_dis_v
    h_prod_v = k_h2 * p_el_prod_v

    # recompute annual values from extracted solution
    revenue_year = float(np.sum(price_h2 * h_sell_v))
    keep_alive_elec_cost_year = float(np.sum(price_e * P_keep_v * DT))
    collect_cost_year = float(np.sum(c_collect_eur_per_mwh * u_v * DT))

    capex_el_overnight_v = float(capex_el_per_mw * P_el_max_v)
    capex_bess_overnight_v = float(capex_b_p * P_b_max_v + capex_b_e * E_b_max_v)
    capex_tank_overnight_v = float(usd_to_eur * (a_usd_per_v * V_tank_v + b_usd_fixed * z_tank_v))
    capex_total_overnight_v = capex_el_overnight_v + capex_bess_overnight_v + capex_tank_overnight_v

    capex_el_ann_v = float(af_el * capex_el_overnight_v)
    capex_bess_ann_v = float(af_b * capex_bess_overnight_v)
    capex_tank_ann_v = float(af_t * capex_tank_overnight_v)

    fixed_opex_ann_v = float(fixed_opex_frac * capex_total_overnight_v)
    h2_holding_cost_year_v = float(np.sum(h2_hold_cost_per_kg_per_step * stock_v))
    objective_v = float(model.ObjVal)

    e_absorbed_mwh = float(np.sum(u_v) * DT)
    e_unused_mwh = float(np.sum(s_v) * DT)
    absorption_rate = 100.0 * e_absorbed_mwh / accessible_energy_mwh if accessible_energy_mwh > 0 else np.nan

    dispatch = pd.DataFrame({
        "dt": dt_index,
        "S_reg_mw": S_reg,
        "A_t_project_accessible_mw": A,
        "u_absorbed_mw": u_v,
        "s_unabsorbed_mw": s_v,
        "p_ch_mw": p_ch_v,
        "p_dis_mw": p_dis_v,
        "soc_mwh": soc_v,
        "P_keep_mw": P_keep_v,
        "p_el_prod_mw": p_el_prod_v,
        "h_prod_kg": h_prod_v,
        "h_sell_kg": h_sell_v,
        "h2_stock_kg": stock_v,
        "elec_price_eur_per_mwh": price_e,
        "h2_price_eur_per_kg": price_h2,
    })

    summary: Dict[str, float | str] = {
        "status": status_str,
        "objective_profit_eur_per_year": objective_v,
        "F_port_mw": float(F_port_mw),
        "rho": float(rho),
        "c_collect_eur_per_mwh": float(c_collect_eur_per_mwh),

        "z_el": z_el_v,
        "P_el_max_mw": P_el_max_v,
        "P_keep_mw": P_keep_v,
        "P_b_max_mw": P_b_max_v,
        "E_b_max_mwh": E_b_max_v,

        "z_tank": z_tank_v,
        "V_tank": V_tank_v,
        "tank_capacity_kg": float(gamma * V_tank_v),
        "tank_max_stock_used_kg": float(np.max(stock_v)) if len(stock_v) else 0.0,

        "H2_prod_year_kg": float(np.sum(h_prod_v)),
        "H2_sell_year_kg": float(np.sum(h_sell_v)),

        "E_absorbed_mwh": e_absorbed_mwh,
        "E_project_accessible_mwh": accessible_energy_mwh,
        "E_unabsorbed_mwh": e_unused_mwh,
        "Absorption_rate_pct": absorption_rate,

        "revenue_year_eur": revenue_year,
        "keep_alive_elec_cost_year_eur": keep_alive_elec_cost_year,
        "collection_cost_year_eur": collect_cost_year,
        "fixed_opex_ann_eur": fixed_opex_ann_v,
        "h2_holding_cost_year_eur": h2_holding_cost_year_v,

        "capex_el_overnight_eur": capex_el_overnight_v,
        "capex_bess_overnight_eur": capex_bess_overnight_v,
        "capex_tank_overnight_eur": capex_tank_overnight_v,
        "capex_total_overnight_eur": capex_total_overnight_v,

        "capex_el_ann_eur": capex_el_ann_v,
        "capex_bess_ann_eur": capex_bess_ann_v,
        "capex_tank_ann_eur": capex_tank_ann_v,

        "tank_capex_line_a_usd_per_V": float(a_usd_per_v),
        "tank_capex_line_b_usd_fixed": float(b_usd_fixed),

        "bess_eta_ch": eta_ch,
        "bess_eta_dis": eta_dis,
        "sec_kwh_per_kg": sec_kwh_per_kg,
        "keep_alive_frac": keep_frac,
        "sell_flow_factor": sell_flow_factor,
        "sell_ramp_frac": sell_ramp_frac,
        "buffer_days_max": buffer_days,
        "min_absorption_share": min_abs_share,
        "fixed_opex_frac_of_capex": fixed_opex_frac,
        "h2_hold_cost_eur_per_kg_per_day": h2_hold_cost_per_kg_per_day,
        "max_avg_residence_days": max_avg_res_days,
        "daily_sell_max_equiv_hours": daily_sell_max_equiv_hours,
        "usd_to_eur": usd_to_eur,

        "model_num_vars": float(model.NumVars),
        "model_num_bin_vars": float(model.NumBinVars),
        "model_num_constrs": float(model.NumConstrs),
        "mip_gap_reported": float(model.MIPGap) if model.SolCount > 0 else np.nan,
    }

    tank_fit_out = os.path.join(
        out_dir,
        f"tank_fit_debug_Fport{int(F_port_mw)}_rho{rho}_collect{c_collect_eur_per_mwh}.csv"
    )
    tank_fit_df.to_csv(tank_fit_out, index=False)

    return ScenarioResult(summary=summary, dispatch=dispatch)


# ============================================================
# 6) Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Run final long-term Gurobi MILP for the port-H2 project.")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Folder containing the absorb curve, price file, material files, and Cost_frac.",
    )
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.abspath(args.data_dir) if args.data_dir else script_dir

    out_dir = os.path.join(data_dir, "outputs_long_term_gurobi_final")
    os.makedirs(out_dir, exist_ok=True)

    print(f"[INFO] Using data_dir = {data_dir}")
    df = load_timeseries(data_dir)
    print(f"[INFO] Loaded merged rows: {len(df)}")
    print(df.head())

    summaries: List[Dict[str, float | str]] = []

    for F_port_mw in CONFIG["F_PORT_LIST_MW"]:
        for rho in CONFIG["RHO_LIST"]:
            for c_collect in CONFIG["C_COLLECT_LIST_EUR_PER_MWH"]:
                print(
                    f"\n[INFO] Solving scenario: F_port={F_port_mw} MW, rho={rho}, "
                    f"C_collect={c_collect} EUR/MWh"
                )
                res = solve_one_scenario(
                    df,
                    data_dir,
                    out_dir,
                    CONFIG,
                    F_port_mw=float(F_port_mw),
                    rho=float(rho),
                    c_collect_eur_per_mwh=float(c_collect),
                )
                summaries.append(res.summary)

                dispatch_name = f"dispatch_Fport{int(F_port_mw)}_rho{rho}_collect{c_collect}.csv"
                res.dispatch.to_csv(os.path.join(out_dir, dispatch_name), index=False)

    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(os.path.join(out_dir, "summary_all_scenarios.csv"), index=False)

    notes = f"""Final model notes
=================
This version differs from the earlier arbitrage-prone model in these critical ways:

1) Grid electricity is modeled as keep-alive power only:
       P_keep = KEEP_ALIVE_FRAC * P_EL_max
   It does NOT produce hydrogen.

2) Hydrogen production comes ONLY from BESS discharge:
       h_prod_t = kappa * p_dis_t

3) H2 selling is regularized:
       h_sell_t <= SELL_FLOW_FACTOR * kappa * P_EL_max
       |h_sell_t - h_sell_t-1| <= SELL_RAMP_FRAC * SELL_FLOW_FACTOR * kappa * P_EL_max

4) H2 tank size is bounded:
       gamma * V_tank <= BUFFER_DAYS_MAX * 48 * SELL_FLOW_FACTOR * kappa * P_EL_max

5) The project must absorb at least MIN_ABSORPTION_SHARE of the accessible annual curtailed energy:
       sum_t(u_t * dt) >= MIN_ABSORPTION_SHARE * sum_t(A_t * dt)

6) Fixed OPEX is added:
       fixed OPEX = FIXED_OPEX_FRAC_OF_CAPEX * overnight CAPEX

7) Inventory carrying cost is added:
       holding cost = H2_HOLD_COST_EUR_PER_KG_PER_DAY applied to stock

8) Average residence time is capped:
       sum(stock_t * dt) <= 24 * MAX_AVG_RESIDENCE_DAYS * sum(h_sell_t)

9) Daily H2 delivery is capped:
       sum_{t in day}(h_sell_t) <= DAILY_SELL_MAX_EQUIV_HOURS * (1000/SEC) * P_EL_max

Default CONFIG
--------------
DT_H = {CONFIG['DT_H']}
F_PORT_LIST_MW = {CONFIG['F_PORT_LIST_MW']}
RHO_LIST = {CONFIG['RHO_LIST']}
C_COLLECT_LIST_EUR_PER_MWH = {CONFIG['C_COLLECT_LIST_EUR_PER_MWH']}
ETA_CH = {CONFIG['ETA_CH']}
ETA_DIS = {CONFIG['ETA_DIS']}
SEC_KWH_PER_KG = {CONFIG['SEC_KWH_PER_KG']}
P_EL_UB_MW = {CONFIG['P_EL_UB_MW']}
P_B_UB_MW = {CONFIG['P_B_UB_MW']}
E_B_UB_MWH = {CONFIG['E_B_UB_MWH']}
KEEP_ALIVE_FRAC = {CONFIG['KEEP_ALIVE_FRAC']}
SELL_FLOW_FACTOR = {CONFIG['SELL_FLOW_FACTOR']}
SELL_RAMP_FRAC = {CONFIG['SELL_RAMP_FRAC']}
BUFFER_DAYS_MAX = {CONFIG['BUFFER_DAYS_MAX']}
MIN_ABSORPTION_SHARE = {CONFIG['MIN_ABSORPTION_SHARE']}
FIXED_OPEX_FRAC_OF_CAPEX = {CONFIG['FIXED_OPEX_FRAC_OF_CAPEX']}
H2_HOLD_COST_EUR_PER_KG_PER_DAY = {CONFIG['H2_HOLD_COST_EUR_PER_KG_PER_DAY']}
MAX_AVG_RESIDENCE_DAYS = {CONFIG['MAX_AVG_RESIDENCE_DAYS']}
DAILY_SELL_MAX_EQUIV_HOURS = {CONFIG['DAILY_SELL_MAX_EQUIV_HOURS']}
H2_CAPACITY_PER_V_KG = {CONFIG['H2_CAPACITY_PER_V_KG']}
EL_CAPEX_EUR_PER_MW = {CONFIG['EL_CAPEX_EUR_PER_MW']}
BESS_CAPEX_EUR_PER_MW = {CONFIG['BESS_CAPEX_EUR_PER_MW']}
BESS_CAPEX_EUR_PER_MWH = {CONFIG['BESS_CAPEX_EUR_PER_MWH']}
DISCOUNT_RATE = {CONFIG['DISCOUNT_RATE']}
USD_TO_EUR = {CONFIG['USD_TO_EUR']}
GUROBI_TIMELIMIT_S = {CONFIG['GUROBI_TIMELIMIT_S']}
"""
    with open(os.path.join(out_dir, "model_notes.txt"), "w", encoding="utf-8") as f:
        f.write(notes)

    print(f"\n[INFO] Done. Outputs written to: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())