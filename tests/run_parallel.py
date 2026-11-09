#!/usr/bin/env python3
"""Run the test suite in parallel worker processes, stdlib only.

    python3 tests/run_parallel.py [-j N] [-v] [--timeout SECONDS] [name ...]

Discovers the same tests as `python3 -m unittest discover -s tests` and splits them into
units of one test class each (a large class in parts, see MAX_UNIT). Each unit runs in a
fresh interpreter (default: as many at the same time as there are CPUs), so setUpModule
and setUpClass run once per unit, and a unit that crashes or hangs (--timeout) is reported
as an error without taking the others with it. The largest units start first. When the
runner ends early (Ctrl-C, SIGTERM, SIGKILL), its units end with it.

Prints one combined result in unittest's format: the failures and errors with their
tracebacks, the output of every unit that failed, the number of tests run, OK or FAILED.
Exits 0 when everything passed, 1 on any failure or error, 2 for a name that matches no
test, 5 when no test ran.

name: a module, class or test (test_vms, test_vms.CreateTest, test_vms.CreateTest.test_x)
to run only those.
"""
import argparse
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
# A unit is one test class; a larger class is split into parts of at most MAX_UNIT tests,
# so that one slow class (test_vms.DeleteTest: about 50 s in one piece) does not decide the
# wall time alone. On titan (24 CPUs) parts of 4 took 36 s, of 8 took 42 s, whole classes
# 56 s. setUpModule and setUpClass run once per part.
MAX_UNIT = 4
SEPARATOR1 = "=" * 70
SEPARATOR2 = "-" * 70


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def loadable_name(test):
    """What a worker loads: the test id; for a module that does not import, the module
    (loading it again reports the import error as unittest does)."""
    if isinstance(test, unittest.loader._FailedTest):
        return test._testMethodName
    return test.id()


