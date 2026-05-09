from solution import fizzbuzz


def test_fizz():
    assert fizzbuzz(3) == "Fizz"
    assert fizzbuzz(9) == "Fizz"


def test_buzz():
    assert fizzbuzz(5) == "Buzz"
    assert fizzbuzz(10) == "Buzz"


def test_fizzbuzz_combo():
    assert fizzbuzz(15) == "FizzBuzz"
    assert fizzbuzz(30) == "FizzBuzz"


def test_plain():
    assert fizzbuzz(1) == "1"
    assert fizzbuzz(2) == "2"
    assert fizzbuzz(7) == "7"
