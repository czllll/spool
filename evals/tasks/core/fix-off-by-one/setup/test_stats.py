from stats import moving_average, normalize


def test_moving_average():
    assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
    assert moving_average([5, 5, 5], 3) == [5.0]
    assert moving_average([1, 2], 3) == []


def test_normalize():
    assert normalize([2, 4, 6]) == [0.0, 0.5, 1.0]
    assert normalize([3, 3]) == [0.0, 0.0]


if __name__ == "__main__":
    test_moving_average()
    test_normalize()
    print("all tests passed")
