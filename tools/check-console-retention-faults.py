"""Q5 regression: routine output cannot evict an older routine line across reads."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('diagnostic_faults', Path(__file__).with_name('check-evidence-faults.py'))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
base.__dict__["FAULTS"] = [
    ('Q5-routine-eviction', 'qajev/diagnostics.py', 'if item["level"] in {"warn", "error"} else None',
     'if item["level"] in {"log", "info", "warn", "error"} else None',
     'tests/test_page_console.py::test_first_fifty_routine_lines_stay_after_a_later_routine_line_in_a_second_read'),
]

if __name__ == '__main__':
    raise SystemExit(base.main())
