"""A tiny calculator: tokenize, parse with precedence climbing, evaluate."""


def tokenize(text):
    tokens, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch.isdigit() or ch == ".":
            tokens.append(("num", float(ch)))
            i += 1
        elif ch in "+-*/()":
            tokens.append(("op", ch))
            i += 1
        else:
            raise ValueError(f"unexpected character {ch!r}")
    return tokens


PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2}


def parse(tokens):
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else (None, None)

    def primary():
        nonlocal pos
        kind, value = peek()
        if kind == "num":
            pos += 1
            return value
        if value == "(":
            pos += 1
            result = expression(0)
            pos += 1  # skip ')'
            return result
        raise ValueError(f"unexpected token {value!r}")

    def expression(min_prec):
        nonlocal pos
        left = primary()
        while True:
            kind, op = peek()
            if kind != "op" or op not in PRECEDENCE or PRECEDENCE[op] < min_prec:
                return left
            pos += 1
            right = expression(PRECEDENCE[op])
            left = apply(op, left, right)

    return expression(0)


def apply(op, a, b):
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a // b
    raise ValueError(op)


def calculate(text):
    return parse(tokenize(text))
