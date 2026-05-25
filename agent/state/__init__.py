"""Agent state persistence (Plan E Task 6).

Rolling per-block history store backed by a single parquet file with
atomic-rename writes. Sized for T2 OUCalibrator (>=50 spreads) and
T3 hazard features F1/F3/F4 (>=500 lags each).
"""
