"""Test package.

Present so ``tests.helpers`` resolves to one unambiguous module name — without
it mypy sees ``tests/helpers.py`` as both ``helpers`` and ``tests.helpers`` and
refuses to check the suite.
"""
