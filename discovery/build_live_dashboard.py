#!/usr/bin/env python3
"""Build live-dashboard.py: the live view as one Python file, for a laptop or jump host with kubectl.

Nothing is installed in the cluster: the file holds live.py (watches, metrics-server polling), textreport.py, the
parts of discover.py they use, both pages (static/live.html, static/text.html) and a small HTTP server for
/, /live, /text and /api/live/*. Read-only, standard library only, so one `scp` moves it into an air-gapped site.

    python3 build_live_dashboard.py [OUT]          (default: build/live-dashboard.py)

The release workflow runs this and attaches the result to the GitHub Release. The modules are copied, not
rewritten: `discover.X` / `live.X` / `textreport.X` keep working because the built file binds those three names to
itself. The build fails on a top-level name that two modules both define.
"""

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# What live.py / textreport.py and the server below take from discover.py; their dependencies are followed.
DISCOVER_SEEDS = ["RANCHER_PROJECT_ANNOTATION", "DEFAULT_SYSTEM_NS_REGEX", "DEFAULT_SYSTEM_PROJECT_REGEX",
                  "KubectlError", "kubectl", "kget", "log", "first_line", "parse_quantity", "_res", "pod_effective",
                  "long_running_containers", "classify", "context_label", "load_rancher_projects",
                  "load_names_configmap"]

