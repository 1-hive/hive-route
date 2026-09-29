def parse_duration(text: str) -> int:
    """Parse a duration such as ``"1h30m"`` into a number of seconds.

    - A duration is one or more parts, each a whole number followed by a unit: ``d``
      (days), ``h`` (hours), ``m`` (minutes) or ``s`` (seconds). Examples: ``"45s"``,
      ``"2d"``, ``"1h30m"``, ``"1d2h3m4s"``.
    - Parts may be separated by single spaces: ``"1h 30m"``. Leading and trailing spaces
      are ignored. No other characters are allowed.
    - Units must appear in the order d, h, m, s, each at most once.
    - Numbers are non-negative integers without a sign; leading zeros are allowed.
    - Anything else, including an empty string, raises ``ValueError``.
    """
    raise NotImplementedError
