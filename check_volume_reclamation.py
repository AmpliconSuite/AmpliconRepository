#!/usr/bin/env python
"""
check_volume_reclamation.py

Answer one question: has DocumentDB given back the storage we deleted?

Between 31 August and 2 September 2026, 257.80 GiB was deleted from the
``ampliconubuntu`` cluster -- 60.09 GiB of soft-deleted production projects,
42.8 GiB of production GridFS residue, and 154.89 GiB from ``caper-dev``.  The
billed figure, CloudWatch's ``VolumeBytesUsed``, did not move.  Reclamation on
this engine is reported to take days, so the question stays open, and it is the
kind of question somebody asks again in three months with no memory of how it
was measured the first time.  Hence a script rather than a note.

**The trap this exists to avoid.**  ``VolumeBytesUsed`` dips below the plateau
three or four times a day on its own.  Over the seven days to 2026-09-02 it did
so 25 times, for 45 to 192 minutes each, as deep as 697.34 GiB -- and every one
returned to the same 713.1348 GiB plateau.  A dip is not reclamation, however
deep and however long it lasts; one was called reclamation once and was wrong.

So this reads the **maximum over a trailing window**, not the current value.
The longest excursion observed is a little over three hours, so an eight-hour
window always contains plateau time and its maximum is the plateau even when
the read happens mid-dip.  Only a fall in *that* number means storage came back.

Do not answer this question with ``collStats``.  ``unusedStorageSize`` on this
cluster froze byte-identical across a 4 GiB write; it was the basis for a
"~43 GB is already free" claim that was not true.

Usage:
    python check_volume_reclamation.py
    python check_volume_reclamation.py --days 30 --profile amprepo
"""

import argparse
import datetime
import sys

GIB = 1024 ** 3

# Measured 2026-09-02 14:17 UTC, after all 257.80 GiB had been deleted.
BASELINE_GIB = 713.1349
BASELINE_TAKEN = '2026-09-02'
DELETED_GIB = 257.80

# Anything under this is a real move rather than jitter.  It sits below the
# deepest dip ever observed (697.34 GiB) only because we read the trailing
# maximum: a dip cannot pull an eight-hour maximum down, so the floor is
# compared against the plateau and not against a momentary value.
PLATEAU_FLOOR_GIB = 700.0

# Dips are counted against this, which is where the metric spends its time
# between excursions rather than a threshold anyone chose.
DIP_THRESHOLD_GIB = 710.0

WINDOW_HOURS = 8


def _client(profile, region):
    import boto3
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client('cloudwatch', region_name=region)


def series(cloudwatch, cluster, start, end, period):
    """Datapoints for VolumeBytesUsed, oldest first, in GiB."""
    response = cloudwatch.get_metric_statistics(
        Namespace='AWS/DocDB',
        MetricName='VolumeBytesUsed',
        Dimensions=[{'Name': 'DBClusterIdentifier', 'Value': cluster}],
        StartTime=start, EndTime=end, Period=period,
        Statistics=['Maximum', 'Minimum'],
    )
    points = sorted(response['Datapoints'], key=lambda p: p['Timestamp'])
    return [(p['Timestamp'], p['Maximum'] / GIB, p['Minimum'] / GIB)
            for p in points]


