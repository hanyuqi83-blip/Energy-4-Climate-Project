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
