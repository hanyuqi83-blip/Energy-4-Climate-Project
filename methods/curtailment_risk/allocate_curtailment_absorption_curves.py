#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
allocate_curtailment_absorption_curves.py

Goal / 目标
-----------
Given:
  1) eco2mix-France-cons-def.csv  (NOT a national total; it contains one row per region per timestamp)
  2) eco2mix-regional-cons-def.csv (one chosen region, e.g., Hauts-de-France)

We will:
  A) Build the TRUE national time-series by summing all regions by timestamp.
  B) Define "union_hours" (high-risk windows) on the NATIONAL series:
       union = PV1 ∪ W1 ∪ W2(all-day) ∪ PV2(daytime-only)
  C) Allocate a national curtailment energy target (default: 4 TWh/year) across union_hours
     to create a 30-min "power-to-absorb" curve P_nat(t) [MW], whose integral equals target.
  D) Allocate the corresponding regional share (sC) of that energy to the REGION, with an export-proxy priority:
       export_mw = - Ech. physiques (MW)
     Cap absorption:
       P_reg(t) ≤ min(export_mw(t), VRE_region(t))   (within union_hours)

Outputs / 输出（默认）
--------------------
- 写入：<弃电问题>/outputs/absorption_allocation_outputs/
- 同时复制关键文件到：<Energy 工程 Part>/short Time/

How to run / 运行方法
--------------------
(1) 直接运行（推荐）：
    python allocate_curtailment_absorption_curves.py

(2) 或指定路径：
    python allocate_curtailment_absorption_curves.py \
      --france "C:\\path\\eco2mix-France-cons-def.csv" \
      --region "C:\\path\\eco2mix-regional-cons-def.csv" \
      --target_twh 4

