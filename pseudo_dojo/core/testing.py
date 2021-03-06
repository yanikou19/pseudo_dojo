"""
Common test support for pseudo_dojo.

This single module should provide all the common functionality for tests
in a single location, so that test scripts can just import it and work right away.
"""
from unittest import TestCase

import numpy.testing.utils as nptu

class PseudoDojoTest(TestCase):
    """Extend TestCase with functions from numpy.testing.utils that support ndarrays."""

    @staticmethod
    def assert_almost_equal(actual, desired, decimal=7, err_msg='', verbose=True):
        return nptu.assert_almost_equal(actual, desired, decimal, err_msg, verbose)

    @staticmethod
    def assert_equal(actual, desired, err_msg='', verbose=True):
        return nptu.assert_equal(actual, desired, err_msg=err_msg, verbose=verbose)
