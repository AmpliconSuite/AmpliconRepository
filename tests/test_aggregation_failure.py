"""The user is told why aggregation failed, not what exit status it used.

Measured on 2026-08-28, locally, by removing every sample from a project: the
aggregator printed "Every sample in the input was excluded -- there is nothing
left to aggregate" and the project's stored ``error_message`` was:

    An error occurred during aggregation: 1

``1`` is ``str(SystemExit(1))``.  All three of the aggregator's fatal conditions
are user-actionable and all three arrived looking like that.
"""

import inspect

import pytest

from caper.aggregation_failure import (
    ABORT_FUNCTION,
    ABORT_MESSAGE_LOCAL,
    abort_reason,
    aggregation_error_message,
)

# Verbatim from asa_stages.py, aggregator 8.3.0 (installed on dev and prod,
# checked 2026-08-28). Reproduced here so the test states what a user sees.
EVERY_SAMPLE_EXCLUDED = ('Every sample in the input was excluded — '
                         'there is nothing left to aggregate.')
NO_RESULT_TABLES = ('No *_result_table.tsv files found. Ensure '
                    'AmpliconClassifier has been run before aggregating.')
MULTIPLE_GENOMES = ("Multiple reference genomes detected: {'hg19', 'GRCh38'}. "
                    'AmpliconRepository only supports single-reference projects.')


def _raise_via_abort(message, depth=0):
    """Raise the SystemExit the aggregator raises, through a frame named as
    the aggregator names it. Not a mock of the recovery -- the recovery reads
    a real traceback, so the test has to produce one."""
    import sys

    def _abort(message):                        # noqa: ANN001 -- mirrors asa_stages
        sys.stderr.write(f'FATAL: {message}\n')
        sys.exit(1)

    def _outer(message, depth):
        if depth:
            return _raise_via_abort(message, depth - 1)
        return _abort(message)

    return _outer(message, depth)


@pytest.mark.parametrize('sentence', [
    EVERY_SAMPLE_EXCLUDED, NO_RESULT_TABLES, MULTIPLE_GENOMES])
def test_the_reason_reaches_the_user(sentence):
    """Each of the aggregator's three fatal conditions, end to end."""
    try:
        _raise_via_abort(sentence)
    except BaseException as exc:                # noqa: BLE001 -- what views does
        message = aggregation_error_message(exc)

    assert sentence in message, (
        f'the reason was lost; user would see {message!r}')
    # The specific regression: the old message was the exit status alone.
    assert message != 'An error occurred during aggregation: 1'
    assert not message.endswith(': 1')


def test_installed_aggregator_still_uses_the_names_we_read():
    """Pins the coupling to the aggregator's internals.

    ``abort_reason`` finds the reason by looking for a frame named ``_abort``
    and reading its ``message`` local. That is a real coupling to another
    project's private function, and the honest way to hold it is to check it
    against whatever aggregator is actually importable -- so a rename fails
    here rather than silently returning every user to bare exit codes.

    Confirmed 2026-08-28 against 8.3.0, the version installed in both the dev
    and prod containers. Locally this resolves to AGGREGATOR_DEV_PATH's working
    copy, which settings.py puts ahead of the installed package on sys.path.
    """
    try:
        import asa_stages
    except ImportError:                          # pragma: no cover
        pytest.skip('the aggregator is not importable in this environment')

    abort = getattr(asa_stages.Aggregator, ABORT_FUNCTION, None)
    assert abort is not None, (
        f'the aggregator no longer has {ABORT_FUNCTION}(); '
        f'abort_reason() cannot find the reason and every fatal condition is '
        f'back to being reported as an exit status')
    assert ABORT_MESSAGE_LOCAL in inspect.signature(abort).parameters, (
        f'{ABORT_FUNCTION}() no longer takes a parameter named '
        f'{ABORT_MESSAGE_LOCAL!r}; abort_reason() reads that local by name')


def test_innermost_abort_wins():
    """Nested frames: the reason is the one that actually raised."""
    try:
        _raise_via_abort('the innermost reason', depth=2)
    except BaseException as exc:                # noqa: BLE001
        assert abort_reason(exc) == 'the innermost reason'


def test_system_exit_without_a_reason_says_that_rather_than_inventing_one():
    """A bare sys.exit from somewhere that is not _abort.

    Several of the aggregator's exit paths write to stderr and exit without
    going through _abort, so there is nothing to recover. The message must say
    so and point at the log, not print a naked ``1``.
    """
    try:
        raise SystemExit(1)
    except BaseException as exc:                # noqa: BLE001
        # Bound to a second name because Python unbinds the `as` target when
        # the except block ends.
        raised, message = exc, aggregation_error_message(exc)

    assert abort_reason(raised) is None
    assert not message.endswith(': 1')
    assert 'exit status' in message and 'log' in message


def test_system_exit_carrying_text_uses_the_text():
    try:
        raise SystemExit('the input archive is not readable')
    except BaseException as exc:                # noqa: BLE001
        assert 'the input archive is not readable' in aggregation_error_message(exc)


def test_ordinary_exception_keeps_its_message():
    """The non-SystemExit case must behave as it always did."""
    try:
        raise RuntimeError('Sample removal needs AmpliconSuiteAggregator >= 8.3.0')
    except BaseException as exc:                # noqa: BLE001
        message = aggregation_error_message(exc)
    assert message == ('An error occurred during aggregation: '
                       'Sample removal needs AmpliconSuiteAggregator >= 8.3.0')


def test_exception_with_no_message_names_its_class():
    try:
        raise MemoryError()
    except BaseException as exc:                # noqa: BLE001
        assert 'MemoryError' in aggregation_error_message(exc)


def test_describing_a_failure_never_fails():
    """This runs inside the handler that records why something broke.

    If it raised, the except block would lose the message it was called to
    produce *and* the one it was handling.
    """
    class Awkward(Exception):
        def __str__(self):
            raise ValueError('even my message is broken')

    message = aggregation_error_message(Awkward())
    assert message.startswith('An error occurred during aggregation')


def test_views_uses_the_helper():
    """The old expression must not survive anywhere in the aggregation path."""
    import caper.views as views

    source = inspect.getsource(views)
    assert "f'An error occurred during aggregation: {str(e)}'" not in source, (
        'views.py is building the message with str(e) again, which renders '
        'SystemExit(1) as "1"')
