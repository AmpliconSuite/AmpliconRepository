"""The tool versions an update is built with. Change these for the next AC release."""

import os
import re

AC_REPO = 'https://github.com/AmpliconSuite/AmpliconClassifier.git'
AC_TAG = 'v2.0.1'
AC_VERSION = '2.0.1'
# Checked as well as the tag, so a moved tag can't change results unnoticed.
AC_COMMIT = '2ec0c146f78e74b4857860fc30b5e8a1de7920b7'

BFBARCHITECT = '1.0.2'
GUROBI = '13.0.2'
REQUIRED_SOLVER = 'gurobi'

SITE_REQUIREMENTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 'requirements.txt')


def aggregator_pin(path=SITE_REQUIREMENTS):
    """The aggregator the site runs. The site rebuilds every upload with it."""
    with open(path) as handle:
        for line in handle:
            match = re.match(r'\s*AmpliconSuiteAggregator==(\S+)', line, re.I)
            if match:
                return match.group(1)
    raise ValueError('no AmpliconSuiteAggregator pin in %s' % path)
