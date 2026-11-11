# H-0003 pool-switch check (post-read, 2026-11-10)

An external review found that H-0003's clause (c), +3294.38pp at the 2026-11-09 read, rests on one row: its 6h exit was printed from a different pool than its entry. `scripts/label.py` prices an exit from whichever pool the collector logged. The labeler is frozen, so this is disclosed, not changed. The correction is in docs/EVIDENCE_LAYER.md, Results, "Post-read correction, 2026-11-10 (H-0003)".

H-0003 was already read, so its returns may be printed. This does not replace the read and moves no status.

## How to run

Only from the root of a git worktree at the read pin:

```
git worktree add --detach <dir> a782e6e59f867d62d9a5b7ab20d89c89574927ab
cd <dir> && python <repo>/analysis/h0003-poolswitch/poolswitch.py
```

The script refuses (exit 2, nothing printed) unless all of these hold:
- the worktree's HEAD is the pin;
- `scripts/evaluate.py` and `scripts/label.py` hash to the read's 76a3c450 and 3ba1b352;
- the newest `data/log` ts is before H-0009's log_watermark, 2026-11-10T12:30:29.042146Z, so it cannot see an H-0009 row.

It exits 1 if the all-rows variant does not reproduce the read's -13.12 / -10.59 / +3294.38.

## What it does

- **Rows.** The read's rows: `label.load_log()`, then `evaluate.prepare_labels(..., "scored")`, then 6h rows with ts after H-0003's watermark, then `RULES["H-0003"]["arms"]`. Both scripts are imported from the worktree, unchanged.
- **Exit row.** `label.outcome()` stores in `t_actual` the ts of the print it used. That is the row nearest entry+6h within ±0.75h or, for a gone_no_liq row with no print in that window, the last row seen before it. The exit row is the log row at `(t_actual, ca)`.
- **Pool switch.** The exit row's `pool` differs from the entry row's `pool`. gone_unseen rows have no exit row. A null pool on either side is counted as unknown, not as a switch (there were none).
- **Variants.** Variants drop population rows only. Each remaining row keeps the baseline peers the read gave it (baseline peers unchanged), and peers whose own exit switched pool are not filtered. Clause values come from `evaluate.med_net`, `gap_vs_baseline` and `mean_net`. The gross median is `evaluate.med_v(rows, v_gross)`. `[x]` marks a registered bar met. No status is formed.

## Output (run 2026-11-11, worktree at a782e6e5)

```
pin a782e6e59f86  evaluator 76a3c450d426  labeler 3ba1b3524df9  pile scored
log 296480 rows, newest ts 2026-11-09T07:00:31.020954+00:00 < H-0009 watermark 2026-11-10T12:30:29.042146+00:00
H-0003 watermark 2026-10-18T13:30:25.020204+00:00, horizon 6h: population 462 rows

exit pool vs entry pool, by status (exit row = log row at t_actual):
  gone_no_liq  switch 7  same 9  unknown 0  no_exit 0
  gone_unseen  switch 0  same 0  unknown 0  no_exit 149
  ok           switch 0  same 273  unknown 0  no_exit 0
  thin         switch 16  same 8  unknown 0  no_exit 0
pool-switch rows: 23 all statuses, 16 priced exits (ok/thin)

rows above +900% net500 (+900%):
  2026-10-27T20:00Z 0x8fc98b871678f9749d17d968a476386f2da6f831  net500 +1005405.07pp  status thin  switch
    entry 2026-10-27T20:00Z pool 0x33551a92a00ad14f12d2fbaba34c69516850ecfdc5bd4a16a0b3ebc8be513992 liq 16729.96 txns 3922 price 3.781320066e-05
    exit  2026-10-28T02:30Z pool 0x15896c063b77c42f37af81ef153993e2869cef677014302b7de991b42aeeeb38 liq 0.24 txns 1 price 0.537771670384698

clauses in pp; baseline peers unchanged; [x] = registered bar met
  variant                                         rows eff_n  tok        (a)        (b)         (c)  gross med
  all rows                                         462   142   69  -13.12[x]  -10.59[x] +3294.38[x]      -4.65
  without the reviewed token row                   461   141   69  -13.17[x]  -10.63[x]    -2.04[ ]      -4.68
  without priced pool-switch rows (ok/thin)        446   133   66  -12.24[x]   -9.69[x]    +0.79[ ]      -4.48
  without all pool-switch rows (all statuses)      439   127   61  -11.57[x]   -9.03[x]    +2.31[ ]      -3.66

reproduces the read (-13.12 / -10.59 / +3294.38): yes
```

## Not verified

- Whether pool 0x15896c06… really trades this token. Taken from the log as written, not checked on chain.
- The other 22 pool-switch rows, one by one. Only the row above +900% net is listed.
- Pool switches among baseline peers. Clause (b)'s baseline may carry the same artefact.

Test: tests/test_h0003_poolswitch.py (synthetic rows only).
