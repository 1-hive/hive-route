"""Check for implement-spec: parse_duration against its docstring."""
import os
import sys

sys.path.insert(0, os.environ["CANARY_WORK"])
from duration import parse_duration  # noqa: E402

good = {"45s": 45, "2d": 172800, "1h30m": 5400, "1d2h3m4s": 93784, "1h 30m": 5400,
        "  10m  ": 600, "0s": 0, "007m": 420, "90m": 5400, "1d 1s": 86401}
for text, want in good.items():
    got = parse_duration(text)
    assert got == want, (text, got, want)
bad = ["", "   ", "10", "h", "1x", "-5m", "+5m", "1.5h", "30m1h", "1h1h", "1h  30m", "1h,30m",
       "1 h", "5m\t", "1e3s"]
for text in bad:
    try:
        parse_duration(text)
    except ValueError:
        continue
    raise AssertionError(f"accepted {text!r}")
print("ok")
