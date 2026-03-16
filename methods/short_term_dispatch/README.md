# Short-Term Dispatch Methods

This folder contains the short-term dispatch optimization model.

Files in this folder include:
- `short time MILP.py`

This model is used to simulate the allocation of renewable surplus across batteries, power-to-heat, and electrolysis under different absorption levels.

---

## 1. Model Purpose

The short-term model is formulated as a **rolling-horizon MILP** at **30-minute resolution**. It is designed to evaluate how three co-existing flexibility assets absorb project-accessible curtailed electricity:

- **Battery Energy Storage System (BESS)**
- **Power-to-Heat (P2H)** with electric heater and thermal energy storage
- **Power-to-Hydrogen (P2H2)** with electrolyzer, hydrogen storage, and hydrogen sales

The model is **not** intended as a long-term investment optimizer. Its purpose is to test the **operational suitability** of different flexibility assets under short-term curtailment conditions. It also supports multiple `alpha` scenarios and an optional strict full-absorption feasibility test.

---

## 2. Main Modelling Assumptions

The script is built on several explicit assumptions:

- **No electricity import and no electricity export**
- Project-accessible curtailed electricity is given by `absorb_need_mw`
- The original regional curtailment curve is preserved as `absorb_need_mw_region`
- Heat demand is an exogenous input `heat_demand_mw_th`
- Hydrogen sales are allowed
- Unmet heat demand is allowed through a slack variable with penalty
- Optional strict mode can enforce full curtailment absorption with zero unused energy and zero power-balance slack

These assumptions make the model an operational screening tool rather than a market arbitrage model.

---

## 3. Time Set and Main Inputs

Let $t \in \mathcal{T}$ denote each 30-minute time step. The time-step length is:

$$
\Delta t = 0.5 \text{ h}
$$

Main exogenous inputs are:

- $A_t$: project-accessible curtailed electricity (`absorb_need_mw`)
- $A_t^{reg}$: regional absorb-need curve (`absorb_need_mw_region`)
- $D_t^{th}$: exogenous heat demand
- $\pi_t^{H_2}$: hydrogen sale price

The script can merge the curtailment time series with a hydrogen price series, and it solves multiple alpha scenarios in one run. The default CLI alpha list is:

$$
\alpha \in \{0.005,\;0.0066,\;0.01\}
$$

meaning 0.5%, 0.66%, and 1% of the regional absorb-need are made accessible to the project.

---

## 4. Decision Variables

### 4.1 Curtailment use

- $P_t^{curt}$: curtailed electricity actually used by the project
- $U_t$: unused curtailed electricity

### 4.2 Battery

- $P_t^{b,ch}$: battery charging power
- $P_t^{b,dis}$: battery discharging power
- $SOC_t^b$: battery state of charge
- $y_t^{b,ch},\;y_t^{b,dis} \in \{0,1\}$: charge/discharge mode binaries

### 4.3 Power-to-Heat

- $P_t^{p2h}$: electric power into the heater
- $Q_t^{th,dis}$: thermal discharge from TES
- $SOC_t^{th}$: thermal storage state of charge
- $y_t^{th,ch},\;y_t^{th,dis} \in \{0,1\}$: thermal charge/discharge binaries
- $Slack_t^{th}$: unmet heat-demand slack

### 4.4 Electrolyzer and hydrogen

- $P_t^{el}$: electrolyzer power
- $y_t^{el,on},\;y_t^{el,start},\;y_t^{el,stop} \in \{0,1\}$: on/off, start, stop binaries
- $H_t^{prod}$: hydrogen production
- $H_t^{sale}$: hydrogen sale
- $SOC_t^{H_2}$: hydrogen inventory

### 4.5 Power-balance slacks

- $P_t^{def}$: power deficit slack
- $P_t^{sur}$: power surplus slack

These variables are all defined explicitly in the MILP model built by `build_and_solve_window(...)`.

---

## 5. Curtailment Absorption Constraints

The project cannot use more curtailed electricity than is available:

$$
P_t^{curt} \le A_t
$$

Unused curtailed electricity is defined through:

$$
U_t \ge A_t - P_t^{curt}
$$

When the script is run in strict full-absorption verification mode, the model enforces:

$$
P_t^{curt} = A_t
$$

$$
U_t = 0
$$

This turns the model into a feasibility check for complete curtailment absorption under all technical constraints.

---

## 6. Battery Model

### 6.1 Charge/discharge exclusivity

The battery cannot charge and discharge simultaneously:

$$
y_t^{b,ch} + y_t^{b,dis} \le 1
$$

