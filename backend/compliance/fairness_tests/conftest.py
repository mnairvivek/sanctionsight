"""Make the backend root importable when pytest discovers these tests.

Mirrors ``backend/tests/conftest.py`` — the fairness suite lives under
``backend/compliance/`` so that regulators reviewing the validation
package see it alongside the model card, but the tests still need to
import from ``backend/`` directly.
"""
import os
import sys

BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
