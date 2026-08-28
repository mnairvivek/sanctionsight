"""
Ensure the tests can import backend/*.py directly. This package sits inside
backend/, but pytest's rootdir may be higher, so we add backend/ explicitly.
"""
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
