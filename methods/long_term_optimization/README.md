# Long-Term Optimization Methods

This folder contains the long-term optimization model for port-based hydrogen infrastructure planning.

Files in this folder include:
- `run_long_term_gurobi_port_h2_final.py`

This model is used to optimize the **annual operation and sizing** of a port-based system combining:

- Battery Energy Storage System (BESS)
- Electrolyzer
- Hydrogen tank

The model is formulated as a **long-term annual MILP** at **30-minute resolution** and solved with **Gurobi**. It is designed not as a pure arbitrage model, but as a **curtailment-absorption project model** with explicit techno-economic and commercial boundary conditions.

---

## 1. Model Purpose

The purpose of the model is to determine the economically optimal fixed-asset design of a hydrogen project that absorbs curtailed renewable electricity in a physically interpretable way.

The model does **not** allow direct grid-powered hydrogen production. Instead, it enforces the following project logic:

1. curtailed electricity is first absorbed by the battery;
2. hydrogen is produced only from battery discharge;
3. grid electricity is used only to keep the electrolyzer hot;
4. hydrogen storage and sales are bounded by realistic operational and commercial constraints.

This design makes the model more conservative, but also more meaningful from an engineering-project perspective.

---

## 2. Time Resolution and Scenario Setup

The model is solved over a full year at:

$$
\Delta t = 0.5 \text{ h}
$$

The default script is intentionally restricted to **one scenario only** in order to keep runtime within about 20 minutes:

- port connection limit: `F_PORT_LIST_MW = [100.0]`
- project share: `RHO_LIST = [1.0]`
- curtailed-electricity collection cost: `C_COLLECT_LIST_EUR_PER_MWH = [10.0]`

The default Gurobi time limit is:

$$
T_{\max}^{solver} = 1200 \text{ s}
$$

---

## 3. Main Inputs

The model merges two main time-series inputs:

- regional absorb-need curve  
  `region_absorb_need_curve_30min`
- electricity and hydrogen price series  
  `prices_2024_30min_elec_plus_h2_fixed2024_margin1.0`

For each time step \( t \), the main exogenous parameters are:

- \( S_t^{reg} \): regional absorb-need curve
- \( \pi_t^e \): electricity price
- \( \pi_t^{H_2} \): hydrogen sale price

The project-accessible curtailed electricity is defined as:

$$
A_t = \min(\rho S_t^{reg}, F_{port})
$$

where:

- \( \rho \) is the project share,
- \( F_{port} \) is the project-side port connection limit.

---

## 4. Decision Variables

### 4.1 Capacity variables

The model optimizes the following infrastructure capacities:

- \( P_{el}^{max} \): electrolyzer capacity (MW)
- \( P^{keep} \): electrolyzer keep-alive power (MW)
- \( P_b^{max} \): battery power capacity (MW)
- \( E_b^{max} \): battery energy capacity (MWh)
- \( V^{tank} \): hydrogen tank size

It also includes binary build variables:

- \( z^{el} \in \{0,1\} \): electrolyzer build decision
- \( z^{tank} \in \{0,1\} \): tank build decision

### 4.2 Operational variables

For each time step \( t \), the main operational variables are:

- \( p_t^{ch} \): battery charging power
- \( p_t^{dis} \): battery discharging power
- \( SOC_t^b \): battery state of charge
- \( u_t \): absorbed curtailed electricity
- \( s_t \): unabsorbed accessible curtailed electricity
- \( h_t^{sell} \): hydrogen sold
- \( H_t \): hydrogen inventory
- \( y_t^{ch}, y_t^{dis} \in \{0,1\} \): battery charge/discharge mode binaries

---

## 5. Capacity Logic

The electrolyzer is built only if \( z^{el}=1 \):

$$
P_{el}^{max} \le \overline{P}_{el} z^{el}
$$

The tank is built only if \( z^{tank}=1 \):

$$
V^{tank} \le \overline{V}^{tank} z^{tank}
$$

The script also defines keep-alive power as a fixed fraction of installed electrolyzer capacity:

$$
P^{keep} = \alpha^{keep} P_{el}^{max}
$$

In the default code:

$$
\alpha^{keep} = 0.05
$$

This means the electrolyzer always requires a small amount of grid electricity to remain hot, but that keep-alive power **does not produce hydrogen**. :contentReference[oaicite:1]{index=1}

---

