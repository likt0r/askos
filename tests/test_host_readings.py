"""Control Room backend (`web/agentbox-web`): host readings (GET /api/host) and the live
event stream (GET /api/events), against fake kernel interface files and a fake
`nvidia-smi` (fake_nvidia_smi.py).

The real server runs on 127.0.0.1 with a throwaway certificate and password. /proc and
/sys are temporary directories (AGENTBOX_WEB_PROC, AGENTBOX_WEB_SYS) laid out like the
Host titan (Ryzen 9 3900X with k10temp, two Samsung NVMe drives, a TITAN RTX); PATH
holds only the fake nvidia-smi. The tests look at HTTP responses, the events on the
stream, the arguments nvidia-smi received and the server's stderr.

Run from the repo root: python3 -m unittest discover -s tests
"""
import http.client
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import served_port  # noqa: E402
from fake_journald import Journald  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "web", "agentbox-web")
STATIC = os.path.join(ROOT, "web", "static")
FAKE_AGENTBOX = os.path.join(ROOT, "tests", "fake_agentbox.py")
FAKE_NVIDIA_SMI = os.path.join(ROOT, "tests", "fake_nvidia_smi.py")
PASSWORD = "correct horse battery staple"
MIB = 1024 * 1024
MAX_STREAMS = 8

conf = None  # TemporaryDirectory with cert.pem, key.pem, password


def setUpModule():
    global conf
    conf = tempfile.TemporaryDirectory()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-days", "1", "-subj", "/CN=t",
                    "-keyout", os.path.join(conf.name, "key.pem"), "-out", os.path.join(conf.name, "cert.pem")],
                   check=True, capture_output=True)
    r = subprocess.run([sys.executable, "-I", SERVER, "--hash-password"], input=PASSWORD + "\n",
                       capture_output=True, text=True, check=True)
    with open(os.path.join(conf.name, "password"), "w") as f:
        f.write(r.stdout)


def tearDownModule():
    conf.cleanup()


def stat(user, system, idle, iowait, cpus=4):
    line = f"cpu  {user} 0 {system} {idle} {iowait} 0 0 0 0 0\n"
    return line + "".join(f"cpu{i} 1 0 1 1 0 0 0 0 0 0\n" for i in range(cpus)) + "intr 1\nctxt 2\n"


def meminfo(total, available, swap_total=8388604, swap_free=8388604):
    return (f"MemTotal:       {total} kB\nMemFree:        1000 kB\nMemAvailable:   {available} kB\n"
            f"Buffers:            4016 kB\nCached:         34303160 kB\n"
            f"SwapTotal:      {swap_total} kB\nSwapFree:       {swap_free} kB\n")


def vmstat(pswpin=0, pswpout=0):
    return f"nr_free_pages 1\npswpin {pswpin}\npswpout {pswpout}\npgfault 12\n"


def qemu(domain, rss_kb, comm="qemu-system-x86"):
    """Files of a QEMU process as libvirt starts it."""
    argv = ["/usr/bin/qemu-system-x86_64", "-name", f"guest={domain},debug-threads=on", "-S", "-m",
            "size=4194304k"]
    return {"comm": comm + "\n", "cmdline": "\0".join(argv) + "\0",
            "status": f"Name:\t{comm}\nUmask:\t0022\nState:\tS (sleeping)\nVmPeak:\t 9999999 kB\n"
                      f"VmRSS:\t {rss_kb} kB\nThreads:\t40\n"}


