# Methods

This folder contains the main modeling and analysis methods used in the project.

## Structure

### `curtailment_risk/`
This subfolder contains the scripts used to identify renewable curtailment-risk periods and to construct the absorb-need curves for France and Hauts-de-France.

Typical files include:
- `eco2mix_curtailment_risk_pipeline.py`
- `allocate_curtailment_absorption_curves.py`

### `short_term_dispatch/`
This subfolder contains the short-term dispatch optimization model.

Typical files include:
- `short time MILP.py`

This model is used to simulate how batteries, power-to-heat, and electrolysis absorb renewable surplus under different project scales.

### `long_term_optimization/`
This subfolder contains the long-term optimization model for port-based hydrogen infrastructure planning.

Typical files include:
- `run_long_term_gurobi_port_h2_final.py`

This model is used to optimize annual operation and infrastructure decisions for battery, electrolyzer, and hydrogen storage systems.
