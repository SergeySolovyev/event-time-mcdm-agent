"""Live signal wrappers (Plan E Task 5).

Each module here wraps the SAME research-side builder used by the
replay engine (``decision.features.{f1_lead,f3_fragmentation,f4_related}``)
but takes a single live ``BlockState`` plus a ``HistoryStore`` snapshot
rather than a per-block parquet.

This is the zero-drift guarantee for signals: if a research-side feature
measured X bps in the empirical study, the live agent reads X bps on
the identical inputs -- because it is the literal same function call.
"""
