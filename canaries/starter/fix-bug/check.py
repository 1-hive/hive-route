"""Check for fix-bug: the original tests, plus more, run against the work folder."""
import os
import sys

sys.path.insert(0, os.environ["CANARY_WORK"])
from stats import mean, median, moving_average  # noqa: E402

assert mean([1, 2, 3, 4]) == 2.5
assert median([3, 1, 2]) == 2
assert median([4, 1, 3, 2]) == 2.5
assert median([10, 20]) == 15
assert median([7]) == 7
assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
assert moving_average([5], 1) == [5]
assert moving_average([1, 2, 3], 3) == [2]
assert moving_average([1, 2], 3) == []
for bad in (lambda: median([]), lambda: mean([]), lambda: moving_average([1], 0)):
    try:
        bad()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
print("ok")
