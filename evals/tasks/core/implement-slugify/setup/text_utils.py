def slugify(text: str) -> str:
    """Turn a title into a URL slug.

    Rules:
    - lowercase everything
    - replace each run of characters that are not a-z or 0-9 with a single hyphen
    - strip hyphens from both ends
    - return "untitled" if nothing is left

    >>> slugify("Hello, World!")
    'hello-world'
    >>> slugify("  Spool: event-sourced agents  ")
    'spool-event-sourced-agents'
    """
    raise NotImplementedError
