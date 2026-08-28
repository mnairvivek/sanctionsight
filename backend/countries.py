"""Built-in sanctioned jurisdictions.

Kept in its own module so both main.py (API surface, country selector endpoint)
and sanctions_engine.py (search/NLP iteration) can import without cycling.
"""

BUILTINS = [
    "Iran", "Syria", "North Korea", "Cuba",
    "Luhansk", "Donetsk", "Crimea", "Ukraine",
    "Russia", "Belarus", "Myanmar", "Venezuela",
]