## 6. Curtailment Absorption and Battery Logic

A key modelling choice is:

$$
u_t = p_t^{ch}
$$

This means curtailed electricity must first enter the battery. The absorbed curtailed electricity is bounded by both the regional accessible surplus and the port connection limit:

$$
u_t \le \rho S_t^{reg}
$$

$$
u_t \le F_{port}
$$

Unabsorbed accessible curtailed electricity is defined as:

$$
s_t = A_t - u_t
$$

### 6.1 Battery mode constraints

The battery cannot charge and discharge simultaneously:

$$
y_t^{ch} + y_t^{dis} \le 1
$$

Charging and discharging powers are limited by optimized battery power:

$$
p_t^{ch} \le P_b^{max}
$$

$$
p_t^{dis} \le P_b^{max}
$$

The code also uses binary mode logic to enforce exclusivity.

### 6.2 Battery state of charge

Battery SOC evolves as:

$$
SOC_t^b =
\begin{cases}
\eta_{ch} p_t^{ch}\Delta t - \dfrac{1}{\eta_{dis}} p_t^{dis}\Delta t, & t = 0 \\
SOC_{t-1}^b + \eta_{ch} p_t^{ch}\Delta t - \dfrac{1}{\eta_{dis}} p_t^{dis}\Delta t, & t > 0
\end{cases}
$$

with the capacity constraint:

$$
SOC_t^b \le E_b^{max}
$$

and terminal emptying condition:

$$
SOC_T^b = 0
$$

In the default configuration, the charging and discharging efficiencies are:

$$
\eta_{ch} = \eta_{dis} = \sqrt{0.85}
$$

---

## 7. Hydrogen Production Logic

Another key modelling choice is that hydrogen can only be produced from battery discharge:

$$
h_t^{prod} = \kappa p_t^{dis}
$$

where:

$$
\kappa = \frac{1000 \Delta t}{SEC}
$$

and \( SEC \) is the specific electricity consumption in kWh/kg.

In the script:

$$
SEC = 55 \text{ kWh/kg}
$$

So hydrogen production is not driven by grid electricity, but strictly by discharged battery electricity. This is one of the most important anti-arbitrage features of the model. :contentReference[oaicite:2]{index=2}

The battery discharge used for hydrogen production is constrained by electrolyzer capacity:

$$
p_t^{dis} \le P_{el}^{max}
$$

---

## 8. Hydrogen Sales and Storage

Hydrogen sales per time step are capped by a factor times electrolyzer nameplate production capability:

$$
h_t^{sell} \le \phi^{sell} \kappa P_{el}^{max}
$$

where \( \phi^{sell} \) is the sell-flow factor.

Hydrogen inventory evolves as:

$$
H_t =
\begin{cases}
h_t^{prod} - h_t^{sell}, & t = 0 \\
(1-\lambda^{H_2})H_{t-1} + h_t^{prod} - h_t^{sell}, & t > 0
\end{cases}
$$

with storage capacity:

$$
H_t \le \gamma V^{tank}
$$

and terminal condition:

$$
H_T = 0
$$

Here:

- \( \gamma \) is the conversion from tank volume to hydrogen storage capacity,
- \( \lambda^{H_2} \) is the per-step hydrogen loss factor.

In the default configuration:

$$
\lambda^{H_2} = 0
$$

so no standing hydrogen loss is assumed.

---

## 9. Anti-Arbitrage and Commercial Boundary Constraints

One of the most distinctive aspects of this model is that it explicitly avoids pathological “store forever and sell only at the single highest hydrogen price” behavior.

### 9.1 Hydrogen sale ramp constraint

Hydrogen sales cannot jump arbitrarily between periods:

$$
h_t^{sell} - h_{t-1}^{sell} \le R^{sell}
$$

$$
h_{t-1}^{sell} - h_t^{sell} \le R^{sell}
$$

This limits the speed of commercial delivery adjustments.

### 9.2 Finite tank buffer days

Tank size is bounded by a finite equivalent number of sell-cap days:

$$
\gamma V^{tank} \le B^{days} \cdot 48 \cdot \phi^{sell}\kappa P_{el}^{max}
$$

This prevents the optimizer from building an unrealistically large tank relative to the project’s delivery scale.

### 9.3 Maximum average residence time

The average hydrogen residence time is bounded:

