"""The Control Room UI build (web/ui/dist) served by the real backend (`web/agentbox-web`)
against the fake agentbox: the page, deep links and every file it loads, and that the
Content-Security-Policy covers everything index.html needs while staying at 'self'. Needs no
Node, only the build. web/ui/dist is not committed (ADR 0011): CI builds it in its `ui` job
and puts it there for these tests; in a VM, `npm run generate` in web/ui writes it. Without a
build the tests are skipped, unless AGENTBOX_REQUIRE_UI_BUILD=1 (set by CI), which turns a
missing build into a failure. The Vitest tests of the UI itself run in CI and in a VM
(web/ui, `npm test`).

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import hashlib
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_control_room_v1 import ROOT, V1Test, setUpModule, tearDownModule  # noqa: E402,F401

DIST = os.path.join(ROOT, "web", "ui", "dist")
BUILT = os.path.isfile(os.path.join(DIST, "index.html"))
REQUIRED = os.environ.get("AGENTBOX_REQUIRE_UI_BUILD") == "1"
NO_BUILD = "no UI build in web/ui/dist (not committed; `npm run generate` in web/ui writes it, in a VM or in CI)"
needs_build = unittest.skipUnless(BUILT, NO_BUILD)
IMMUTABLE = "public, max-age=31536000, immutable"
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
         ".svg": "image/svg+xml"}


def sha256(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def directives(csp):
    out = {}
    for part in csp.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


class BuildPresentTest(unittest.TestCase):
    def test_build_present_when_required(self):
        if not REQUIRED:
            self.skipTest("AGENTBOX_REQUIRE_UI_BUILD is not 1")
        self.assertTrue(BUILT, NO_BUILD + "; AGENTBOX_REQUIRE_UI_BUILD=1 requires it")


@needs_build
class DistTest(unittest.TestCase):
    def test_real_files_only(self):
        self.assertTrue(os.path.isfile(os.path.join(DIST, "index.html")))
        for dirpath, dirnames, filenames in os.walk(DIST):
            for name in dirnames + filenames:
                self.assertFalse(os.path.islink(os.path.join(dirpath, name)), os.path.join(dirpath, name))

    def test_nothing_from_another_origin(self):
        external = re.compile(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//|url\(\s*["']?(?:https?:)?//|@import""", re.I)
        for dirpath, _, filenames in os.walk(DIST):
            for name in filenames:
                if name.endswith((".html", ".css")):
                    with open(os.path.join(dirpath, name)) as f:
                        self.assertIsNone(external.search(f.read()), name)


@needs_build
class ServedUiTest(V1Test):
    UI_SOURCE = DIST

    def get(self, path, headers=None):
        return self.request("GET", path, header=False, raw=True, headers=headers)

    def index(self):
        with open(os.path.join(DIST, "index.html")) as f:
            return f.read()

    def assertServed(self, path):
        status, body, resp = self.get(path)
        self.assertEqual(status, 200, path)
        with open(os.path.join(DIST, path.lstrip("/")), "rb") as f:
            self.assertEqual(body, f.read(), path)
        ext = os.path.splitext(path)[1]
        if ext in TYPES:
            self.assertEqual(resp.getheader("Content-Type"), TYPES[ext], path)
        if path.startswith("/_nuxt/"):
            self.assertEqual(resp.getheader("Cache-Control"), IMMUTABLE, path)
        return body

    def test_page_and_its_csp(self):
        status, body, resp = self.get("/")
        self.assertEqual((status, body.decode()), (200, self.index()))
        csp = resp.getheader("Content-Security-Policy")
        self.assertNotIn("unsafe", csp)
        d = directives(csp)
        self.assertEqual(d["default-src"], ["'self'"])
        for name in ("font-src", "connect-src"):
            self.assertEqual(d[name], ["'self'"])
        # every inline script the browser runs, and every inline style, is allowed by its hash
        html = self.index()
        scripts = styles = 0
        for tag, attrs, text in re.findall(r"<(script|style)\b([^>]*)>(.*?)</\1>", html, re.S):
            if tag == "script":
                if re.search(r"\ssrc=", attrs):
                    continue
                kind = re.search(r"""\stype=["']?([^"'\s>]+)""", attrs)
                if kind and kind.group(1) not in ("module", "importmap", "text/javascript"):
                    continue
                self.assertIn(sha256(text), d["script-src"])
                scripts += 1
            else:
                self.assertIn(sha256(text), d["style-src"])
                styles += 1
        self.assertGreater(scripts, 0)
        self.assertEqual(len(d["script-src"]), scripts + 1)
        self.assertEqual(len(d["style-src"]), styles + 1)
        # no style attributes in the HTML (the CSP would block them)
        self.assertNotRegex(html, r"\sstyle\s*=")

    def test_deep_links_get_the_page(self):
        for path in ("/vms", "/vms/work", "/vms/work/log", "/sign-in?redirect=%2Fsettings", "/settings"):
            with self.subTest(path=path):
                status, body, resp = self.get(path)
                self.assertEqual((status, body.decode()), (200, self.index()))
                self.assertEqual(resp.getheader("Cache-Control"), "no-cache")

    def test_every_file_the_page_loads(self):
        html = self.index()
        local = re.findall(r"""(?:src|href)="(/[^"]+)\"""", html)
        self.assertTrue(any(p.endswith(".js") for p in local))
        self.assertTrue(any(p.endswith(".css") for p in local))
        seen, todo = set(), list(local)
        importmap = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
        if importmap:
            todo += re.findall(r'"(/_nuxt/[^"]+)"', importmap.group(1))
        # follow the chunks' imports and the stylesheets' fonts
        while todo:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            body = self.assertServed(path)
            base = os.path.dirname(path)
            if path.endswith(".js"):
                todo += [base + "/" + p for p in re.findall(r"""["'`]\./([\w.-]+\.(?:js|css))["'`]""", body.decode())]
            elif path.endswith(".css"):
                todo += [base + "/" + p for p in re.findall(r"url\(\./([\w.-]+)\)", body.decode())]
        self.assertTrue(any(p.endswith(".woff2") for p in seen), "fonts are self-hosted")

    def test_missing_asset_is_404(self):
        self.assertEqual(self.get("/_nuxt/gone.js")[0], 404)


if __name__ == "__main__":
    unittest.main()