def discover_units(names):
    """Test names grouped by module and class, a class of more than MAX_UNIT tests in
    equal parts, the largest units first; and the names that match no test."""
    classes, unmatched = {}, set(names)
    for test in flatten(unittest.TestLoader().discover(TESTS, top_level_dir=TESTS)):
        name = loadable_name(test)
        if names:
            # a module that does not import also counts for the names of its classes and tests
            failed_import = isinstance(test, unittest.loader._FailedTest)
            matches = {n for n in names if name == n or name.startswith(n + ".")
                       or failed_import and n.startswith(name + ".")}
            if not matches:
                continue
            unmatched -= matches
        cls = name.rsplit(".", 1)[0] if name.count(".") >= 2 else name
        classes.setdefault(cls, []).append(name)
    units = []
    for cls, tests in classes.items():
        parts = -(-len(tests) // MAX_UNIT)
        for i in range(parts):
            units.append((cls if parts == 1 else f"{cls} [{i + 1}/{parts}]", tests[i::parts]))
    return sorted(units, key=lambda u: (-len(u[1]), u[0])), sorted(unmatched)


# --- worker: one unit in its own interpreter --------------------------------------------

def describe(test):
    """As unittest's text runner names a test in its error list."""
    doc = test.shortDescription()
    return f"{test}\n{doc}" if doc else str(test)


def end_with_runner(runner_pid):
    """However the runner ends (Ctrl-C, SIGTERM, SIGKILL), the unit ends with it: the
    worker leads its own process group and takes the group along."""
    while os.getppid() == runner_pid:
        time.sleep(1)
    os.killpg(0, signal.SIGKILL)


def worker(runner_pid, result_path, names):
    threading.Thread(target=end_with_runner, args=(runner_pid,), daemon=True).start()
    result = unittest.TestResult()
    unittest.TestLoader().loadTestsFromNames(names).run(result)
    with open(result_path, "w") as f:
        json.dump({"run": result.testsRun,
                   "failures": [(describe(t), tb) for t, tb in result.failures],
                   "errors": [(describe(t), tb) for t, tb in result.errors],
                   "unexpected_successes": [describe(t) for t in result.unexpectedSuccesses],
                   "skipped": len(result.skipped),
                   "expected_failures": len(result.expectedFailures)}, f)


# --- parent: schedule the units, combine the results ------------------------------------

def kill(p):
    """The worker with its process group (the CLI and fakes a test started)."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Runner:
    def __init__(self, workdir, timeout):
        self.workdir = workdir
        self.timeout = timeout
        self.running = set()
        self.lock = threading.Lock()
        self.stopping = False

    def run_unit(self, index, unit, names):
        result_path = os.path.join(self.workdir, f"{index}.json")
        log_path = os.path.join(self.workdir, f"{index}.log")
        start = time.monotonic()
        with open(log_path, "w+") as log:
            with self.lock:
                if self.stopping:
                    return None
                p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", str(os.getpid()),
                                      result_path, *names],
                                     cwd=REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
                self.running.add(p)
            try:
                code = p.wait(timeout=self.timeout)
                problem = f"worker exited with code {code}"
            except subprocess.TimeoutExpired:
                kill(p)
                p.wait()
                code, problem = None, f"timed out after {self.timeout:g} s"
            with self.lock:
                self.running.discard(p)
            log.seek(0)
            output = log.read()
        try:
            with open(result_path) as f:
                result = json.load(f)
        except (OSError, ValueError):
            result = {"run": 0, "failures": [], "errors": [], "unexpected_successes": [], "skipped": 0,
                      "expected_failures": 0}
        if code != 0 and not result["errors"]:
            result["errors"] = [(f"{unit} ({problem})", output or "(no output)\n")]
            output = ""
        result.update(unit=unit, seconds=time.monotonic() - start, output=output)
        return result

    def stop(self):
        """No new units, and the running ones end with their whole process group."""
        with self.lock:
            self.stopping = True
            for p in self.running:
                kill(p)


def failed(result):
    return bool(result["failures"] or result["errors"] or result["unexpected_successes"])


# what unittest names a failing setUpModule, setUpClass, ...: in a class split into parts
# it fails once per part, unittest discover reports it once
FIXTURES = ("setUpModule (", "tearDownModule (", "setUpClass (", "tearDownClass (")


def report(results, seconds, jobs):
    out = sys.stdout
    errors, failures, unexpected = [], [], []
    for r in results:
        errors += [e for e in r["errors"] if not (e[0].startswith(FIXTURES) and e[0] in (d for d, _ in errors))]
        failures += r["failures"]
        unexpected += r["unexpected_successes"]
    for flavour, entries in (("ERROR", errors), ("FAIL", failures)):
        for description, tb in entries:
            out.write(f"{SEPARATOR1}\n{flavour}: {description}\n{SEPARATOR2}\n{tb}\n")
    for description in unexpected:
        out.write(f"{SEPARATOR1}\nUNEXPECTED SUCCESS: {description}\n")
    for r in results:
        if failed(r) and r["output"].strip():
            out.write(f"{SEPARATOR1}\nOutput of {r['unit']}:\n{SEPARATOR2}\n{r['output'].rstrip()}\n")
    run = sum(r["run"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    expected = sum(r["expected_failures"] for r in results)
    out.write(f"{SEPARATOR2}\nRan {run} test{'' if run == 1 else 's'} in {seconds:.3f}s "
              f"({len(results)} units, {jobs} workers)\n\n")
    infos = [f"{name}={n}" for name, n in (("failures", len(failures)), ("errors", len(errors)),
                                           ("skipped", skipped), ("expected failures", expected),
                                           ("unexpected successes", len(unexpected))) if n]
    ok = not (errors or failures or unexpected)
    if run == 0 and ok and not skipped:
        out.write("NO TESTS RAN\n")
        return 5
    out.write(("OK" if ok else "FAILED") + (f" ({', '.join(infos)})" if infos else "") + "\n")
    return 0 if ok else 1


def default_jobs():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    if sys.argv[1:2] == ["--worker"]:
        return worker(int(sys.argv[2]), sys.argv[3], sys.argv[4:])
    parser = argparse.ArgumentParser(description="Run the test suite in parallel worker processes.")
    parser.add_argument("-j", "--jobs", type=int, default=default_jobs(),
                        help="units at the same time (default: the number of CPUs, %(default)s here)")
    parser.add_argument("-v", "--verbose", action="store_true", help="one line per unit with its time")
    parser.add_argument("--timeout", type=float, default=300, metavar="SECONDS",
                        help="a unit still running after this long is ended and reported as an error "
                             "(default: %(default)g)")
    parser.add_argument("names", nargs="*", help="only these modules, classes or tests")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    start = time.monotonic()
    units, unmatched = discover_units(args.names)
    if unmatched:
        parser.error("no test matches " + ", ".join(unmatched))
    jobs = min(args.jobs, len(units)) or 1
    results = []
    # SIGTERM and SIGHUP (an agent's time limit, a closed terminal) end the run like Ctrl-C
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    with tempfile.TemporaryDirectory(prefix="agentbox-tests-") as workdir:
        runner = Runner(workdir, args.timeout)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
        try:
            futures = [pool.submit(runner.run_unit, i, unit, names) for i, (unit, names) in enumerate(units)]
            for future in concurrent.futures.as_completed(futures):
                r = future.result()
                results.append(r)
                status = "FAILED" if failed(r) else "ok"
                if args.verbose:
                    print(f"{r['unit']} ({r['run']} tests) ... {status} {r['seconds']:.1f}s", flush=True)
                else:
                    print("." if status == "ok" else "F", end="", flush=True)
        except KeyboardInterrupt:
            print("\nInterrupted", file=sys.stderr)
            return 130
        finally:
            runner.stop()
            pool.shutdown(cancel_futures=True)
    if not args.verbose:
        print()
    results.sort(key=lambda r: r["unit"])
    return report(results, time.monotonic() - start, jobs)


if __name__ == "__main__":
    sys.exit(main())