# The Host titan as the kernel shows it (values from titan, rounded to avoid ties)
TITAN = {
    "proc/cpuinfo": "processor\t: 0\nvendor_id\t: AuthenticAMD\nmodel name\t: AMD Ryzen 9 3900X 12-Core Processor\n",
    "proc/stat": stat(1000, 500, 8000, 500),
    "proc/vmstat": vmstat(100, 200),
    "proc/loadavg": "0.23 0.18 0.24 1/1588 230048\n",
    "proc/meminfo": meminfo(65732440, 47776160),
    **{f"proc/12098/{k}": v for k, v in qemu("agentbox-priv", 2942832).items()},
    **{f"proc/12500/{k}": v for k, v in qemu("agentbox-research-lab", 1048576).items()},
    **{f"proc/13000/{k}": v for k, v in qemu("windows11", 8388608).items()},              # not ours
    **{f"proc/14000/{k}": v for k, v in qemu("agentbox-fake", 1, comm="bash").items()},  # not QEMU
    "proc/1/comm": "systemd\n",
    "proc/1/cmdline": "/usr/lib/systemd/systemd\0",
    "sys/class/hwmon/hwmon0/name": "nvme\n",
    "sys/class/hwmon/hwmon0/temp1_label": "Composite\n",
    "sys/class/hwmon/hwmon0/temp1_input": "41000\n",
    "sys/class/hwmon/hwmon0/temp2_label": "Sensor 1\n",
    "sys/class/hwmon/hwmon0/temp2_input": "45000\n",
    "sys/devices/nvme0/model": "Samsung SSD 970 EVO Plus 2TB            \n",
    "sys/class/hwmon/hwmon1/name": "nvme\n",
    "sys/class/hwmon/hwmon1/temp1_label": "Composite\n",
    "sys/class/hwmon/hwmon1/temp1_input": "30000\n",
    "sys/devices/nvme1/model": "Samsung SSD 970 EVO 1TB                 \n",
    "sys/class/hwmon/hwmon4/name": "k10temp\n",
    "sys/class/hwmon/hwmon4/temp1_label": "Tctl\n",
    "sys/class/hwmon/hwmon4/temp1_input": "44500\n",
    "sys/class/hwmon/hwmon4/temp3_label": "Tccd1\n",
    "sys/class/hwmon/hwmon4/temp3_input": "47000\n",
    "sys/class/hwmon/hwmon7/name": "iwlwifi_1\n",
    "sys/class/hwmon/hwmon7/temp1_input": "20000\n",
    "sys/class/powercap/intel-rapl:0/name": "package-0\n",
    "sys/class/powercap/intel-rapl:0/energy_uj": "1000000\n",
    "sys/class/powercap/intel-rapl:0/max_energy_range_uj": "65532610987\n",
    "sys/class/powercap/intel-rapl:0:0/name": "core\n",
    "sys/class/powercap/intel-rapl:0:0/energy_uj": "500000\n",
}
DEVICE_LINKS = {"sys/class/hwmon/hwmon0/device": "sys/devices/nvme0",
                "sys/class/hwmon/hwmon1/device": "sys/devices/nvme1"}
# energy_uj is root-only on titan
UNREADABLE = ("sys/class/powercap/intel-rapl:0/energy_uj", "sys/class/powercap/intel-rapl:0:0/energy_uj")


class Stream:
    """A client of GET /api/events that reads the stream block by block."""

    def __init__(self, test, last_id=None, cookie=None):
        self.conn = http.client.HTTPSConnection("127.0.0.1", test.port, context=test.ssl, timeout=10)
        headers = {"Cookie": cookie or test.cookie}
        if last_id:
            headers["Last-Event-ID"] = last_id
        self.conn.request("GET", "/api/events", headers=headers)
        self.resp = self.conn.getresponse()
        self.status = self.resp.status

    def block(self):
        """Next event or comment as {field: value}; EOFError when the server ends the stream."""
        block = {}
        while True:
            line = self.resp.readline()
            if not line:
                raise EOFError
            line = line.decode().rstrip("\n")
            if not line:
                if block:
                    return block
                continue
            if line.startswith(":"):
                block["comment"] = line[1:].strip()
                continue
            field, _, value = line.partition(":")
            block[field] = value[1:] if value.startswith(" ") else value

    def event(self, name, limit=100):
        for _ in range(limit):
            b = self.block()
            if b.get("event") == name:
                return b
        raise AssertionError(f"no {name} event")

    def close(self):
        self.resp.close()
        self.conn.close()