Charging and discharging powers are bounded by installed battery power:

$$
P_t^{b,ch} \le \overline{P}_b \, y_t^{b,ch}
$$

$$
P_t^{b,dis} \le \overline{P}_b \, y_t^{b,dis}
$$

### 6.2 State-of-charge dynamics

Battery SOC evolves as:

For the first step of each rolling window:
$$
SOC_0^b = SOC_{\text{init}}^b + \eta^{ch} P_0^{b,ch}\Delta t - \frac{1}{\eta^{dis}} P_0^{b,dis}\Delta t
$$

For subsequent steps:
$$
SOC_t^b = SOC_{t-1}^b + \eta^{ch} P_t^{b,ch}\Delta t - \frac{1}{\eta^{dis}} P_t^{b,dis}\Delta t, \qquad t>0
$$

with bounds:

$$
SOC_{\min}^b \le SOC_t^b \le SOC_{\max}^b
$$

The minimum and maximum SOC are defined as fractions of battery energy capacity in the parameter set.

---

## 7. Power-to-Heat and Thermal Storage Model

### 7.1 Mode exclusivity and power limits

The thermal subsystem also uses mutually exclusive charging and discharging modes:

$$
y_t^{th,ch} + y_t^{th,dis} \le 1
$$

The electric heater is bounded by its capacity:

$$
P_t^{p2h} \le \overline{P}_{p2h}\, y_t^{th,ch}
$$

TES discharge is bounded by its thermal discharge limit:

$$
Q_t^{th,dis} \le \overline{Q}_{th,dis}\, y_t^{th,dis}
$$

### 7.2 Thermal charging and storage dynamics

Thermal charging is linked to electric heater input via efficiency:

$$
Q_t^{th,ch} = \eta_{p2h} P_t^{p2h}
$$

Thermal SOC evolves as:

For the first step of each rolling window:
$$
SOC_0^{th} = (1-\lambda_{th})\,SOC_{\text{init}}^{th} + Q_0^{th,ch}\Delta t - Q_0^{th,dis}\Delta t
$$

For subsequent steps:
$$
SOC_t^{th} = (1-\lambda_{th})\,SOC_{t-1}^{th} + Q_t^{th,ch}\Delta t - Q_t^{th,dis}\Delta t, \qquad t>0
$$

with bounds:

$$
SOC_{\min}^{th} \le SOC_t^{th} \le SOC_{\max}^{th}
$$

### 7.3 Heat-demand satisfaction

Heat demand is met by TES discharge plus slack:

$$
Q_t^{th,dis} + Slack_t^{th} \ge D_t^{th}
$$

This means unmet heat demand is allowed, but penalized in the objective.

---

## 8. Electrolyzer Commitment Model

The electrolyzer is represented by a binary commitment formulation.

### 8.1 Capacity and minimum-load constraints

When on, the electrolyzer must stay between its minimum and maximum load:

$$
P_t^{el} \le \overline{P}_{el}\, y_t^{el,on}
$$

$$
P_t^{el} \ge \underline{\alpha}_{el}\,\overline{P}_{el}\, y_t^{el,on}
$$

### 8.2 Start and stop logic

Start and stop indicators are defined from on/off changes:

$$
y_t^{el,start} \ge y_t^{el,on} - y_{t-1}^{el,on}
$$

$$
y_t^{el,stop} \ge y_{t-1}^{el,on} - y_t^{el,on}
$$

For the first step of each rolling window, the previous electrolyzer status is passed from the previous window.

### 8.3 Ramp-rate constraints

The script imposes ramp limits using an engineering-style big-$M$ relaxation at startup and shutdown:

$$
P_t^{el} - P_{t-1}^{el} \le R_{el} + M\,y_t^{el,start}
$$

$$
P_{t-1}^{el} - P_t^{el} \le R_{el} + M\,y_t^{el,stop}
$$

### 8.4 Minimum up-time and minimum down-time

Minimum up-time:

$$
\sum_{k=t}^{t+U-1} y_k^{el,on} \ge U\, y_t^{el,start}
$$

Minimum down-time:

$$
\sum_{k=t}^{t+D-1} \left(1-y_k^{el,on}\right) \ge D\, y_t^{el,stop}
$$

These constraints are explicitly included to avoid unrealistic cycling and to capture engineering start/stop behavior.

---

## 9. Electric Power Balance

Because the short-term model does not allow grid import or export, the nodal electric balance is:

$$
P_t^{curt} + P_t^{b,dis} + P_t^{def}
=
P_t^{b,ch} + P_t^{el} + P_t^{p2h} + P_t^{sur}
$$

