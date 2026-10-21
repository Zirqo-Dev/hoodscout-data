# HS-77 power simulation

This is a report-only analysis. Nothing here decides a hypothesis. Results are summarised in docs/EVIDENCE_LAYER.md, Layer 3d, step 11.

Data: `data/history.jsonl` as of commit d9f8047 (2026-09-04 to 2026-10-02). That is pre-watermark data, already used in the in-sample backtest. `build_pool.py` asserts that every row's 6h exit window closes before the 2026-10-18T13:30Z watermark.

Reproduce:

    git show d9f8047:data/history.jsonl > /tmp/history_sep.jsonl
    python analysis/hs77/build_pool.py /tmp/history_sep.jsonl /tmp/pool.json
    python analysis/hs77/sim.py /tmp/pool.json 2000 h1 analysis/hs77/out_h1.json
    python analysis/hs77/sim.py /tmp/pool.json 2000 h2 analysis/hs77/out_h2.json

The run is seeded (20261021): the same numpy version gives identical output. Run h1 and h2 separately, as above. A combined "both" run draws H-0002 from a different random stream.
