# `bucket(age)`

Returns the age bracket of a person for pricing:

- `"child"` for ages 0 to 12 inclusive,
- `"adult"` for ages 13 to 64 inclusive,
- `"senior"` for 65 and over.

Negative ages raise `ValueError`. The change should make the function reject non-integer ages (floats, strings) with `TypeError`; booleans are not ages either.