Here:

- $P_t^{def}$ is deficit slack
- $P_t^{sur}$ is surplus slack

Under strict full-absorption verification mode, both are forced to zero:

$$
P_t^{def} = 0
$$

$$
P_t^{sur} = 0
$$

This makes the power balance exact under verification mode.

---

## 10. Hydrogen Production and Storage

Hydrogen production is proportional to electrolyzer electricity consumption:

$$
H_t^{prod} = \frac{P_t^{el}\Delta t}{SEC}
$$

where $SEC$ is the specific electricity consumption in MWh/kg.

Hydrogen inventory evolves as:

For the first step of each rolling window:
$$
SOC_0^{H_2} = SOC_{\text{init}}^{H_2} + H_0^{prod} - H_0^{sale}
$$

For subsequent steps:
$$
SOC_t^{H_2} = SOC_{t-1}^{H_2} + H_t^{prod} - H_t^{sale}, \qquad t>0
$$

with bounds:

$$
SOC_{\min}^{H_2} \le SOC_t^{H_2} \le SOC_{\max}^{H_2}
$$

Hydrogen sales are therefore constrained both by production and by storage availability over time.

---

## 11. Objective Function

The short-term model is solved as a cost minimization problem.

It minimizes:

- unused-curtailment penalty
- electrolyzer variable O&M cost
- electrolyzer start cost
- electrolyzer stop cost
- power-balance deficit penalty
- power-balance surplus penalty
- unmet heat-demand penalty

while subtracting hydrogen sale revenue.

The objective is:

$$
\min Z^{ST} =
\sum_t c^{unused} U_t \Delta t
+ \sum_t c^{el,var} P_t^{el}\Delta t
+ \sum_t \left(c^{start} y_t^{el,start} + c^{stop} y_t^{el,stop}\right)
+ \sum_t c^{def} P_t^{def}\Delta t
+ \sum_t c^{sur} P_t^{sur}\Delta t
+ \sum_t c^{th,unmet} Slack_t^{th}\Delta t
- \sum_t \pi_t^{H_2} H_t^{sale}
$$

This objective makes the model favor:

- high curtailment absorption
- technically feasible dispatch
- limited electrolyzer cycling
- heat-demand satisfaction
- hydrogen sale revenue whenever possible

---

## 12. Rolling-Horizon Structure

The script does not solve the entire year as one monolithic MILP. Instead, it uses a rolling-horizon structure.

At the start of each optimization window, the following state variables are passed from the previous window:

- battery SOC
- thermal storage SOC
- hydrogen storage SOC
- electrolyzer on/off state
- previous electrolyzer power

This preserves temporal continuity while keeping the optimization computationally manageable. The default rolling settings in the CLI are:

- window length = `168 h`
- overlap = `24 h`

So the model solves weekly windows with one-day overlap.

---

## 13. Alpha Scenarios and Full-Absorption Verification

The script is explicitly designed to run multiple alpha scenarios in one execution.

For each $\alpha$, the project-accessible curtailed electricity is built as:

$$
A_t^{proj} = \alpha A_t^{reg}
$$

and results are stored in subfolders such as:

- `alpha_0p005`
- `alpha_0p0066`
- `alpha_0p01`

The script also writes:

- scenario-level dispatch files
- scenario-level KPI summaries
- a root-level `alpha_kpi_summary.csv`

When `--verify_full_absorption` is activated, the script becomes a strict feasibility test. It checks whether the chosen capacities can absorb 100% of the project-accessible curtailed electricity without unused energy and without power-balance slack.

---

## 14. Main Interpretation

This short-term MILP is best interpreted as an operational screening model.

It is not asking:

> What is the globally optimal long-term investment portfolio?

Instead, it is asking:

> Given a short-term curtailed-electricity signal, how should batteries, power-to-heat, and electrolysis share the absorption task under realistic technical constraints?

That is why the model includes:

- battery mode exclusivity
- TES charge/discharge logic
- electrolyzer commitment and ramping
- heat-demand penalties
- rolling-horizon continuity
- optional full-absorption verification

These features make it well suited for comparing operational roles and identifying where saturation begins under increasing project-accessible curtailment.

---

## 15. Output Files

For each alpha scenario, the script exports:

- `dispatch_timeseries.csv`
- `dispatch_partial.csv`
- `kpi_summary.txt`

At the root output directory, it also exports:

- `alpha_kpi_summary.csv`

These outputs provide time-series dispatch results, intermediate rolling-horizon outputs, and aggregated KPI summaries across alpha scenarios.
