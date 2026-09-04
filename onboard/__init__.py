"""Bring-your-own-data — an AI-assisted onboarding layer (not one of the graded phases).

A new bank, gateway, or invoice export rarely uses this project's exact column
names. Instead of hand-writing a loader per source, `onboard.mapper` shows the
LLM the file's headers and a few sample rows and asks it to propose a mapping
onto the canonical `Payment` / `Invoice` fields already used everywhere else in
the codebase (`tools.loaders`). A human confirms the mapping once; after that,
applying it to every row is plain, deterministic Python — the AI is never in
the loop for reading the actual amounts.

This package is purely additive: it does not import from, or get imported by,
`agent/`, `eval/`, `monitor/`, or `qa/`, and it does not touch the graded
130-payment pipeline. Point it at a differently-shaped file, get back the same
`Payment`/`Invoice` objects, and everything downstream works unchanged.
"""
