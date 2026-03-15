# Energy-4-Climate-Project

## Project Overview

This repository contains the full project material for a student team submission developed in the **Energy 4 Climate Student Challenge**. The project focuses on how curtailed renewable electricity in **Hauts-de-France** can be absorbed more effectively through a **port-centred green hydrogen flexibility hub** combining battery storage, electrolysis, and hydrogen-related infrastructure.

The repository is structured to document the project from problem definition to engineering interpretation. It includes background context, data organization, methodological scripts, and model results. The purpose of this repository is not only to store code, but also to present the project as a coherent engineering and management exercise.

---

## Background

France is entering a period in which renewable generation continues to expand, while the ability of the power system to absorb that generation remains uneven across time and space. In practice, this creates periods of renewable curtailment. The problem is not simply that wind and solar produce large quantities of electricity. The difficulty is that production often arrives when local demand is low, export corridors are stressed, and flexible demand is insufficient.

This project starts from that challenge. Instead of asking how to build more renewable generation, it asks how already-available renewable electricity can be absorbed more effectively through local flexibility and conversion infrastructure.

Hauts-de-France was selected as the regional case study because the region is especially relevant during stressed renewable periods. The project therefore treats the curtailment issue not as an abstract annual surplus, but as a repeated operational stress concentrated in identifiable time windows.

---

## Project Objective

The objective of this project is to translate a regional curtailment problem into a physically coherent and economically interpretable infrastructure concept.

The proposed solution is a **port-centred green hydrogen flexibility hub** with a layered design logic:

- **Battery Energy Storage System (BESS)** absorbs the fastest and most volatile part of the surplus.
- **Electrolysis** converts the more persistent part of the surplus into hydrogen.
- **Hydrogen storage and logistics** provide temporal decoupling and delivery flexibility where economically justified.
- **The port** acts as a realistic aggregation point, connecting renewable absorption, industrial demand, storage space, and logistics potential.

This layered structure is central to the project. Short solar-driven spikes, longer wind-driven surpluses, and repeated export stress do not require exactly the same response. The project therefore combines fast flexibility and industrial conversion rather than relying on a single technology.

---

## Project Logic

The work is organized in three connected steps.

### 1. Curtailment-risk identification and regional allocation

The project first reconstructs a time-resolved absorb-need curve for Hauts-de-France from national and regional electricity data. This stage identifies the high-risk periods during which renewable curtailment is most likely to occur and translates a national curtailment challenge into a regional engineering problem.

### 2. Short-term screening

The second step compares the short-term operational role of different flexibility options. This stage is used to clarify which technologies are best suited to fast response, repeated export stress, and more persistent renewable surplus.

### 3. Long-term deployment logic

The final step moves from operational screening to fixed-asset project design. At this stage, the project is no longer treated only as a dispatch problem. It becomes a long-term infrastructure concept involving battery storage, a port-based electrolyzer, and potentially hydrogen storage, together with engineering governance, implementation logic, and commercial interpretation.

---

## Engineering and Management Perspective

A key contribution of this project is that it does not stop at energy-system modelling. It also treats the proposed solution as a real engineering programme.

The repository therefore reflects a broader project-development perspective, including:

- **engineering lifecycle thinking**, from feasibility to commissioning;
- **organizational structure**, including OBS and responsibility allocation;
- **project governance**, with clear links between technical work, schedule control, and decision-making;
- **risk management**, especially regarding grid access, permitting, supply chain constraints, safety, and commercialization;
- **commercial interpretation**, focusing on the boundary between technical feasibility and economic viability.

This means the project should be read not only as a modelling exercise, but also as a structured project proposal.

---

## Repository Structure