class HostTest(unittest.TestCase):
    ENV = {}                    # extra server environment of a test class
    NVIDIA_SMI = True           # fake nvidia-smi on PATH

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self.remove_dir)
        self.addCleanup(self.make_readable)
        for path, content in TITAN.items():
            self.write(path, content)
        for link, target in DEVICE_LINKS.items():
            os.symlink(os.path.join(self.dir, target), os.path.join(self.dir, link))
        for path in UNREADABLE:
            os.chmod(os.path.join(self.dir, path), 0)
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        if self.NVIDIA_SMI:
            with open(os.path.join(bindir, "nvidia-smi"), "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_NVIDIA_SMI}" "$@"\n')
            os.chmod(os.path.join(bindir, "nvidia-smi"), 0o755)
        self.stderr_path = os.path.join(self.dir, "server.err")
        # sign-ins go to a stand-in, never into the Host's Activity
        journald = Journald(os.path.join(self.dir, "journal", "socket"))
        self.addCleanup(journald.close)
        env = dict(os.environ, PATH=bindir,
                   AGENTBOX_WEB_CONF=conf.name, AGENTBOX_WEB_STATIC=STATIC,
                   AGENTBOX_WEB_BIND="127.0.0.1", AGENTBOX_WEB_PORT="0", AGENTBOX_WEB_JOURNAL=journald.path,
                   AGENTBOX_WEB_COMMAND=f"{sys.executable} -I {FAKE_AGENTBOX}", FAKE_AGENTBOX_DIR=self.dir,
                   FAKE_NVIDIA_SMI_DIR=self.dir,
                   AGENTBOX_WEB_PROC=os.path.join(self.dir, "proc"), AGENTBOX_WEB_SYS=os.path.join(self.dir, "sys"),
                   AGENTBOX_WEB_SAMPLE_SECONDS="0.2", AGENTBOX_WEB_HEARTBEAT_SECONDS="0.5")
        env.update(self.ENV)
        # own process group: stopping the server also stops a running fake nvidia-smi
        with open(self.stderr_path, "w") as err:
            self.server = subprocess.Popen([sys.executable, "-I", SERVER], env=env, start_new_session=True,
                                           stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self.stop_server)
        self.ssl = ssl._create_unverified_context()
        self.wait_for_server()
        self.cookie = None

    def make_readable(self):
        for path in UNREADABLE:
            try:
                os.chmod(os.path.join(self.dir, path), 0o644)
            except OSError:
                pass

    def stop_server(self):
        if self.server.poll() is None:
            os.killpg(self.server.pid, signal.SIGTERM)
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.server.pid, signal.SIGKILL)
                self.server.wait()

    def remove_dir(self):
        # a fake nvidia-smi that was just stopped may still finish writing its record
        for _ in range(20):
            try:
                shutil.rmtree(self.dir)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.05)
        shutil.rmtree(self.dir)

    def wait_for_server(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.port = served_port.port(self.server_log())
            if self.port:
                return
            if self.server.poll() is not None:
                self.fail("server exited early: " + self.server_log())
            time.sleep(0.05)
        self.fail("server did not start: " + self.server_log())

    def server_log(self):
        with open(self.stderr_path) as f:
            return f.read()

    # --- fake kernel and nvidia-smi ------------------------------------------

    def write(self, path, content):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full + ".new", "w") as f:
            f.write(content)
        os.replace(full + ".new", full)

    def remove(self, path):
        full = os.path.join(self.dir, path)
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)

    def nvidia_smi(self, stdout=None, exitcode=0):
        r = {"exitcode": exitcode}
        if stdout is not None:
            r["stdout"] = stdout
        self.write("nvidia-smi.json", json.dumps(r))

    def nvidia_smi_calls(self):
        try:
            with open(os.path.join(self.dir, "nvidia-smi.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    # --- HTTP ----------------------------------------------------------------

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ssl, timeout=30)
        headers = {"Content-Type": "application/json", "X-Agentbox": "1"}
        if cookie or self.cookie:
            headers["Cookie"] = cookie or self.cookie
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read()), resp
        finally:
            conn.close()

    def login(self):
        status, _, resp = self.request("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.cookie = resp.getheader("Set-Cookie").split(";")[0]

    def host(self):
        status, obj, _ = self.request("GET", "/api/host")
        self.assertEqual(status, 200)
        return obj

    def until(self, check, timeout=10):
        """GET /api/host until check(answer) holds; the answer."""
        deadline = time.monotonic() + timeout
        while True:
            obj = self.host()
            try:
                if check(obj):
                    return obj
            except KeyError:        # no sample yet, or not this reading
                pass
            if time.monotonic() > deadline:
                self.fail(f"condition not met, last answer: {json.dumps(obj)[:2000]}")
            time.sleep(0.05)

    def sampled(self):
        return self.until(lambda h: h["readings"])

    @staticmethod
    def by_id(answer):
        return {r["id"]: r for r in answer["readings"]}

    def reading(self, rid, check=lambda r: True):
        """The reading once it exists and check(reading) holds."""
        answer = self.until(lambda h: rid in self.by_id(h) and check(self.by_id(h)[rid]))
        return self.by_id(answer)[rid]


class ReadingsTest(HostTest):
    """#16: readings come from the kernel's files and nvidia-smi, with their source."""

    def setUp(self):
        super().setUp()
        self.login()

    def test_readings_of_the_host(self):
        r = self.by_id(self.sampled())
        cpu = "AMD Ryzen 9 3900X 12-Core Processor"
        expected = {
            "cpu.temp": ("cpu_temp", "°C", 44.5, cpu, "k10temp/Tctl"),
            "memory": ("memory", "%", 27.3, None, "/proc/meminfo"),
            "swap": ("swap", "%", 0.0, None, "/proc/meminfo"),
            "gpu0.temp": ("gpu_temp", "°C", 30.0, "NVIDIA TITAN RTX", "nvidia-smi"),
            "gpu0.load": ("gpu_load", "%", 5.0, "NVIDIA TITAN RTX", "nvidia-smi"),
            "gpu0.memory": ("gpu_memory", "%", 0.2, "NVIDIA TITAN RTX", "nvidia-smi"),
            "gpu0.power": ("gpu_power", "W", 21.9, "NVIDIA TITAN RTX", "nvidia-smi"),
            "nvme0.temp": ("nvme_temp", "°C", 41.0, "Samsung SSD 970 EVO Plus 2TB", "nvme0/Composite"),
            "nvme1.temp": ("nvme_temp", "°C", 30.0, "Samsung SSD 970 EVO 1TB", "nvme1/Composite"),
        }
        for rid, (kind, unit, value, device, source) in expected.items():
            with self.subTest(reading=rid):
                self.assertEqual((r[rid]["kind"], r[rid]["unit"], r[rid]["value"], r[rid]["device"],
                                  r[rid]["source"]), (kind, unit, value, device, source))
                self.assertNotIn("unavailable", r[rid])
        self.assertEqual((r["memory"]["used_bytes"], r["memory"]["total_bytes"]),
                         ((65732440 - 47776160) * 1024, 65732440 * 1024))
        self.assertEqual((r["gpu0.memory"]["used_bytes"], r["gpu0.memory"]["total_bytes"]), (49 * MIB, 24576 * MIB))
        self.assertEqual(r["gpu0.power"]["limit_watts"], 280.0)
        self.assertEqual(r["cpu.load"]["loadavg"], [0.23, 0.18, 0.24])
        self.assertEqual(r["cpu.load"]["cpus"], 4)
        # the Wi-Fi card's sensor and Tccd1 are not readings of their own
        self.assertEqual(sorted(r), sorted([*expected, "cpu.load", "cpu.power", "swap_activity"]))

    def test_memory_per_running_vm_from_its_qemu_process(self):
        self.assertEqual(self.sampled()["vms"], {"priv": {"memory_bytes": 2942832 * 1024},
                                                 "research-lab": {"memory_bytes": 1048576 * 1024}})
        # a paused VM has no QEMU process and drops out
        self.remove("proc/12500")
        answer = self.until(lambda h: "research-lab" not in h["vms"])
        self.assertEqual(list(answer["vms"]), ["priv"])

    def test_nvidia_smi_is_asked_for_csv_without_units(self):
        self.sampled()
        self.assertEqual(self.nvidia_smi_calls()[0], [
            "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit",
            "--format=csv,noheader,nounits"])

    def test_several_gpus(self):
        self.nvidia_smi("0, NVIDIA TITAN RTX, 30, 5, 49, 24576, 21.93, 280.00\n"
                        "1, NVIDIA GeForce RTX 3090, 61, 99, 20000, 24576, 340.5, 350.00\n")
        r = self.by_id(self.until(lambda h: "gpu1.temp" in self.by_id(h)))
        self.assertEqual((r["gpu1.temp"]["value"], r["gpu1.load"]["value"], r["gpu1.power"]["value"]),
                         (61.0, 99.0, 340.5))
        self.assertEqual(r["gpu1.temp"]["device"], "NVIDIA GeForce RTX 3090")
        self.assertEqual(r["gpu0.temp"]["value"], 30.0)

    def test_cpu_load_is_the_busy_share_between_two_samples(self):
        # nothing has changed since the server started: no load to compute yet
        first = self.by_id(self.sampled())["cpu.load"]
        self.assertIsNone(first["value"])
        self.assertEqual(first["unavailable"], "pending")
        # 300 of 1000 ticks busy (user, system), 700 idle (idle, iowait)
        self.write("proc/stat", stat(1200, 600, 8600, 600))
        self.assertEqual(self.reading("cpu.load", lambda r: r["value"] is not None)["value"], 30.0)

    def test_swap_activity_is_the_rate_of_pages_between_two_samples(self):
        # the first sample has nothing to compare with: pending, not 0
        first = self.sampled()
        first = self.by_id(first)["swap_activity"]
        self.assertIsNone(first["value"])
        self.assertEqual(first["unavailable"], "pending")
        self.assertEqual((first["unit"], first["source"]), ("MiB/s", "/proc/vmstat"))
        # nothing moved between two samples
        self.assertEqual(self.reading("swap_activity", lambda r: r["value"] is not None)["value"], 0.0)
        # 1000 pages in and 3000 out since the last sample
        self.write("proc/vmstat", vmstat(1100, 3200))
        r = self.reading("swap_activity", lambda r: r["value"])
        self.assertAlmostEqual(r["out_bytes_per_second"] / r["in_bytes_per_second"], 3, places=2)
        self.assertAlmostEqual(r["value"] * MIB, r["in_bytes_per_second"] + r["out_bytes_per_second"],
                               delta=0.01 * MIB)
        # it was a burst: the next sample shows the rate falling again
        self.assertEqual(self.reading("swap_activity", lambda r: r["value"] == 0.0)["value"], 0.0)

    def test_swap_activity_without_vmstat_or_counters_is_unavailable(self):
        self.remove("proc/vmstat")
        r = self.reading("swap_activity", lambda r: r["value"] is None and r["unavailable"] != "pending")
        self.assertEqual(r["unavailable"], "no-sensor")
        self.assertFalse(r["over"])
        self.write("proc/vmstat", "nr_free_pages 1\npgfault 12\n")
        r = self.reading("swap_activity", lambda r: r["unavailable"] != "no-sensor")
        self.assertEqual(r["unavailable"], "failed")
        # readable again: pending once, then a rate
        self.write("proc/vmstat", vmstat(5, 5))
        self.assertEqual(self.reading("swap_activity", lambda r: r["value"] is not None)["value"], 0.0)

    def test_swap_activity_counters_that_restart_do_not_give_a_negative_rate(self):
        self.reading("swap_activity", lambda r: r["value"] is not None)
        self.write("proc/vmstat", vmstat(1, 1))                    # below the last sample
        self.reading("swap_activity", lambda r: r["value"] is None)
        self.assertEqual(self.reading("swap_activity", lambda r: r["value"] is not None)["value"], 0.0)

    def test_cpu_power_from_the_package_energy_counter(self):
        self.make_readable()
        stream = Stream(self)
        self.addCleanup(stream.close)

        def power():
            return self.by_id(json.loads(stream.event("host")["data"]))["cpu.power"]

        # readable now: the counter has not moved between two samples
        for _ in range(50):
            if power()["value"] == 0.0:
                break
        else:
            self.fail("cpu.power never became available")
        # 2 J more; the next sample shows the power that took
        self.write("sys/class/powercap/intel-rapl:0/energy_uj", "3000000\n")
        for _ in range(50):
            p = power()
            if p["value"]:
                break
        self.assertGreater(p["value"], 0)
        self.assertEqual((p["unit"], p["source"]), ("W", "powercap/intel-rapl"))

    def test_device_names_are_printable_text(self):
        self.write("sys/devices/nvme0/model", "Evil\x1b[31m\nSSD\x00" + "x" * 200)
        self.nvidia_smi('0, "NVIDIA\x07TITAN\x1b[2J", 30, 5, 49, 24576, 21.93, 280.00\n')
        r = self.by_id(self.until(lambda h: self.by_id(h)["gpu0.temp"]["device"] != "NVIDIA TITAN RTX"
                                  and self.by_id(h)["nvme0.temp"]["device"].startswith("Evil")))
        for device in (r["nvme0.temp"]["device"], r["gpu0.temp"]["device"]):
            self.assertTrue(device.isprintable(), device)
            self.assertLessEqual(len(device), 64)


class ThresholdTest(HostTest):
    """#16: every reading carries its threshold and whether it is over it."""

    def setUp(self):
        super().setUp()
        self.login()

    def test_default_thresholds(self):
        answer = self.sampled()
        self.assertEqual(answer["thresholds"], {
            "cpu_load": 90, "cpu_temp": 85, "cpu_power": None, "memory": 90, "swap": 50, "swap_activity": 10,
            "gpu_temp": 87, "gpu_load": 90, "gpu_memory": 90, "gpu_power": None, "nvme_temp": 70})
        for r in answer["readings"]:
            with self.subTest(reading=r["id"]):
                self.assertEqual(r["threshold"], answer["thresholds"][r["kind"]])
                self.assertFalse(r["over"])

    def test_readings_at_or_above_their_threshold_are_over(self):
        self.write("sys/class/hwmon/hwmon4/temp1_input", "85000\n")      # exactly at the threshold
        self.write("sys/class/hwmon/hwmon0/temp1_input", "69900\n")      # just below
        self.write("sys/class/hwmon/hwmon1/temp1_input", "74000\n")
        self.write("proc/meminfo", meminfo(65732440, 3286622, swap_total=8388604, swap_free=4000000))
        self.nvidia_smi("0, NVIDIA TITAN RTX, 88, 97, 23000, 24576, 279.5, 280.00\n")
        r = self.by_id(self.until(lambda h: self.by_id(h)["cpu.temp"]["value"] == 85.0
                                  and self.by_id(h)["gpu0.temp"]["value"] == 88.0
                                  and self.by_id(h)["memory"]["value"] > 90))
        over = {rid: x["over"] for rid, x in r.items()}
        self.assertEqual(over, {"cpu.load": False, "cpu.temp": True, "cpu.power": False, "memory": True,
                                "swap": True, "swap_activity": False, "gpu0.temp": True, "gpu0.load": True, "gpu0.memory": True,
                                "gpu0.power": False, "nvme0.temp": False, "nvme1.temp": True})

    def test_swap_activity_over_its_threshold_is_flagged(self):
        self.reading("swap_activity", lambda r: r["value"] is not None)
        # 100000 pages (about 390 MiB) within a sample of 0.2 s: far over 10 MiB/s
        self.write("proc/vmstat", vmstat(100100, 200))
        r = self.reading("swap_activity", lambda r: r["value"])
        self.assertEqual(r["threshold"], 10)
        self.assertTrue(r["over"])
        self.assertEqual(self.reading("swap_activity", lambda r: r["value"] == 0.0)["over"], False)

    def test_falling_below_clears_the_flag(self):
        self.write("sys/class/hwmon/hwmon4/temp1_input", "91000\n")
        self.assertTrue(self.reading("cpu.temp", lambda r: r["value"] == 91.0)["over"])
        self.write("sys/class/hwmon/hwmon4/temp1_input", "84900\n")
        self.assertFalse(self.reading("cpu.temp", lambda r: r["value"] == 84.9)["over"])


class UnavailableTest(HostTest):
    """#16: what cannot be read without privileges is reported as unavailable, never estimated."""

    def setUp(self):
        super().setUp()
        self.login()

    @unittest.skipIf(os.geteuid() == 0, "root reads files without permission")
    def test_root_only_energy_counter(self):
        power = self.by_id(self.sampled())["cpu.power"]
        self.assertIsNone(power["value"])
        self.assertEqual(power["unavailable"], "no-permission")
        self.assertFalse(power["over"])

    def test_no_powercap(self):
        self.remove("sys/class/powercap")
        self.assertEqual(self.reading("cpu.power", lambda r: r.get("unavailable") == "no-sensor")["value"], None)

    def test_no_cpu_sensor(self):
        self.remove("sys/class/hwmon/hwmon4")
        temp = self.reading("cpu.temp", lambda r: r["value"] is None)
        self.assertEqual(temp["unavailable"], "no-sensor")

    @unittest.skipIf(os.geteuid() == 0, "root reads files without permission")
    def test_unreadable_nvme_sensor(self):
        os.chmod(os.path.join(self.dir, "sys/class/hwmon/hwmon1/temp1_input"), 0)
        self.addCleanup(os.chmod, os.path.join(self.dir, "sys/class/hwmon/hwmon1/temp1_input"), 0o644)
        temp = self.reading("nvme1.temp", lambda r: r["value"] is None)
        self.assertEqual(temp["unavailable"], "no-permission")
        self.assertEqual(self.by_id(self.host())["nvme0.temp"]["value"], 41.0)

    def test_failing_nvidia_smi(self):
        self.sampled()
        self.nvidia_smi("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n", 9)
        r = self.by_id(self.until(lambda h: self.by_id(h)["gpu0.temp"]["value"] is None))
        for what in ("temp", "load", "memory", "power"):
            with self.subTest(what=what):
                self.assertIsNone(r[f"gpu0.{what}"]["value"])
                self.assertEqual(r[f"gpu0.{what}"]["unavailable"], "failed")
        self.assertNotIn("NVIDIA-SMI has failed", self.server_log())

    def test_values_nvidia_smi_cannot_report(self):
        self.nvidia_smi("0, NVIDIA TITAN RTX, 30, 5, 49, 24576, [N/A], [Not Supported]\n")
        r = self.by_id(self.until(lambda h: self.by_id(h)["gpu0.power"]["value"] is None))
        self.assertEqual(r["gpu0.power"]["unavailable"], "not-supported")
        self.assertIsNone(r["gpu0.power"]["limit_watts"])
        self.assertEqual(r["gpu0.temp"]["value"], 30.0)

    def test_garbage_from_nvidia_smi(self):
        self.nvidia_smi("0, NVIDIA TITAN RTX, warm, 5, 49, 24576, 21.93, 280.00\n")
        self.assertEqual(self.reading("gpu0.temp", lambda r: r["value"] is None)["unavailable"], "failed")


class WithoutNvidiaSmiTest(HostTest):
    """#16: a Host without NVIDIA driver has no GPU readings."""
    NVIDIA_SMI = False

    def test_no_gpu_readings(self):
        self.login()
        ids = set(self.by_id(self.sampled()))
        self.assertFalse([rid for rid in ids if rid.startswith("gpu")], ids)
        self.assertIn("cpu.temp", ids)


class HistoryTest(HostTest):
    """#16: the backend keeps a window of history (30 minutes on the Host)."""
    ENV = {"AGENTBOX_WEB_SAMPLE_SECONDS": "0.1", "AGENTBOX_WEB_HISTORY_SECONDS": "1"}

    def test_history_holds_the_window_and_no_more(self):
        self.login()
        answer = self.until(lambda h: h["history"] and h["time"] - h["history"][0]["time"] >= 0.9, timeout=15)
        time.sleep(1)
        answer = self.host()
        self.assertEqual((answer["interval"], answer["history_seconds"]), (0.1, 1))
        times = [e["time"] for e in answer["history"]]
        self.assertEqual(times, sorted(times))
        self.assertLessEqual(answer["time"] - times[0], 1.0)
        self.assertGreaterEqual(len(times), 5)
        self.assertLessEqual(len(times), 12)
        last = answer["history"][-1]
        self.assertEqual(last["time"], answer["time"])
        self.assertEqual(last["values"], {r["id"]: r["value"] for r in answer["readings"]})
        self.assertEqual(last["vms"], {"priv": 2942832 * 1024, "research-lab": 1048576 * 1024})

    def test_history_follows_the_readings(self):
        self.login()
        self.write("sys/class/hwmon/hwmon4/temp1_input", "60000\n")
        answer = self.until(lambda h: any(e["values"]["cpu.temp"] == 60.0 for e in h["history"]))
        temps = [e["values"]["cpu.temp"] for e in answer["history"]]
        self.assertEqual(temps[-1], 60.0)


class AccessTest(HostTest):
    """#16: readings and the live stream need a session, like every other API."""

    def test_host_requires_session(self):
        self.assertEqual(self.request("GET", "/api/host")[:2], (401, {"error": "not signed in"}))
        self.assertEqual(self.request("GET", "/api/host", cookie="agentbox=bogus")[0], 401)

    def test_events_require_session(self):
        for cookie in ("", "agentbox=bogus"):
            with self.subTest(cookie=cookie):
                stream = Stream(self, cookie=cookie or "x=y")
                self.addCleanup(stream.close)
                self.assertEqual(stream.status, 401)
                self.assertEqual(json.loads(stream.resp.read()), {"error": "not signed in"})

    def test_logout_ends_the_live_stream(self):
        self.login()
        stream = Stream(self)
        self.addCleanup(stream.close)
        stream.event("host")
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        with self.assertRaises(EOFError):
            for _ in range(100):
                stream.block()
        # and it cannot come back with the old cookie
        again = Stream(self)
        self.addCleanup(again.close)
        self.assertEqual(again.status, 401)


class EventsTest(HostTest):
    """#16 and #12: GET /api/events is the backend's Server-Sent Events stream."""

    def setUp(self):
        super().setUp()
        self.login()

    def open(self, last_id=None):
        stream = Stream(self, last_id=last_id)
        self.addCleanup(stream.close)
        return stream

    def test_stream_starts_with_hello_and_the_current_readings(self):
        self.sampled()
        stream = self.open()
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(stream.resp.getheader("Cache-Control"), "no-store")
        hello = stream.block()
        self.assertEqual(hello["event"], "hello")
        self.assertEqual(hello["retry"], "3000")
        self.assertNotIn("id", hello)
        data = json.loads(hello["data"])
        self.assertEqual(data["resumed"], False)
        self.assertRegex(data["stream"], r"^[0-9a-f]{8}$")
        first = stream.block()
        self.assertEqual(first["event"], "host")
        self.assertRegex(first["id"], "^" + data["stream"] + r"-\d+$")
        host = json.loads(first["data"])
        self.assertEqual(set(host), {"time", "readings", "vms"})
        self.assertEqual(self.by_id(host)["cpu.temp"]["value"], 44.5)
        self.assertEqual(host["vms"]["priv"], {"memory_bytes": 2942832 * 1024})

    def test_every_sample_is_an_event_with_the_next_id(self):
        stream = self.open()
        ids, times = [], []
        while len(ids) < 4:
            b = stream.event("host")
            ids.append(int(b["id"].rsplit("-", 1)[1]))
            times.append(json.loads(b["data"])["time"])
        self.assertEqual(ids, list(range(ids[0], ids[0] + 4)))
        self.assertEqual(times, sorted(times))

    def test_event_matches_the_readings_endpoint(self):
        self.write("sys/class/hwmon/hwmon4/temp1_input", "77000\n")
        stream = self.open()
        for _ in range(50):
            host = json.loads(stream.event("host")["data"])
            if self.by_id(host)["cpu.temp"]["value"] == 77.0:
                break
        r = self.by_id(host)["cpu.temp"]
        self.assertEqual((r["threshold"], r["over"], r["unit"]), (85, False, "°C"))
        answer = self.host()
        self.assertIn(host["time"], [e["time"] for e in answer["history"]])

    def test_reconnect_with_last_event_id_replays_what_was_missed(self):
        stream = self.open()
        seen = stream.event("host")["id"]
        stream.close()
        time.sleep(0.7)     # a few samples happen while disconnected
        again = self.open(last_id=seen)
        hello = again.block()
        self.assertEqual(json.loads(hello["data"])["resumed"], True)
        prefix, n = seen.rsplit("-", 1)
        replayed = [again.event("host")["id"] for _ in range(3)]
        self.assertEqual(replayed, [f"{prefix}-{int(n) + i}" for i in (1, 2, 3)])

    def test_unknown_last_event_id_gets_a_fresh_start(self):
        self.sampled()
        for last_id in ("0badc0de-3", "garbage", "-1", f"{'f' * 8}-999999"):
            with self.subTest(last_id=last_id):
                stream = self.open(last_id=last_id)
                self.assertEqual(json.loads(stream.block()["data"])["resumed"], False)
                self.assertEqual(stream.block()["event"], "host")
                stream.close()

    def test_id_from_the_future_of_this_stream_gets_a_fresh_start(self):
        stream = self.open()
        prefix = json.loads(stream.block()["data"])["stream"]
        stream.close()
        again = self.open(last_id=f"{prefix}-99999")
        self.assertEqual(json.loads(again.block()["data"])["resumed"], False)

    def test_streams_are_bounded_and_freed_when_clients_leave(self):
        streams = [self.open() for _ in range(MAX_STREAMS)]
        for s in streams:
            self.assertEqual(s.status, 200)
            s.event("hello")
        status, obj, resp = self.request("GET", "/api/events")
        self.assertEqual((status, obj), (503, {"error": "too many live connections"}))
        self.assertEqual(resp.getheader("Retry-After"), "10")
        # readings stay available meanwhile
        self.assertTrue(self.sampled()["readings"])
        # a client that goes away frees its place once the server notices
        streams[0].close()
        streams[1].resp.close()
        deadline = time.monotonic() + 15
        while True:
            s = Stream(self)
            self.addCleanup(s.close)
            if s.status == 200:
                break
            s.close()
            self.assertLess(time.monotonic(), deadline, "no place freed")
            time.sleep(0.2)
        self.assertEqual(s.block()["event"], "hello")
        self.assertNotIn("Traceback", self.server_log())


class HeartbeatTest(HostTest):
    """#12: an idle stream gets a comment line, so proxies and the browser keep it open."""
    ENV = {"AGENTBOX_WEB_SAMPLE_SECONDS": "3600", "AGENTBOX_WEB_HEARTBEAT_SECONDS": "0.2"}

    def test_heartbeat_while_nothing_happens(self):
        self.login()
        stream = Stream(self)
        self.addCleanup(stream.close)
        self.assertEqual(stream.block()["event"], "hello")
        # the first sample comes after a second, the next only after an hour
        blocks = []
        while not (any(b.get("event") == "host" for b in blocks) and blocks[-1].get("comment")):
            blocks.append(stream.block())
            self.assertLess(len(blocks), 50)
        # (the state a new connection gets first, such as version and watch, is no sample)
        self.assertIn({"comment": "ping"}, blocks[:blocks.index(next(b for b in blocks if b.get("event") == "host"))])
        self.assertEqual(blocks[-1], {"comment": "ping"})


if __name__ == "__main__":
    unittest.main()