def dip_census(points, threshold, period_seconds):
    """Runs of consecutive datapoints whose minimum went below *threshold*.

    Reported as (count, shortest_minutes, longest_minutes, deepest_gib).  A dip
    that is still in progress at the end of the window is counted; its length is
    a lower bound, which is the safe direction for a number used to argue that
    dips are short.

    The count and the durations shift with the sampling period and with where
    the window happens to start -- two adjacent excursions separated by one
    plateau datapoint are two dips at 15 minutes and one at an hour.  Treat them
    as "several a day, an hour or two each" rather than as exact figures.  The
    depth is the stable number, and the plateau is the one that matters.
    """
    runs, current, deepest = [], 0, None
    for _, _, low in points:
        if low < threshold:
            current += 1
            deepest = low if deepest is None else min(deepest, low)
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    if not runs:
        return 0, None, None, None
    minutes = [r * period_seconds / 60.0 for r in runs]
    return len(runs), min(minutes), max(minutes), deepest


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cluster', default='ampliconubuntu')
    parser.add_argument('--days', type=int, default=7,
                        help='how far back to census the dips (default 7)')
    parser.add_argument('--profile', default=None,
                        help='AWS profile; omit to use the ambient credentials')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--baseline', type=float, default=BASELINE_GIB,
                        help='plateau to compare against (default %.4f GiB, '
                             'measured %s)' % (BASELINE_GIB, BASELINE_TAKEN))
    args = parser.parse_args()

    cloudwatch = _client(args.profile, args.region)
    now = datetime.datetime.now(datetime.timezone.utc)

    recent = series(cloudwatch, args.cluster,
                    now - datetime.timedelta(hours=WINDOW_HOURS), now, 900)
    if not recent:
        print('No datapoints for %s in the last %d hours.' % (args.cluster, WINDOW_HOURS))
        print('CloudWatch publishes nothing at all while a cluster is creating '
              'or stopped, so an empty result is not the same as zero.')
        return 2

    plateau = max(high for _, high, _ in recent)
    current = recent[-1][1]
    delta = plateau - args.baseline

    # 15-minute resolution, not hourly: at 3600 s a 50-minute dip and a
    # 3-hour one both round to "1 to 3 datapoints", and the census then
    # overstates how long the excursions last. CloudWatch keeps 15-minute data
    # for 63 days, which covers any window worth asking about here.
    census_period = 900
    history = series(cloudwatch, args.cluster,
                     now - datetime.timedelta(days=args.days), now, census_period)
    count, shortest, longest, deepest = dip_census(history, DIP_THRESHOLD_GIB, census_period)

    print('cluster            %s' % args.cluster)
    print('read at            %s' % now.strftime('%Y-%m-%d %H:%M UTC'))
    print('')
    print('plateau            %.4f GiB   (maximum over the trailing %d h -- this is the number that matters)'
          % (plateau, WINDOW_HOURS))
    print('latest datapoint   %.4f GiB   (may be mid-dip; not evidence on its own)' % current)
    print('baseline           %.4f GiB   (measured %s, after %.2f GiB was deleted)'
          % (args.baseline, BASELINE_TAKEN, DELETED_GIB))
    print('change             %+.4f GiB' % delta)
    print('')

    if count:
        print('dips below %.0f GiB in the last %d day(s): %d, lasting %.0f-%.0f min, deepest %.2f GiB'
              % (DIP_THRESHOLD_GIB, args.days, count, shortest, longest, deepest))
        print('  Every one of these is expected. They are not reclamation.')
    else:
        print('no dips below %.0f GiB in the last %d day(s)' % (DIP_THRESHOLD_GIB, args.days))
    print('')

    if plateau < PLATEAU_FLOOR_GIB:
        print('VERDICT: the plateau has fallen below %.0f GiB. Storage has been returned.'
              % PLATEAU_FLOOR_GIB)
        print('  This is the outcome the deletion campaign was waiting on. Record the')
        print('  date, and re-baseline this script so the next person compares against')
        print('  the new plateau rather than the old one.')
        return 0

    if delta < -1.0:
        print('VERDICT: the plateau has moved down %.2f GiB but is still above %.0f GiB.'
              % (-delta, PLATEAU_FLOOR_GIB))
        print('  Worth watching. Re-run over more days before concluding anything.')
        return 0

    print('VERDICT: no reclamation. The plateau is where it was.')
    print('  %.2f GiB was deleted and the billed volume has not changed.' % DELETED_GIB)
    print('  This has been the answer since 2026-09-02. It is a fact about the')
    print('  engine, not a fault, and nothing on our side is waiting on it.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
