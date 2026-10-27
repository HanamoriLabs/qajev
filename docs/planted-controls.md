# Planted controls in reports

A control with `expect_fail` deliberately supplies a fault for its named check to
catch. When that check catches it and all other checks pass, the HTML report
shows **CAUGHT** in green and counts it separately, for example
`15 pass · 1 plant caught`.

A control graded `fail` stays red and says **the check missed a planted fault**.
A browser or harness interruption remains incomplete and never counts as a
catch. Ordinary product failures retain their existing failure result.

The JSON report keeps the original `outcome`, `checks` and `counts`. For planted
controls, `expected`, `expected_check`, `graded` and `graded_reason` explain the
gate's grade. Human display labels do not change how the gate is calculated.
