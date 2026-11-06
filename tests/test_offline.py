"""Pytest entry point for the checks that need no audio and no model.

The suites themselves stay runnable as `python -m server.test_<name>`.
This file is how CI collects them, measures coverage, and fails the build.
"""

from __future__ import annotations

import importlib

import pytest

OFFLINE = [
    "server.test_security",
    "server.test_phi",
    "server.test_profiles",
    "server.test_providers",
    "server.test_corrections",
    "server.test_textclean",
]


@pytest.mark.parametrize("module_name", OFFLINE)
def test_offline_module(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.main() == 0, module_name
