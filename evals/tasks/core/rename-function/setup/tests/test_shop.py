import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from shop.cart import get_total
from shop.checkout import checkout


def main():
    assert get_total([(2.5, 2), (1.0, 3)]) == 8.0
    assert checkout([(10.0, 1)], coupon="SAVE10") == 9.0
    print("all tests passed")


if __name__ == "__main__":
    main()
