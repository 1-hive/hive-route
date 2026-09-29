"""Check for review-clean: the review's verdict must be passed."""
import json
import os
import sys

path = os.path.join(os.environ["CANARY_WORK"], "REVIEW.json")
try:
    review = json.load(open(path))
except (OSError, ValueError) as e:
    sys.exit(f"no readable REVIEW.json: {e}")
verdict = review.get("verdict")
if verdict != "passed":
    sys.exit(f"verdict {verdict!r}, expected 'passed': {review.get('reason', '')[:300]}")
print("ok")
