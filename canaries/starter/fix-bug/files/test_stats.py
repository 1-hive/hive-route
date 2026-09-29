from stats import mean, median, moving_average


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5


def test_moving_average():
    assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
    assert moving_average([5], 1) == [5]
