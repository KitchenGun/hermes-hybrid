from solution import two_sum


def test_basic():
    i, j = two_sum([2, 7, 11, 15], 9)
    assert i < j
    assert {i, j} == {0, 1}


def test_middle():
    i, j = two_sum([1, 2, 3, 4, 5], 8)
    assert i < j
    assert {i, j} == {2, 4}  # 3 + 5 = 8


def test_with_duplicates():
    i, j = two_sum([3, 3, 5, 7], 6)
    assert i < j
    assert {i, j} == {0, 1}
