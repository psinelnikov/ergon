"""Run the test suite in parallel: one process per core, standard library only.

    python3 tests/run_parallel.py                      # every test, one worker per core
    python3 tests/run_parallel.py -j 4                 # four workers
    python3 tests/run_parallel.py test_ergon.TestAccept test_serve   # only these
    python3 tests/run_parallel.py -v                   # a line per test with its time

Most tests build a throwaway git repo and drive the real CLI, so each is isolated and they can
run side by side. The runner finds tests as `python3 -m unittest discover -s tests` does, hands
each test id to a pool of worker processes, and prints a unittest-style report: the failures
and errors with their tracebacks, then `Ran N tests` and `OK` or `FAILED (...)`. It exits 1 if
any test fails or errors, if a test module can't be imported, or if a worker dies.

Each test runs on its own, so setUpClass and setUpModule run once per test, not once per class
or module. A test's stdout and stderr are captured and shown only with its failure, as
unittest's --buffer does. The serial run, `python3 -m unittest discover -s tests`, still works
and gives the same results.
"""
import argparse
import os
import sys
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
import multiprocessing

TESTS = os.path.dirname(os.path.abspath(__file__))


def loader():
    return unittest.TestLoader()


def flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t


def collect(names):
    """The test cases to run: everything discover finds under tests/, or only the named
    modules, classes and methods (as unittest names them, e.g. test_ergon.TestAccept)."""
    ld = loader()
    suite = ld.loadTestsFromNames(names) if names else ld.discover(TESTS, top_level_dir=TESTS)
    return list(flatten(suite))  # a module that can't be imported comes back as a _FailedTest


def init_worker():
    if TESTS not in sys.path:
        sys.path.insert(0, TESTS)


def run_one(tid):
    """Run one test by id in this worker. Returns (id, seconds, problems, counts): problems are
    (kind, test description, traceback) triples, already strings, so they pickle."""
    start = time.time()
    result = unittest.TestResult()
    result.buffer = True
    suite = loader().loadTestsFromName(tid)
    suite.run(result)
    problems = ([("ERROR", str(t), tb) for t, tb in result.errors]
                + [("FAIL", str(t), tb) for t, tb in result.failures]
                + [("UNEXPECTED SUCCESS", str(t), "") for t in result.unexpectedSuccesses])
    counts = {"run": result.testsRun, "skipped": len(result.skipped),
              "expected": len(result.expectedFailures)}
    return tid, time.time() - start, problems, counts


def run_here(test):
    """Run a test in this process: the stand-ins unittest makes for a module it couldn't import,
    whose id names no real test, so a worker couldn't load them by id."""
    result = unittest.TestResult()
    test.run(result)
    return [("ERROR", str(t), tb) for t, tb in result.errors + result.failures]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the test suite in parallel (standard library only).")
    ap.add_argument("names", nargs="*", help="test modules, classes or methods, e.g. test_ergon.TestAccept "
                                              "(default: every test under tests/)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="worker processes (default: one per core, %(default)s here)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print each test with its time as it finishes")
    args = ap.parse_args(argv)
    if args.jobs < 1:
        ap.error("--jobs must be at least 1")

    init_worker()
    tests = collect(args.names)
    here = [t for t in tests if isinstance(t, unittest.loader._FailedTest)]
    ids = [t.id() for t in tests if not isinstance(t, unittest.loader._FailedTest)]
    problems = [p for t in here for p in run_here(t)]
    run = len(here)
    skipped = expected = 0
    start = time.time()
    broken = None
    jobs = min(args.jobs, len(ids)) or 1
    print(f"running {len(ids)} tests in {jobs} processes", file=sys.stderr)
    with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"),
                             initializer=init_worker) as pool:
        futures = {pool.submit(run_one, tid): tid for tid in ids}
        try:
            for f in as_completed(futures):
                try:
                    tid, secs, found, counts = f.result()
                except BrokenProcessPool as e:
                    broken = f"a worker process died while running {futures[f]} (or another test): {e}"
                    break
                problems += found
                run += counts["run"]
                skipped += counts["skipped"]
                expected += counts["expected"]
                if args.verbose:
                    mark = found[0][0] if found else "ok"
                    print(f"{secs:6.1f}s  {mark:5}  {tid}", file=sys.stderr)
                elif found:
                    sys.stderr.write({"FAIL": "F", "ERROR": "E"}.get(found[0][0], "u"))
                else:
                    sys.stderr.write(".")
                sys.stderr.flush()
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    took = time.time() - start
    if not args.verbose:
        sys.stderr.write("\n")

    for kind, desc, tb in sorted(problems, key=lambda p: p[1]):
        print(f"{'=' * 70}\n{kind}: {desc}\n{'-' * 70}\n{tb}", file=sys.stderr)
    print(f"{'-' * 70}\nRan {run} test{'' if run == 1 else 's'} in {took:.1f}s "
          f"({jobs} process{'' if jobs == 1 else 'es'})\n", file=sys.stderr)
    counted = [(name, sum(k == kind for k, _, _ in problems))
               for name, kind in (("failures", "FAIL"), ("errors", "ERROR"),
                                  ("unexpected successes", "UNEXPECTED SUCCESS"))]
    bad = [f"{n}={v}" for n, v in counted if v]
    if broken:
        bad.append("a worker died")
        print(broken, file=sys.stderr)
    if not run and not broken:
        bad.append("no tests ran")
    extra = [f"{n}={v}" for n, v in (("skipped", skipped), ("expected failures", expected)) if v]
    if bad:
        print(f"FAILED ({', '.join(bad + extra)})", file=sys.stderr)
        return 1
    print("OK" + (f" ({', '.join(extra)})" if extra else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