```text
Energy-4-Climate-Project/
│
├── data/
│   ├── raw/
│   ├── short_term_inputs/
│   └── long_term_inputs/
│
├── methods/
│   ├── curtailment_risk/
│   ├── short_term_dispatch/
│   └── long_term_optimization/
│
├── results/
│   ├── curtailment_risk/
│   ├── absorption_allocation/
│   ├── short_term_dispatch/
│   └── long_term_optimization/
│
└── README.md


## Mathematical Formulation of the Optimization Models

This project includes two complementary optimization models:

1. a **short-term dispatch MILP**, used to study how different flexibility assets absorb curtailed electricity over 30-minute intervals;  
2. a **long-term annual MILP**, used to evaluate the economically optimal fixed-asset design of a port-based battery–hydrogen system.

The short-term model is designed as an **operational screening model**, while the long-term model is designed as an **investment-aware techno-economic model**.

---

## 1. Short-Term Dispatch Model

### 1.1 Purpose

The short-term model evaluates how three co-existing flexibility assets can absorb project-accessible curtailed electricity:

- **Battery Energy Storage System (BESS)**
- **Power-to-Heat (P2H)** with electric heater and thermal energy storage
- **Power-to-Hydrogen (P2H$_2$)** with electrolyzer, hydrogen storage, and hydrogen sales

The model is solved at **30-minute resolution** and uses a **rolling-horizon MILP** formulation. The curtailed-electricity input seen by the project is the alpha-scaled curve `absorb_need_mw`, while the original regional curve `absorb_need_mw_region` is preserved for reporting. The model explicitly assumes **no electricity import and no electricity export**. Hydrogen sales are allowed, and heat demand is treated as an exogenous time series. Any unmet heat demand is penalized through a slack variable.  

### 1.2 Time Set and Main Inputs

Let \( t \in \mathcal{T} \) denote each 30-minute time step, and let \( \Delta t = 0.5 \) h.

Main exogenous inputs:

- \( A_t \): project-accessible curtailed electricity, represented by `absorb_need_mw`
- \( A_t^{reg} \): regional absorb-need curve, represented by `absorb_need_mw_region`
- \( D_t^{th} \): heat demand
- \( \pi_t^{H_2} \): hydrogen sale price

### 1.3 Decision Variables

The main decision variables are:

#### Curtailment use
- \( P_t^{curt} \): curtailed electricity actually used by the project
- \( U_t \): unused curtailed electricity

#### Battery
- \( P_t^{b,ch} \): battery charging power
- \( P_t^{b,dis} \): battery discharging power
- \( SOC_t^b \): battery state of charge
- \( y_t^{b,ch}, y_t^{b,dis} \in \{0,1\} \): charge/discharge mode binaries

#### Power-to-Heat
- \( P_t^{p2h} \): electric input to heater
- \( Q_t^{th,dis} \): thermal discharge from TES
- \( SOC_t^{th} \): thermal storage state of charge
- \( y_t^{th,ch}, y_t^{th,dis} \in \{0,1\} \): thermal charge/discharge binaries
- \( Slack_t^{th} \): unmet heat demand slack

#### Electrolyzer and hydrogen
- \( P_t^{el} \): electrolyzer electric power
- \( y_t^{el,on}, y_t^{el,start}, y_t^{el,stop} \in \{0,1\} \): on/off, start, and stop binaries
- \( H_t^{prod} \): hydrogen production
- \( H_t^{sale} \): hydrogen sale
- \( SOC_t^{H_2} \): hydrogen inventory

#### Power-balance feasibility slacks
- \( P_t^{def} \): power deficit slack
- \( P_t^{sur} \): power surplus slack

### 1.4 Curtailment Absorption Constraints

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
P_t^{curt} = A_t, \qquad U_t = 0
$$

This makes the model a feasibility test for complete curtailment absorption under all technical constraints.

### 1.5 Battery Constraints

The battery cannot charge and discharge simultaneously:

$$
y_t^{b,ch} + y_t^{b,dis} \le 1
$$

Charging and discharging power are bounded by the installed battery power:

$$
P_t^{b,ch} \le \bar P^b y_t^{b,ch}, \qquad
P_t^{b,dis} \le \bar P^b y_t^{b,dis}
$$

Battery SOC evolves as:

$$
SOC_t^b =
\begin{cases}
SOC_0^b + \eta_{ch} P_t^{b,ch}\Delta t - \dfrac{1}{\eta_{dis}} P_t^{b,dis}\Delta t, & t=0 \\
SOC_{t-1}^b + \eta_{ch} P_t^{b,ch}\Delta t - \dfrac{1}{\eta_{dis}} P_t^{b,dis}\Delta t, & t>0
\end{cases}
$$

with SOC bounds:

$$
SOC_{min}^b \le SOC_t^b \le SOC_{max}^b
$$

### 1.6 Power-to-Heat and Thermal Storage Constraints

The thermal system also uses mutually exclusive charge/discharge modes:

$$
y_t^{th,ch} + y_t^{th,dis} \le 1
$$

Heater and TES discharge limits are:

$$
P_t^{p2h} \le \bar P^{p2h} y_t^{th,ch}
$$

$$
Q_t^{th,dis} \le \bar Q^{th,dis} y_t^{th,dis}
$$

Thermal charging is linked to electric input through the heater efficiency:

$$
Q_t^{th,ch} = \eta_{p2h} P_t^{p2h}
$$

Thermal storage evolves as:

$$
SOC_t^{th} =
\begin{cases}
(1-\lambda^{th})SOC_0^{th} + Q_t^{th,ch}\Delta t - Q_t^{th,dis}\Delta t, & t=0 \\
(1-\lambda^{th})SOC_{t-1}^{th} + Q_t^{th,ch}\Delta t - Q_t^{th,dis}\Delta t, & t>0
\end{cases}
$$

with bounds:

$$
SOC_{min}^{th} \le SOC_t^{th} \le SOC_{max}^{th}
$$

Heat demand is met through TES discharge plus slack:

$$
Q_t^{th,dis} + Slack_t^{th} \ge D_t^{th}
$$

This formulation allows the model to remain feasible when the thermal subsystem cannot serve all heat demand, while penalizing such shortages in the objective.

### 1.7 Electrolyzer Operational Constraints

The electrolyzer is modeled with binary commitment logic. When on, it must remain between minimum and maximum load:

$$
P_t^{el} \le \bar P^{el} y_t^{el,on}
$$

$$
P_t^{el} \ge \underline{\alpha}^{el}\bar P^{el} y_t^{el,on}
$$

Start and stop indicators are defined by the change in on/off status:

$$
y_t^{el,start} \ge y_t^{el,on} - y_{t-1}^{el,on}
$$

$$
y_t^{el,stop} \ge y_{t-1}^{el,on} - y_t^{el,on}
$$

The script implements ramp-rate limits with a big-\(M\) relaxation at startup and shutdown:

$$
P_t^{el} - P_{t-1}^{el} \le R^{el} + M y_t^{el,start}
$$

$$
P_{t-1}^{el} - P_t^{el} \le R^{el} + M y_t^{el,stop}
$$

It also enforces minimum up-time and minimum down-time:

$$
\sum_{k=t}^{t+U-1} y_k^{el,on} \ge U \, y_t^{el,start}
$$

$$
\sum_{k=t}^{t+D-1} (1-y_k^{el,on}) \ge D \, y_t^{el,stop}
$$

These constraints are important because they prevent unrealistic electrolyzer cycling and reflect engineering start/stop behavior.

### 1.8 Power Balance

Because the short-term model does not allow grid import or export, the nodal electric balance is written as:

$$
P_t^{curt} + P_t^{b,dis} + P_t^{def}
=
P_t^{b,ch} + P_t^{el} + P_t^{p2h} + P_t^{sur}
$$

Here \(P_t^{def}\) and \(P_t^{sur}\) are penalty slacks. In strict full-absorption verification mode, both are forced to zero:

$$
P_t^{def} = 0, \qquad P_t^{sur} = 0
$$

### 1.9 Hydrogen Production and Storage

Hydrogen production is proportional to electrolyzer electricity consumption:

$$
H_t^{prod} = \frac{P_t^{el}\Delta t}{SEC}
$$

where \( SEC \) is the specific electricity consumption in MWh/kg.

Hydrogen inventory evolves as:

$$
SOC_t^{H_2} =
\begin{cases}
SOC_0^{H_2} + H_t^{prod} - H_t^{sale}, & t=0 \\
SOC_{t-1}^{H_2} + H_t^{prod} - H_t^{sale}, & t>0
\end{cases}
$$

with storage bounds:

$$
SOC_{min}^{H_2} \le SOC_t^{H_2} \le SOC_{max}^{H_2}
$$

### 1.10 Objective Function

The short-term model is solved as a **cost minimization** problem. It minimizes:

- penalty for unused curtailed electricity,
- electrolyzer variable O\&M cost,
- electrolyzer start/stop cost,
- power-balance slack penalties,
- unmet heat demand penalty,

while subtracting hydrogen sale revenue.

The objective can be written as:

$$
\min Z^{ST}
=
\sum_t c^{unused} U_t \Delta t
+ \sum_t c^{el,var} P_t^{el}\Delta t
+ \sum_t \left(c^{start} y_t^{el,start} + c^{stop} y_t^{el,stop}\right)
+ \sum_t c^{def} P_t^{def}\Delta t
+ \sum_t c^{sur} P_t^{sur}\Delta t
+ \sum_t c^{th,unmet} Slack_t^{th}\Delta t
- \sum_t \pi_t^{H_2} H_t^{sale}
$$

### 1.11 Boundary Conditions and Rolling Horizon

The short-term model is not solved as one single full-year block. Instead, it is implemented through a **rolling-horizon** procedure. At the start of each optimization window, the following states are passed from the previous window:

- battery SOC,
- thermal storage SOC,
- hydrogen storage SOC,
- electrolyzer on/off state,
- previous electrolyzer power.

This ensures temporal continuity while keeping the MILP computationally manageable.

---

## 2. Long-Term Annual Optimization Model

### 2.1 Purpose

The long-term model is an annual MILP used to identify the economically optimal fixed-asset design of a **port-based hydrogen project**. Compared with the short-term model, it simplifies the asset set and keeps only:

- **BESS**
- **Electrolyzer**
- **Hydrogen tank**

The model is solved at **30-minute resolution** over the year, but its objective is annual profit maximization with explicit annualized CAPEX and fixed OPEX. A key design principle in the code is that curtailed electricity must first be absorbed by the battery, and hydrogen can only be produced from **battery discharge**, not directly from grid power. A small grid power \(P^{keep}\) is allowed only to keep the electrolyzer hot. It does **not** produce hydrogen.

### 2.2 Accessible Curtailment

At each time step \(t\), project-accessible curtailed electricity is defined as:

$$
A_t = \min(\rho S_t^{reg}, F_{port})
$$

where:

- \( S_t^{reg} \) is the regional absorb-need curve,
- \( \rho \) is the project share,
- \( F_{port} \) is the project-side port connection limit.

### 2.3 Capacity Variables

The long-term model optimizes both operation and infrastructure sizing. Main capacity variables include:

- \( P_{el}^{max} \): electrolyzer capacity
- \( P^{keep} \): electrolyzer keep-alive power
- \( P_b^{max} \): battery power capacity
- \( E_b^{max} \): battery energy capacity
- \( V^{tank} \): hydrogen tank volume

with binary build variables:

- \( z^{el} \in \{0,1\} \)
- \( z^{tank} \in \{0,1\} \)

The installation logic is:

$$
P_{el}^{max} \le \bar P_{el} z^{el}
$$

$$
V^{tank} \le \bar V^{tank} z^{tank}
$$

The keep-alive power is linked to electrolyzer size by:

$$
P^{keep} = \alpha^{keep} P_{el}^{max}
$$

### 2.4 Battery and Curtailment Absorption

The model explicitly forces curtailed electricity to enter the battery first:

$$
u_t = p_t^{ch}
$$

with

$$
u_t \le \rho S_t^{reg}, \qquad
u_t \le F_{port}
$$

and unused accessible curtailed electricity:

$$
s_t = A_t - u_t
$$

The battery cannot charge and discharge simultaneously:

$$
y_t^{ch} + y_t^{dis} \le 1
$$

and is bounded by the optimized battery power:

$$
p_t^{ch} \le P_b^{max}, \qquad
p_t^{dis} \le P_b^{max}
$$

Battery SOC evolves as:

$$
SOC_t^b =
\begin{cases}
\eta_{ch} p_t^{ch}\Delta t - \dfrac{1}{\eta_{dis}}p_t^{dis}\Delta t, & t=0 \\
SOC_{t-1}^b + \eta_{ch} p_t^{ch}\Delta t - \dfrac{1}{\eta_{dis}}p_t^{dis}\Delta t, & t>0
\end{cases}
$$

with:

$$
SOC_t^b \le E_b^{max}
$$

The model enforces terminal emptying:

$$
SOC_T^b = 0
$$

### 2.5 Hydrogen Production and Storage

Hydrogen can only be produced from battery discharge:

$$
h_t^{prod} = \kappa p_t^{dis}
$$

where

$$
\kappa = \frac{1000\Delta t}{SEC}
$$

The electrolyzer production power is therefore effectively constrained by:

$$
p_t^{dis} \le P_{el}^{max}
$$

Hydrogen sales are bounded per step:

$$
h_t^{sell} \le \phi^{sell}\kappa P_{el}^{max}
$$

Hydrogen inventory evolves as:

$$
H_t =
\begin{cases}
h_t^{prod} - h_t^{sell}, & t=0 \\
(1-\lambda^{H_2})H_{t-1} + h_t^{prod} - h_t^{sell}, & t>0
\end{cases}
$$

Tank capacity is given by:

$$
H_t \le \gamma V^{tank}
$$

and terminal inventory is forced to zero:

$$
H_T = 0
$$

### 2.6 Anti-Arbitrage and Commercial Boundary Constraints

A distinctive feature of the long-term code is that it includes several constraints to prevent unrealistic “store forever and sell only at the absolute peak hydrogen price” behavior.

#### (a) Hydrogen selling ramp
$$
h_t^{sell} - h_{t-1}^{sell} \le R^{sell}
$$

$$
h_{t-1}^{sell} - h_t^{sell} \le R^{sell}
$$

#### (b) Finite equivalent tank-buffer days
The tank is bounded by a multiple of equivalent sell-cap days:

$$
\gamma V^{tank} \le B^{days}\cdot 48 \cdot \phi^{sell}\kappa P_{el}^{max}
$$

#### (c) Maximum average residence time
$$
\sum_t H_t \Delta t \le 24\tau^{res}\sum_t h_t^{sell}
$$

#### (d) Daily hydrogen delivery cap
For each day \(d\):

$$
\sum_{t\in d} h_t^{sell} \le H^{day,max}\cdot \frac{1000}{SEC}P_{el}^{max}
$$

These constraints make the long-term solution more commercially realistic and prevent the optimizer from relying on unrealistic perfect intertemporal arbitrage.

### 2.7 Minimum Absorption Requirement

To ensure that the optimized project still performs a meaningful curtailment-absorption role, the annual model imposes a minimum absorption constraint:

$$
\sum_t u_t \Delta t \ge \theta^{abs}\sum_t A_t \Delta t
$$

This prevents the optimizer from selecting a purely symbolic asset configuration with negligible curtailment absorption.

### 2.8 Tank CAPEX Representation

The hydrogen tank CAPEX is not hard-coded as a single constant. The script first fits a linear cost function from material-consumption and material-price input tables:

$$
CAPEX^{tank,USD}(V) \approx aV + b
$$

This fitted linear relationship is then converted into euros and annualized inside the optimization.

### 2.9 Annual Profit Objective

The long-term model is solved as a **profit maximization** problem. Its annual objective includes:

- hydrogen sale revenue,
- electricity cost of keep-alive operation,
- curtailed-electricity collection cost,
- annualized electrolyzer CAPEX,
- annualized BESS CAPEX,
- annualized tank CAPEX,
- fixed OPEX as 5% of overnight CAPEX,
- hydrogen inventory holding cost.

The objective is:

$$
\max Z^{LT}
=
\sum_t \pi_t^{H_2} h_t^{sell}
-\sum_t \pi_t^e P^{keep}\Delta t
-\sum_t c^{collect} u_t \Delta t
-CAPEX_{ann}^{el}
-CAPEX_{ann}^{bess}
-CAPEX_{ann}^{tank}
-OPEX^{fixed}
-OPEX^{hold}
$$

where:

$$
OPEX^{fixed} = \alpha^{opex}\left(CAPEX^{el} + CAPEX^{bess} + CAPEX^{tank}\right)
$$

and annualized CAPEX is obtained through the standard annuity factor:

$$
AF(r,n)=\frac{r(1+r)^n}{(1+r)^n-1}
$$

### 2.10 Interpretation of the Long-Term Boundary Conditions

The long-term model is intentionally conservative. It does **not** allow direct grid-powered hydrogen production except for keep-alive operation, and it does **not** let the project absorb curtailment without first passing through the battery. In other words:

- curtailed electricity must first be buffered,
- hydrogen must be produced from battery discharge,
- tank storage cannot be infinitely large or infinitely patient,
- daily hydrogen offtake is limited,
- the project must still absorb at least a minimum annual share of accessible curtailed electricity.

These boundary conditions are central to the model’s interpretation. They ensure that the solution is not just mathematically optimal, but also closer to a physically and commercially interpretable infrastructure concept.

---

## 3. Relationship Between the Two Models

The two models play different roles in the project:

### Short-term model
The short-term dispatch MILP is used to answer:

- Which asset reacts best to short solar spikes?
- Which asset can manage repeated export stress?
- How do BESS, thermal flexibility, and electrolysis share the absorption task?
- At what project share does the system start to saturate?

### Long-term model
The long-term annual MILP is used to answer:

- What fixed-asset configuration is economically selected when CAPEX and OPEX are included?
- How much battery, electrolyzer, and tank capacity is justified under annual operation?
- Is the project pushed toward large-scale absorption or toward a minimum-loss design?

In that sense, the short-term model is a **technology screening tool**, while the long-term model is a **project design and investment screening tool**.
