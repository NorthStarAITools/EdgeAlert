# Edge Alert Landing — Accuracy Tab Hide Diff
**Drafted:** 2026-04-11 by M4-Cowork
**Status:** STAGED FOR CEO REVIEW — do not apply until P0 stable + 30 days of real signal data exist
**File:** `products/EdgeAlert/landing/index.html`

## Why
Scanner just produced its first real signal today (PID 15259, 🟢 HIGH NO KXBTC15M-26APR111200-00, +393.1% edge). Live accuracy dashboard would be embarrassingly empty if shipped now. Hide every accuracy reference until 30 days of real data exist.

## What gets hidden
1. Nav link "Accuracy" (line 496)
2. Hero "View Accuracy Data" button (line 520)
3. Entire `<section id="accuracy">` block (lines 630–668)
4. "Accuracy dashboard access" line in Basic plan features (line 691)

(Pro plan has no accuracy reference — verified.)

## The diff

### Change 1 — Nav link (line 496)
```diff
-      <a href="#accuracy">Accuracy</a>
+      <!-- <a href="#accuracy">Accuracy</a> --> <!-- HIDDEN until 30d real data -->
```

### Change 2 — Hero secondary CTA (line 520)
```diff
-    <a href="accuracy_dashboard.html" class="btn-secondary">View Accuracy Data</a>
+    <!-- <a href="accuracy_dashboard.html" class="btn-secondary">View Accuracy Data</a> --> <!-- HIDDEN until 30d real data -->
```

### Change 3 — Entire Accuracy section (lines 630–668)
Wrap the whole `<section id="accuracy">...</section>` block in an HTML comment. Cleanest way: add `<!--` immediately before line 630 and `-->` immediately after line 668.

```diff
+<!-- HIDDEN until 30 days of real signal data — 2026-04-11
 <!-- ── Accuracy ──────────────────────────────────────────────────────── -->
 <section id="accuracy" style="background: var(--surface);">
   ...
 </section>
+-->
```

### Change 4 — Basic plan feature (line 691)
```diff
-          <li>Accuracy dashboard access</li>
+          <!-- <li>Accuracy dashboard access</li> --> <!-- HIDDEN until 30d real data -->
```

## What stays
- The `accuracy_dashboard.html` file itself stays in the directory. Just don't link to it.
- Calibration stats ("80,000+ markets calibrated") stay — those are real backtest numbers, not live signal accuracy.

## Unhide criteria (BOTH must be true)
1. Edge Alert scanner has produced ≥1 real signal per day for 30 consecutive days
2. Accuracy dashboard shows ≥10 settled signals with predicted-vs-actual data

## Application steps (when CEO approves)
1. CC CLI applies the 5 edits above
2. Visual smoke test: open index.html locally, confirm no broken layout
3. Commit on `main` branch with message: `Hide accuracy tab until 30d real data`
4. Then push to GitHub Pages (THIS is the GitHub Pages push that's been on hold)