Author: (your assistant)
"""

import argparse  # parse CLI args
from pathlib import Path  # robust paths
import zipfile  # zip outputs

import numpy as np  # numeric
import pandas as pd  # data

import matplotlib.pyplot as plt  # plots


DT_H = 0.5  # eco2mix time step: 30 minutes = 0.5 hour

SCRIPT_DIR = Path(__file__).resolve().parent  # scripts/
PROJECT_DIR = SCRIPT_DIR.parent  # 弃电问题/
ENERGY_DIR = PROJECT_DIR.parent  # Energy 工程 Part/
DATA_DIR = PROJECT_DIR / "data"  # 弃电问题/data
OUTPUTS_DIR = PROJECT_DIR / "outputs_allocate_curtailment"  # 弃电问题/outputs
DEFAULT_OUTDIR = OUTPUTS_DIR / "absorption_allocation_outputs"  # 默认输出目录
DEFAULT_SHORT_TIME_DIR = ENERGY_DIR / "short Time"  # 默认 short Time 目录


def resolve_input_path(user_value: str, fallback_dir: Path) -> Path:  # resolve input path
    p = Path(user_value)  # parse path
    if p.is_absolute() and p.exists():  # absolute and exists
        return p  # return it
    if p.exists():  # relative but exists from cwd
        return p.resolve()  # return resolved
    cand = fallback_dir / user_value  # try in fallback dir
    if cand.exists():  # if found
        return cand.resolve()  # return resolved
    cand2 = PROJECT_DIR / user_value  # try in project root
    if cand2.exists():  # if found
        return cand2.resolve()  # return resolved
    raise FileNotFoundError(f"Cannot find input: {user_value} (also tried {fallback_dir} and {PROJECT_DIR})")  # raise error


def resolve_outdir(user_value: str) -> Path:  # resolve output directory
    p = Path(user_value)  # parse path
    if p.is_absolute():  # absolute path
        return p  # return it
    return (PROJECT_DIR / p).resolve()  # make it relative to project dir


def load_eco2mix(path: str, is_france: bool) -> tuple[pd.DataFrame, int]:  # load eco2mix
    df = pd.read_csv(path, sep=";", low_memory=False)  # read csv
    df.columns = [c.strip() for c in df.columns]  # strip headers

    if "Date - Heure" not in df.columns:  # check timestamp column
        raise ValueError(f"Missing 'Date - Heure' column in {path}")  # raise error

    dt_utc = pd.to_datetime(df["Date - Heure"], errors="coerce", utc=True)  # parse to UTC
    df = df.loc[~dt_utc.isna()].copy()  # drop bad timestamps
    df["dt"] = dt_utc.loc[~dt_utc.isna()].dt.tz_convert("Europe/Paris")  # convert to Europe/Paris

    keep = ["dt"]  # init keep columns
    if "Région" in df.columns:  # include region for France file
        keep.append("Région")  # keep region column

    candidates = [  # candidate numeric columns
        "Consommation (MW)",
        "Eolien (MW)",
        "Solaire (MW)",
        "Ech. physiques (MW)",
    ]
    for c in candidates:  # keep existing candidate columns
        if c in df.columns:
            keep.append(c)

    df = df[keep].copy()  # slice relevant columns

    for c in candidates:  # cast numeric
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    before = len(df)  # count rows before dedup
    if is_france:  # dedup rule for France file
        if "Région" not in df.columns:
            raise ValueError("France file must contain 'Région' (one row per region per timestamp).")
        df = df.drop_duplicates(subset=["dt", "Région"], keep="first")
    else:  # dedup rule for region file
        df = df.drop_duplicates(subset=["dt"], keep="first")

    dups_removed = before - len(df)  # compute removed duplicates
    return df.sort_values("dt").reset_index(drop=True), dups_removed  # return cleaned df


def timestep_deltas_minutes(df: pd.DataFrame) -> dict:  # compute delta histogram
    deltas = df["dt"].diff().dropna().dt.total_seconds().div(60).round().astype(int)  # compute minutes delta
    return deltas.value_counts().to_dict()  # return histogram


def build_union_hours(fr_idx: pd.DataFrame) -> dict:  # build union masks
    wind = fr_idx["wind"].astype(float).fillna(0.0)  # wind series
    solar = fr_idx["solar"].astype(float).fillna(0.0)  # solar series
    conso = fr_idx["conso"].astype(float).replace(0, np.nan)  # demand series

    hours = fr_idx.index.hour  # hour vector
    months = fr_idx.index.month  # month vector
    weekday = fr_idx.index.weekday  # weekday vector
    is_weekend = weekday >= 5  # weekend flag

    wind_p75 = float(wind.quantile(0.75))  # wind P75
    solar_mask_for_threshold = (hours >= 8) & (hours <= 17) & (solar > 0)  # daylight solar mask
    solar_p75 = float(solar[solar_mask_for_threshold].quantile(0.75))  # solar P75

    pv1_mask = (months >= 4) & (months <= 9) & (hours >= 10) & (hours < 16) & (solar >= solar_p75)  # PV1
    w1_mask = (wind >= wind_p75) & (is_weekend | (hours >= 22) | (hours < 6))  # W1

    daily_solar_mwh = (solar * DT_H).groupby(fr_idx.index.date).sum()  # daily solar energy
    daily_solar_mwh.index = pd.to_datetime(daily_solar_mwh.index)  # set datetime index
    daily_solar_mwh_ss = daily_solar_mwh[(daily_solar_mwh.index.month >= 4) & (daily_solar_mwh.index.month <= 9)]  # Apr-Sep
    pv2_p80 = float(daily_solar_mwh_ss.quantile(0.80)) if len(daily_solar_mwh_ss) else 0.0  # PV2 P80
    high_solar_day = (daily_solar_mwh_ss >= pv2_p80).astype(int)  # high solar day flag

    pv2_days = set()  # pv2 day set
    if len(high_solar_day):  # check non-empty
        dts = high_solar_day.index.sort_values()  # sorted days
        flags = high_solar_day.reindex(dts).values  # flags array
        start = None  # start day
        for i, (d, f) in enumerate(zip(dts, flags)):  # iterate
            if f == 1 and start is None:
                start = d
            if (f == 0 or i == len(flags) - 1) and start is not None:
                end = dts[i - 1] if f == 0 else d
                length = (end - start).days + 1
                if length >= 2:
                    pv2_days.update([(start + pd.Timedelta(days=k)).date() for k in range(length)])
                start = None

    pv2_mask = pd.Series(False, index=fr_idx.index)  # init PV2 mask
    if pv2_days:
        pv2_mask.loc[[t for t in fr_idx.index if (t.date() in pv2_days) and (8 <= t.hour < 18)]] = True  # mark PV2 daylight

    daily_wind_mean = wind.groupby(fr_idx.index.date).mean()  # daily wind mean
    daily_wind_mean.index = pd.to_datetime(daily_wind_mean.index)  # set datetime index
    w2_p80 = float(daily_wind_mean.quantile(0.80)) if len(daily_wind_mean) else 0.0  # W2 P80
    high_wind_day = (daily_wind_mean >= w2_p80).astype(int)  # high wind day flag

    w2_days = set()  # w2 day set
    if len(high_wind_day):
        dts = high_wind_day.index.sort_values()
        flags = high_wind_day.reindex(dts).values
        start = None
        for i, (d, f) in enumerate(zip(dts, flags)):
            if f == 1 and start is None:
                start = d
            if (f == 0 or i == len(flags) - 1) and start is not None:
                end = dts[i - 1] if f == 0 else d
                length = (end - start).days + 1
                if length >= 2:
                    w2_days.update([(start + pd.Timedelta(days=k)).date() for k in range(length)])
                start = None

    w2_mask = pd.Series(False, index=fr_idx.index)  # init W2 mask
    if w2_days:
        w2_mask.loc[[t for t in fr_idx.index if t.date() in w2_days]] = True  # mark W2 all-day

    union_mask = pv1_mask | w1_mask | pv2_mask | w2_mask  # union mask

    return {  # return dict
        "wind_p75": wind_p75,
        "solar_p75": solar_p75,
        "pv2_p80": pv2_p80,
        "w2_p80": w2_p80,
        "pv1_mask": pv1_mask,
        "w1_mask": w1_mask,
        "pv2_mask": pv2_mask,
        "w2_mask": w2_mask,
        "union_mask": union_mask,
        "pv2_days": pv2_days,
        "w2_days": w2_days,
    }


def allocate_national_curve(fr_idx: pd.DataFrame, union_mask: pd.Series, target_twh: float) -> pd.DataFrame:  # allocate national curve
    wind = fr_idx["wind"].astype(float).fillna(0.0)  # wind series
    solar = fr_idx["solar"].astype(float).fillna(0.0)  # solar series
    conso = fr_idx["conso"].astype(float).replace(0, np.nan)  # demand series

    vre = wind + solar  # VRE series
    stress = (vre * (1.0 + (vre / (conso + 1.0)))).fillna(0.0)  # stress weights

    w = stress.where(union_mask, 0.0)  # keep only union
    wsum = float(w.sum())  # sum weights
    if wsum <= 0:
        raise ValueError("Union-hour weight sum is zero; cannot allocate.")

    E_total_mwh = target_twh * 1_000_000.0  # convert TWh -> MWh
    energy_mwh = (E_total_mwh * (w / wsum)).where(union_mask, 0.0)  # allocate energy
    absorb_need_mw = energy_mwh / DT_H  # convert to MW

    out = pd.DataFrame({  # build output df
        "dt": fr_idx.index,
        "in_union": union_mask.astype(int).values,
        "wind_mw": wind.values,
        "solar_mw": solar.values,
        "conso_mw": fr_idx["conso"].astype(float).fillna(0.0).values,
        "vre_mw": vre.values,
        "stress_weight": w.values,
        "absorb_need_mw": absorb_need_mw.values,
    })
    return out  # return df


def allocate_with_caps(total_energy_mwh: float, weights: pd.Series, cap_mw: pd.Series, dt_h: float = 0.5,
                       max_iter: int = 80, tol_mwh: float = 1e-6) -> tuple[pd.Series, float]:  # allocate with caps
    weights = weights.astype(float).copy()  # copy weights
    cap_mw = cap_mw.astype(float).copy()  # copy caps
    alloc_mw = pd.Series(0.0, index=weights.index)  # init allocation

    remaining = float(total_energy_mwh)  # init remaining energy
    active = (weights > 0) & (cap_mw > 0)  # active timesteps

    for _ in range(max_iter):  # iterate redistribution
        if remaining <= tol_mwh:
            break

        w_act = weights.where(active, 0.0)  # active weights
        wsum = float(w_act.sum())  # sum active weights
        if wsum <= 0:
            break

        prov_energy = remaining * (w_act / wsum)  # provisional energy
        prov_mw = prov_energy / dt_h  # convert to MW

        room = (cap_mw - alloc_mw).clip(lower=0.0)  # headroom under cap
        add = pd.concat([prov_mw, room], axis=1).min(axis=1).where(active, 0.0)  # allocate limited by cap

        alloc_mw += add  # update allocation
        used = float((add * dt_h).sum())  # compute used energy
        remaining -= used  # update remaining

        active = active & ((cap_mw - alloc_mw) > 1e-9)  # deactivate capped timesteps

    return alloc_mw, remaining  # return allocation and leftover


def allocate_region_curve(nat_curve: pd.DataFrame, reg_idx: pd.DataFrame, union_mask: pd.Series,
                          target_twh: float) -> tuple[pd.DataFrame, float, float]:  # allocate region curve
    fr = nat_curve.set_index("dt")  # set dt index for nat curve
    common = fr.index.intersection(reg_idx.index)  # common timestamps

    fr_common = fr.loc[common]  # slice national
    reg_common = reg_idx.loc[common]  # slice region
    union_common = union_mask.reindex(common, fill_value=False)  # align union mask

    reg_wind = reg_common["wind"].astype(float).fillna(0.0)  # region wind
    reg_solar = reg_common["solar"].astype(float).fillna(0.0)  # region solar
    reg_conso = reg_common["conso"].astype(float).fillna(0.0)  # region demand
    reg_vre = reg_wind + reg_solar  # region VRE

    export_mw = (-reg_common["ech_phys"].astype(float)).fillna(0.0)  # export proxy

    fr_vre = fr_common["vre_mw"].astype(float)  # national VRE
    num = float((reg_vre[union_common].sum() * DT_H))  # region VRE energy in union
    den = float((fr_vre[union_common].sum() * DT_H))  # national VRE energy in union
    sC = num / den if den > 0 else 0.0  # compute share

    E_total_mwh = target_twh * 1_000_000.0  # convert TWh -> MWh
    E_reg_mwh = E_total_mwh * sC  # region target energy

    weights = (export_mw.clip(lower=0.0) ** 2).where(union_common, 0.0)  # export-squared weights
    cap_mw = pd.concat([export_mw.clip(lower=0.0), reg_vre.clip(lower=0.0)], axis=1).min(axis=1).where(union_common, 0.0)  # physical caps

    alloc_mw, remaining_mwh = allocate_with_caps(E_reg_mwh, weights, cap_mw, dt_h=DT_H)  # capped allocation

    region_curve = pd.DataFrame({  # build region output
        "dt": common,
        "in_union": union_common.astype(int).values,
        "export_mw": export_mw.values,
        "wind_mw": reg_wind.values,
        "solar_mw": reg_solar.values,
        "conso_mw": reg_conso.values,
        "vre_mw": reg_vre.values,
        "cap_mw_min_export_vre": cap_mw.values,
        "weight_export_sq": weights.values,
        "absorb_need_mw_region": alloc_mw.values,
    })
    region_curve["export_after_mw"] = (region_curve["export_mw"] - region_curve["absorb_need_mw_region"]).clip(lower=0.0)  # compute export after absorb

    unallocated_twh = remaining_mwh / 1_000_000.0  # leftover in TWh
    return region_curve, sC, unallocated_twh  # return results


def plot_duration(series: pd.Series, title: str, outpath: Path) -> None:  # plot duration curve
    s = series.sort_values(ascending=False).reset_index(drop=True)  # sort
    plt.figure(figsize=(10, 4))  # new figure
    plt.plot(s.values)  # plot series
    plt.xlabel("30-min intervals ranked by absorb_need (descending)")  # x label
    plt.ylabel("Absorb need (MW)")  # y label
    plt.title(title)  # title
    plt.tight_layout()  # layout
    plt.savefig(outpath)  # save fig
    plt.close()  # close fig


def copy_key_outputs_to_short_time(outdir: Path, short_time_dir: Path) -> None:  # copy outputs to short time
    short_time_dir.mkdir(parents=True, exist_ok=True)  # ensure short time dir exists
    src = outdir / "region_absorb_need_curve_30min.csv"  # source region curve
    if src.exists():  # check exists
        dst = short_time_dir / "region_absorb_need_curve_30min.csv"  # destination file
        dst.write_bytes(src.read_bytes())  # copy bytes
    src2 = outdir / "allocation_summary.txt"  # source summary
    if src2.exists():  # check exists
        dst2 = short_time_dir / "allocation_summary.txt"  # destination summary
        dst2.write_bytes(src2.read_bytes())  # copy bytes


def main():  # main entry
    parser = argparse.ArgumentParser()  # create parser
    parser.add_argument("--france", type=str, default=str(DATA_DIR / "eco2mix-France-cons-def.csv"),
                        help="Path to eco2mix-France-cons-def.csv (regional rows).")  # france file
    parser.add_argument("--region", type=str, default=str(DATA_DIR / "eco2mix-regional-cons-def.csv"),
                        help="Path to eco2mix-regional-cons-def.csv (one region, e.g., Hauts-de-France).")  # region file
    parser.add_argument("--target_twh", type=float, default=4.0,
                        help="Annual national curtailment energy to absorb (TWh/year). Default: 4.")  # target
    parser.add_argument("--outdir", type=str, default=str(DEFAULT_OUTDIR),
                        help="Output directory. Default: <弃电问题>/outputs/absorption_allocation_outputs")  # outdir
    parser.add_argument("--short_time_dir", type=str, default=str(DEFAULT_SHORT_TIME_DIR),
                        help="Short Time directory to copy key outputs into.")  # short time dir
    parser.add_argument("--no_copy_to_short_time", action="store_true",
                        help="Disable copying outputs to short Time folder.")  # disable copy
    args = parser.parse_args()  # parse args

    fr_path = resolve_input_path(args.france, DATA_DIR)  # resolve france path
    reg_path = resolve_input_path(args.region, DATA_DIR)  # resolve region path
    outdir = resolve_outdir(args.outdir)  # resolve output dir
    outdir.mkdir(parents=True, exist_ok=True)  # create output dir

    short_time_dir = Path(args.short_time_dir)  # parse short time dir
    if not short_time_dir.is_absolute():  # make short time relative to energy dir
        short_time_dir = (ENERGY_DIR / short_time_dir).resolve()  # resolve short time dir

    fr_regional, fr_dups = load_eco2mix(str(fr_path), is_france=True)  # load france file
    reg_df, reg_dups = load_eco2mix(str(reg_path), is_france=False)  # load region file

    fr_deltas = timestep_deltas_minutes(fr_regional.drop_duplicates(subset=["dt"]))  # france deltas
    reg_deltas = timestep_deltas_minutes(reg_df)  # region deltas

    num_cols = [c for c in ["Consommation (MW)", "Eolien (MW)", "Solaire (MW)"] if c in fr_regional.columns]  # numeric cols
    fr_nat = fr_regional.groupby("dt", as_index=False)[num_cols].sum().sort_values("dt").reset_index(drop=True)  # aggregate to national

    fr_idx = fr_nat.set_index("dt").rename(columns={  # rename nat columns
        "Consommation (MW)": "conso",
        "Eolien (MW)": "wind",
        "Solaire (MW)": "solar",
    })

    reg_idx = reg_df.set_index("dt").rename(columns={  # rename region columns
        "Consommation (MW)": "conso",
        "Eolien (MW)": "wind",
        "Solaire (MW)": "solar",
        "Ech. physiques (MW)": "ech_phys",
    })

    for col in ["conso", "wind", "solar"]:  # validate required columns
        if col not in fr_idx.columns:
            raise ValueError(f"National series missing required column: {col}")
        if col not in reg_idx.columns:
            raise ValueError(f"Region series missing required column: {col}")
    if "ech_phys" not in reg_idx.columns:
        raise ValueError("Region series missing 'Ech. physiques (MW)' needed for export proxy.")

    union = build_union_hours(fr_idx)  # build union masks
    union_mask = union["union_mask"]  # get union mask

    nat_curve = allocate_national_curve(fr_idx, union_mask, target_twh=args.target_twh)  # allocate national curve
    nat_energy_twh = float((nat_curve["absorb_need_mw"] * DT_H).sum() / 1_000_000.0)  # check national energy

    region_curve, sC, unalloc_twh = allocate_region_curve(nat_curve, reg_idx, union_mask, target_twh=args.target_twh)  # allocate region curve
    reg_energy_twh = float((region_curve["absorb_need_mw_region"] * DT_H).sum() / 1_000_000.0)  # check region energy

    nat_csv = outdir / "national_absorb_need_curve_30min.csv"  # nat csv path
    reg_csv = outdir / "region_absorb_need_curve_30min.csv"  # region csv path
    nat_curve.to_csv(nat_csv, index=False)  # write national csv
    region_curve.to_csv(reg_csv, index=False)  # write region csv

    plot_duration(nat_curve.loc[nat_curve["in_union"] == 1, "absorb_need_mw"],  # nat duration series
                  f"National absorb-need duration curve ({args.target_twh:.1f} TWh on union_hours)",  # nat title
                  outdir / "national_duration_curve.png")  # nat plot path
    plot_duration(region_curve.loc[region_curve["in_union"] == 1, "absorb_need_mw_region"],  # region duration series
                  "Region absorb-need duration curve (share-based + export-capped)",  # region title
                  outdir / "region_duration_curve.png")  # region plot path

    max_dt = region_curve.loc[region_curve["export_mw"].idxmax(), "dt"]  # find max export timestamp
    window = (region_curve["dt"] >= (max_dt - pd.Timedelta(days=3))) & (region_curve["dt"] <= (max_dt + pd.Timedelta(days=3)))  # build window
    tmp = region_curve.loc[window].copy()  # slice window
    plt.figure(figsize=(12, 4))  # new figure
    plt.plot(tmp["dt"], tmp["export_mw"], label="export (before)")  # plot before
    plt.plot(tmp["dt"], tmp["export_after_mw"], label="export (after absorb)")  # plot after
    plt.xlabel("Time")  # x label
    plt.ylabel("MW")  # y label
    plt.title("Region export proxy around max-export week (before vs after absorb allocation)")  # title
    plt.legend()  # legend
    plt.tight_layout()  # layout
    plt.savefig(outdir / "region_export_before_after_week.png")  # save fig
    plt.close()  # close fig

    union_hours = float(union_mask.sum() * DT_H)  # union hours
    pv1_hours = float(union["pv1_mask"].sum() * DT_H)  # pv1 hours
    w1_hours = float(union["w1_mask"].sum() * DT_H)  # w1 hours
    pv2_days = len(union["pv2_days"])  # pv2 days count
    w2_days = len(union["w2_days"])  # w2 days count

    summary_path = outdir / "allocation_summary.txt"  # summary path
    lines = []  # init lines
    lines.append("=== INPUT FILES ===")
    lines.append(f"France file  : {fr_path}")
    lines.append(f"Region file  : {reg_path}")
    lines.append("")
    lines.append("=== DATA QUALITY CHECKS (DST / duplicates / gaps) ===")
    lines.append(f"[France] duplicates removed before aggregation: {fr_dups}")
    lines.append(f"[France] observed time-step deltas (minutes): {fr_deltas}")
    lines.append(f"[Region] duplicates removed: {reg_dups}")
    lines.append(f"[Region] observed time-step deltas (minutes): {reg_deltas}")
    lines.append("")
    lines.append("=== NATIONAL THRESHOLDS ===")
    lines.append(f"Wind P75 (MW)                           : {union['wind_p75']:,.1f}")
    lines.append(f"Solar P75 (MW) [08:00–17:59 & solar>0]  : {union['solar_p75']:,.1f}")
    lines.append(f"PV2 daily solar energy P80 (MWh/day)     : {union['pv2_p80']:,.1f}")
    lines.append(f"W2 daily mean wind P80 (MW)             : {union['w2_p80']:,.1f}")
    lines.append("")
    lines.append("=== WINDOW COVERAGE ===")
    lines.append(f"PV1 hours (h/year)   : {pv1_hours:,.1f}")
    lines.append(f"W1 hours  (h/year)   : {w1_hours:,.1f}")
    lines.append(f"PV2 event-days       : {pv2_days}")
    lines.append(f"W2 event-days        : {w2_days}")
    lines.append(f"Union hours (h/year) : {union_hours:,.1f}")
    lines.append("")
    lines.append("=== NATIONAL ALLOCATION ===")
    lines.append(f"Target energy (TWh)                 : {args.target_twh:,.3f}")
    lines.append(f"Energy check from curve (TWh)       : {nat_energy_twh:,.6f}")
    lines.append(f"Peak absorb_need_mw (MW)            : {nat_curve['absorb_need_mw'].max():,.1f}")
    lines.append("")
    lines.append("=== REGION ALLOCATION ===")
    lines.append(f"sC = region share inside union_hours: {sC*100:,.2f}%")
    lines.append(f"Region energy implied (TWh)         : {args.target_twh*sC:,.6f}")
    lines.append(f"Energy check from curve (TWh)       : {reg_energy_twh:,.6f}")
    lines.append(f"Unallocated due to caps (TWh)       : {unalloc_twh:,.6f}")
    lines.append(f"Peak absorb_need_mw_region (MW)     : {region_curve['absorb_need_mw_region'].max():,.1f}")
    summary_path.write_text("\n".join(lines), encoding="utf-8")  # write summary

    zip_path = outdir / "absorption_allocation_outputs.zip"  # zip path
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:  # create zip
        for p in outdir.rglob("*"):  # iterate files
            if p.is_file() and p.name != zip_path.name:
                z.write(p, arcname=str(p.relative_to(outdir)))  # add file

    if (not args.no_copy_to_short_time) and DEFAULT_SHORT_TIME_DIR.exists():  # check copy condition
        copy_key_outputs_to_short_time(outdir, short_time_dir)  # copy key outputs

    print(f"[OK] Outputs written to: {outdir}")  # print output dir
    print(f"     National energy check: {nat_energy_twh:.6f} TWh (target={args.target_twh:.3f} TWh)")  # print nat check
    print(f"     Region sC: {sC*100:.2f}% -> region energy {args.target_twh*sC:.6f} TWh, unallocated={unalloc_twh:.6f} TWh")  # print region check
    print(f"     Zip: {zip_path}")  # print zip path
    if (not args.no_copy_to_short_time) and DEFAULT_SHORT_TIME_DIR.exists():  # print copy status
        print(f"     Copied to short Time: {short_time_dir / 'region_absorb_need_curve_30min.csv'}")  # print copied file


if __name__ == "__main__":  # entry
    main()  # run