SERVER = r'''
# ---------------------------------------------------------------------------
# Server: the live routes of server.py, nothing else
# ---------------------------------------------------------------------------

PAGE_CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:"
LIVE_DEBOUNCE = 2.0    # seconds a change waits on the SSE stream for the rest of its burst
LIVE_PING = 15         # seconds between SSE events while nothing changes (keeps proxies from closing the stream)
NAMES_REFRESH = 1800   # seconds between reads of the Rancher Project names
PAGES = {"/": LIVE_HTML, "/live": LIVE_HTML, "/text": TEXT_HTML}


def make_handler(live_state):
    def since_of(query):
        try:
            return int(query)
        except (TypeError, ValueError):
            return -1

    class Handler(BaseHTTPRequestHandler):
        server_version = "cluster-resource-report-live"

        def log_message(self, fmt, *a):
            if not self.path.startswith("/healthz"):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

        def _send(self, code, body, ctype, extra=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if path in PAGES:
                self._send(200, PAGES[path], "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP})
            elif path == "/healthz":
                self._send(200, "ok", "text/plain")
            elif path == "/api/live/snapshot":
                body, gz = live_state.snapshot_json()
                headers = {"Vary": "Accept-Encoding"}
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    body, headers["Content-Encoding"] = gz, "gzip"
                self._send(200, body, "application/json", headers)
            elif path == "/api/live/changes":
                self._json(200, live_state.changes(since_of((query.get("since") or [None])[0])))
            elif path == "/api/live/stream":
                self._live_stream(since_of((query.get("since") or [self.headers.get("Last-Event-ID")])[0]))
            elif path == "/api/live/text":
                first = lambda k, d=None: (query.get(k) or [d])[0]  # noqa: E731
                try:
                    top = int(first("top", DEFAULT_TOP))
                except ValueError:
                    return self._send(400, "top must be a number\n", "text/plain; charset=utf-8")
                try:
                    text = render(live_state.snapshot(), first("report", "nodes"), first("ns"), first("sort", "mem"), top)
                except ReportError as e:
                    return self._send(400, f"{e}\n", "text/plain; charset=utf-8")
                self._send(200, text, "text/plain; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def _live_stream(self, since):
            """Server-Sent Events: the changes after `since` (event: changes, id: the new seq), a ping while nothing
            changes; a client that fell behind the change log gets {"reset": true} and reloads the snapshot."""
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"),
                         ("X-Accel-Buffering", "no"), ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(b"retry: 5000\n\n")
                self.wfile.flush()
                while True:
                    seq = live_state.wait(since, LIVE_PING)
                    if seq <= since:
                        self.wfile.write(f"event: ping\ndata: {json.dumps({'seq': seq})}\n\n".encode())
                        self.wfile.flush()
                        continue
                    time.sleep(LIVE_DEBOUNCE)
                    delta = live_state.changes(since)
                    self.wfile.write(f"id: {delta['seq']}\nevent: changes\ndata: {json.dumps(delta)}\n\n".encode())
                    self.wfile.flush()
                    since = delta["seq"]
            except (BrokenPipeError, ConnectionResetError, OSError):
                return   # the page went away

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def names_loop(args, live_state):
    """Rancher Project display names, read every NAMES_REFRESH seconds (read-only): from the Rancher local cluster
    (--rancher-local-context / --rancher-local-kubeconfig / --rancher-local-self) or the ConfigMap published by
    publish_rancher_names.py (--rancher-names-configmap). Without any of them, the page shows project ids."""
    while True:
        try:
            if args.rancher_names_configmap:
                projects, _ = load_names_configmap(args.context, args.rancher_names_configmap)
            else:
                ctx = args.context if args.rancher_local_self else args.rancher_local_context
                kubeconfig = args.rancher_local_kubeconfig or (args.kubeconfig if args.rancher_local_self else None)
                projects, _ = load_rancher_projects(ctx, kubeconfig)
            live_state.set_names(projects)
        except (KubectlError, ValueError, KeyError) as e:
            log(f"project names: {first_line(e)}")
        time.sleep(NAMES_REFRESH)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", help="kubectl context of the cluster (default: the current context)")
    ap.add_argument("--kubeconfig", help="kubeconfig file (default: $KUBECONFIG or ~/.kube/config)")
    ap.add_argument("--kubectl", default="kubectl", help="kubectl binary (default: kubectl on $PATH)")
    ap.add_argument("--listen", default="127.0.0.1:8080", help="host:port to listen on (default: 127.0.0.1:8080)")
    ap.add_argument("--cluster-name", help="name shown on the page (default: the context name)")
    ap.add_argument("--live-metrics-interval", type=int, default=DEFAULT_METRICS_INTERVAL, metavar="SECONDS",
                    help=f"seconds between metrics-server usage snapshots (default: {DEFAULT_METRICS_INTERVAL})")
    ap.add_argument("--system-ns-regex", default=DEFAULT_SYSTEM_NS_REGEX,
                    help="namespaces counted as System (regex on the name)")
    ap.add_argument("--system-project-regex", default=DEFAULT_SYSTEM_PROJECT_REGEX,
                    help="Rancher Projects counted as System (regex on the display name, case-insensitive)")
    ap.add_argument("--project-label", help="group namespaces by this label instead of the Rancher Project annotation")
    names = ap.add_argument_group("Rancher Project names (optional, read-only)")
    names.add_argument("--rancher-local-context", help="kubectl context of the Rancher local cluster")
    names.add_argument("--rancher-local-kubeconfig", help="kubeconfig file of the Rancher local cluster")
    names.add_argument("--rancher-local-self", action="store_true", help="the cluster itself is the Rancher local cluster")
    names.add_argument("--rancher-names-configmap", metavar="NAMESPACE/NAME",
                       help="ConfigMap published by publish_rancher_names.py on this cluster")
    args = ap.parse_args()
    if args.live_metrics_interval < 5:
        ap.error("--live-metrics-interval must be at least 5 seconds")
    if args.rancher_names_configmap and "/" not in args.rancher_names_configmap:
        ap.error("--rancher-names-configmap must be NAMESPACE/NAME")
    if args.rancher_local_kubeconfig:
        args.rancher_local_kubeconfig = os.path.expanduser(args.rancher_local_kubeconfig)
    if args.kubeconfig:
        args.kubeconfig = os.path.expanduser(args.kubeconfig)
        os.environ["KUBECONFIG"] = args.kubeconfig   # for the kubectl() calls that take no kubeconfig
    if not args.cluster_name:
        args.cluster_name = context_label(args.context)

    live_state = LiveState(args, metrics_interval=args.live_metrics_interval)
    start(live_state, context=args.context, kubeconfig=args.kubeconfig, kubectl_cmd=(args.kubectl,))
    if (args.rancher_local_context or args.rancher_local_kubeconfig or args.rancher_local_self
            or args.rancher_names_configmap):
        threading.Thread(target=names_loop, args=(args, live_state), daemon=True).start()

    host, port = args.listen.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host, int(port)), make_handler(live_state))
    log(f"live dashboard for {args.cluster_name}: http://{args.listen}/  (/live, /text, /api/live/...)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

DOC = '''#!/usr/bin/env python3
"""Live cluster dashboard in one file: nodes, Rancher Projects, namespaces, workloads and pods with their requests,
limits and usage, plus the plain-text reports. Runs on a laptop or jump host with kubectl and a kubeconfig; nothing
is installed in the cluster and nothing is written to it.

    python3 live-dashboard.py --context prod-01 [--rancher-local-kubeconfig local.yaml]
    open http://127.0.0.1:8080/        (live view; /text: text reports; /api/live/text?report=nodes for curl)

Needs: Python 3.9+, kubectl, and get/list/watch on nodes, namespaces, pods and poddisruptionbudgets plus get on
metrics.k8s.io (usage shows n/a without metrics-server). A Rancher-downloaded kubeconfig works as is.

GENERATED by discovery/build_live_dashboard.py from live.py, textreport.py, discover.py and static/*.html in
https://github.com/cdalar/cluster-resource-allocation -- edit those, not this file.
"""
'''


def top_level(tree):
    """name -> node for the functions, classes and simple assignments at module level."""
    names = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names[t.id] = node
    return names


def segment(src_lines, node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "".join(src_lines[start - 1:node.end_lineno])


def imports_of(tree):
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.add(ast.unparse(node))
    return out


def extract_discover(src):
    """The seeds and everything they reference, in source order."""
    tree = ast.parse(src)
    names = top_level(tree)
    wanted, todo = set(), list(DISCOVER_SEEDS)
    while todo:
        name = todo.pop()
        if name in wanted:
            continue
        if name not in names:
            sys.exit(f"discover.py: no top-level {name}")
        wanted.add(name)
        todo += [n.id for n in ast.walk(names[name]) if isinstance(n, ast.Name) and n.id in names and n.id != name]
    lines = src.splitlines(keepends=True)
    nodes = sorted({id(names[n]): names[n] for n in wanted}.values(), key=lambda n: n.lineno)
    return [segment(lines, n) for n in nodes], {n.name if hasattr(n, "name") else None for n in nodes} | wanted


def body_after_imports(src):
    """Everything after the module docstring and the top-level imports (comments included)."""
    tree = ast.parse(src)
    skip = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
            or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
    last = max(n.end_lineno for n in skip) if skip else 0
    return "".join(src.splitlines(keepends=True)[last:]), top_level(tree).keys()


def page(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        html = f.read()
    html, n = re.subn(r'^\s*<a href="\.">Resource report</a>\n', "", html, flags=re.M)
    if n != 1:
        sys.exit(f"{name}: expected one 'Resource report' link, found {n}")
    if '"""' in html or html.endswith("\\"):
        sys.exit(f"{name}: cannot be embedded as a raw triple-quoted string")
    return html


def build():
    read = lambda n: open(os.path.join(HERE, n), encoding="utf-8").read()  # noqa: E731
    discover_src, live_src, text_src = read("discover.py"), read("live.py"), read("textreport.py")
    disc_parts, disc_names = extract_discover(discover_src)
    live_body, live_names = body_after_imports(live_src)
    text_body, text_names = body_after_imports(text_src)
    for a, b, what in ((disc_names, live_names, "discover/live"), (disc_names, text_names, "discover/textreport"),
                       (live_names, text_names, "live/textreport")):
        dup = (set(a) & set(b)) - {None}
        if dup:
            sys.exit(f"{what} both define {sorted(dup)}")
    imports = imports_of(ast.parse(discover_src)) | imports_of(ast.parse(live_src)) | imports_of(ast.parse(text_src))
    imports |= {"import argparse", "import urllib.parse", "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer"}
    imports -= {"import discover", "import live", "import textreport"}
    out = [DOC, "\n", "\n".join(sorted(imports, key=lambda s: (s.startswith("from"), s))), "\n\n",
           "# ---------------------------------------------------------------------------\n# From discover.py\n"
           "# ---------------------------------------------------------------------------\n\n",
           "\n\n".join(p.rstrip("\n") + "\n" for p in disc_parts), "\n\n",
           "# ---------------------------------------------------------------------------\n# live.py\n"
           "# ---------------------------------------------------------------------------\n", live_body, "\n\n",
           "# ---------------------------------------------------------------------------\n# textreport.py\n"
           "# ---------------------------------------------------------------------------\n", text_body, "\n\n",
           "# the copied modules call each other through their module names\n",
           "discover = live = textreport = sys.modules[__name__]\n\n",
           "# ---------------------------------------------------------------------------\n# static/live.html and "
           "static/text.html\n# ---------------------------------------------------------------------------\n\n",
           'LIVE_HTML = r"""', page("live.html"), '"""\n\n', 'TEXT_HTML = r"""', page("text.html"), '"""\n',
           SERVER]
    return "".join(out)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "build", "live-dashboard.py")
    code = build()
    compile(code, out, "exec")   # a syntax error here is a build error, not the user's
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(code)
    os.chmod(out, 0o755)
    print(f"{out}: {len(code.splitlines())} lines")


if __name__ == "__main__":
    main()
