from calc import calculate

CASES = [
    ("1 + 2", 3),
    ("2 * 3 + 4", 10),
    ("2 + 3 * 4", 14),
    ("12 + 30", 42),
    ("7 / 2", 3.5),
    ("10 - 4 - 3", 3),
    ("(1 + 2) * 3", 9),
    ("-5 + 2", -3),
    ("2 * (3 + 4) - 1", 13),
    ("1.5 * 4", 6),
]


def main():
    failed = []
    for expr, want in CASES:
        try:
            got = calculate(expr)
        except Exception as exc:
            got = f"error: {exc}"
        if got != want:
            failed.append(f"{expr!r}: got {got!r}, want {want!r}")
    if failed:
        print(f"{len(failed)} of {len(CASES)} tests failed:")
        print("\n".join(failed))
    else:
        print(f"all {len(CASES)} tests passed")


if __name__ == "__main__":
    main()
