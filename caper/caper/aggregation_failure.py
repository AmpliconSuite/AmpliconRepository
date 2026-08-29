"""Turning an aggregator failure into something a user can act on.

The aggregator runs in-process, and it reports fatal conditions by writing a
sentence to stderr and calling ``sys.exit(1)``.  The site catches that
``SystemExit`` in the background-aggregation handler, and until 2026-08-28 built
its message with ``str(e)`` -- which for ``SystemExit(1)`` is the string
``'1'``.  So every one of these reached the user as:

    An error occurred during aggregation: 1

and the sentence explaining what was actually wrong went to a stderr nobody
reads.  Measured on 2026-08-28 by removing every sample from a project locally:
the aggregator said "Every sample in the input was excluded -- there is nothing
left to aggregate" and the project's ``error_message`` said ``1``.

The three conditions the aggregator aborts on are all user-actionable, and two
of them describe an input the user can go and fix:

  * no ``*_result_table.tsv`` -- AmpliconClassifier was never run
  * every sample excluded -- an edit removed the last sample
  * multiple reference genomes in one project

**Why the traceback and not stderr.**  The obvious way to recover the sentence
is to capture stderr around the aggregator call.  That is wrong here:
aggregation runs in a background thread, ``sys.stderr`` is process-global, and
swapping it would splice one upload's output into another's.  The exception
carries its own traceback, ``_abort`` is a frame in it, and the sentence is a
local variable of that frame -- thread-safe by construction, with no global
state touched.

The coupling to ``_abort``'s name and parameter is real, and deliberate: it is
pinned by a test that reads them off the installed aggregator, so a rename
fails the suite rather than silently returning users to exit codes.
"""

# The aggregator function that reports a fatal condition, and the parameter it
# takes the sentence in. Verified against the installed package by
# tests/test_aggregation_failure.py.
ABORT_FUNCTION = '_abort'
ABORT_MESSAGE_LOCAL = 'message'

GENERIC = 'An error occurred during aggregation'


def abort_reason(exc):
    """The sentence the aggregator aborted with, or None if it did not.

    Walks the traceback outwards-in and keeps the *last* matching frame, which
    is the innermost ``_abort`` -- the one that actually raised.
    """
    reason = None
    traceback = getattr(exc, '__traceback__', None)
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_name == ABORT_FUNCTION:
            candidate = frame.f_locals.get(ABORT_MESSAGE_LOCAL)
            if isinstance(candidate, str) and candidate.strip():
                reason = candidate.strip()
        traceback = traceback.tb_next
    return reason


def aggregation_error_message(exc):
    """The user-facing message for a failure raised out of the aggregator.

    Never raises: this runs inside an ``except BaseException`` handler whose
    whole job is to record why something failed, and a failure to describe a
    failure would replace a bad message with no message at all.
    """
    try:
        reason = abort_reason(exc)
        if reason:
            return f'{GENERIC}: {reason}'

        if isinstance(exc, SystemExit):
            # sys.exit('some text') puts the text in .code; sys.exit(1) puts the
            # status there. The first is worth showing, the second is not.
            code = exc.code
            if isinstance(code, str) and code.strip():
                return f'{GENERIC}: {code.strip()}'
            return (f'{GENERIC}. The aggregator stopped with exit status '
                    f'{code!r} without reporting a reason; the server log for '
                    f'this project has the traceback.')

        detail = str(exc).strip()
        if not detail:
            # A bare raise of an exception with no arguments -- str() is ''. The
            # class name is the only thing left that says anything at all.
            return f'{GENERIC}: {type(exc).__name__}'
        return f'{GENERIC}: {detail}'
    except Exception:                                     # noqa: BLE001
        return f'{GENERIC}.'