$$
\sum_t H_t \Delta t \le 24\tau^{res}\sum_t h_t^{sell}
$$

where \( \tau^{res} \) is the maximum average residence time in days.

This prevents a solution in which hydrogen is kept in storage for excessively long periods just to exploit isolated price peaks.

### 9.4 Daily hydrogen delivery cap

For each day \( d \), the total daily hydrogen delivery is limited by an equivalent number of full-load production hours:

$$
\sum_{t \in d} h_t^{sell} \le H^{day,max}\cdot \frac{1000}{SEC} P_{el}^{max}
$$

This keeps the annual project solution closer to realistic logistics and offtake conditions.

---

## 10. Minimum Annual Absorption Requirement

To ensure that the project still performs a meaningful curtailment-absorption role, the model imposes a minimum annual absorption constraint:

$$
\sum_t u_t \Delta t \ge \theta^{abs} \sum_t A_t \Delta t
$$

This prevents the optimizer from selecting a purely symbolic system with negligible renewable absorption.

In the default code:

$$
\theta^{abs} = 0.05
$$

So the project must absorb at least 5% of the accessible annual curtailed electricity. :contentReference[oaicite:3]{index=3}

---

## 11. Tank CAPEX Representation

The hydrogen tank CAPEX is not entered as a fixed constant. Instead, the script first reads:

- `Material_consumption_700bar`
- `Material_price_700bar`
- `Cost_frac`

and fits a linear tank cost function:

$$
CAPEX^{tank,USD}(V) \approx aV + b
$$

This fitted function is then converted from USD to EUR and annualized within the optimization.

This is an important modelling choice because it allows the tank CAPEX to scale with tank size rather than being approximated by a single lumped parameter.

---

## 12. Annual Objective Function

The model is solved as a **profit maximization** problem.

### 12.1 Revenue
Hydrogen sale revenue:

$$
\sum_t \pi_t^{H_2} h_t^{sell}
$$

### 12.2 Costs

The objective subtracts:

- keep-alive electricity cost,
- curtailed-electricity collection cost,
- annualized electrolyzer CAPEX,
- annualized BESS CAPEX,
- annualized tank CAPEX,
- fixed OPEX,
- hydrogen inventory holding cost.

The full objective is:

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

where fixed OPEX is defined as a fraction of overnight CAPEX:

$$
OPEX^{fixed} = \alpha^{opex}
\left(
CAPEX^{el} + CAPEX^{bess} + CAPEX^{tank}
\right)
$$

In the default code:

$$
\alpha^{opex} = 0.05
$$

### 12.3 Annualization

Annualized CAPEX is computed with the standard annuity factor:

$$
AF(r,n) = \frac{r(1+r)^n}{(1+r)^n - 1}
$$

This annuity factor is applied separately to:

- electrolyzer lifetime,
- BESS lifetime,
- tank lifetime.

---

## 13. Boundary Conditions and Interpretation

The long-term model is intentionally conservative. It imposes several boundaries that are central to its interpretation:

- curtailed electricity must first pass through the battery;
- hydrogen can only be produced from battery discharge;
- grid electricity is allowed only as electrolyzer keep-alive power;
- tank size is bounded relative to delivery scale;
- hydrogen cannot remain in inventory indefinitely;
- daily hydrogen delivery is capped;
- the project must absorb at least a minimum annual share of accessible curtailed electricity;
- both battery SOC and hydrogen inventory are forced to zero at the end of the year.

These conditions make the solution more restrictive than a pure arbitrage optimizer, but much closer to a physically and commercially interpretable project design.

---

## 14. Main Modelling Interpretation

This long-term MILP is not asking:

> “What is the theoretically most profitable electricity-to-hydrogen arbitrage strategy?”

Instead, it asks:

> “Given a port-based curtailment-absorption project, what battery, electrolyzer, and tank configuration is economically justified under explicit physical and commercial constraints?”

That is why the model is best understood as a **project design and investment screening model**, not just an operational dispatch optimizer.

---

## 15. Output Files

The script exports:

- scenario dispatch results,
- scenario summaries,
- fitted tank CAPEX debug table,
- model notes.

Typical outputs include:

- `dispatch_Fport100_rho1.0_collect10.0.csv`
- `summary_all_scenarios.csv`
- `tank_fit_debug_Fport100_rho1.0_collect10.0.csv`
- `model_notes.txt`

These outputs can be found in the generated output directory.
