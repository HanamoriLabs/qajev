# Saved-frame visible-effect regression

`on.jpg` and `off.jpg` are unmodified captures supplied by the application owner. `provenance.json` records their
original names, hashes, suite commit, scenario and run. Both frames are 1280 by 900. The region is the street under
the cafe tables: x 890, y 560, width 380, height 300. The original suite passed despite no visible shadow change.

The offline test decodes the JPEGs, then supplies lossless PNG crops through the real region-capture method,
scenario runner, pixel comparison and report gate. It must produce FAIL. The production capture path still
requires complete PNG input; the test does not relax that validation or recompress the source frames as JPEG.

The test's tolerance is 15: integer channel differences of 16 or more exceed 6% of 255. The required changed
fraction is 1%. The supplied pair's small noise must stay below that minimum.

The passing control creates two new grey 1280 by 900 images, with a dark 180 by 120 patch added only inside the
same region in the second image. Exactly 21,600 of 114,000 region pixels change. This is a synthetic control,
not a claim that the application's current shadows render correctly.

Run `tests/test_effect_saved_frames.py` through the normal boxtest queue on workload-cpu with `--box-only`.
It requires no browser, model, GPU, cloud run or operator credentials. Live application acceptance remains separate.
