"""EXPERIMENT (not shipped): can the plugin censor Hermes auxiliary calls safely and cheaply?

Mechanism under test: an in-place edit of the message dictionaries handed to ``pre_auxiliary_call`` (an UNDOCUMENTED
side effect: Hermes copies the list, not the dictionaries), undone by ``post_auxiliary_call`` so the caller's own
objects are left as they were.

Runs against the REAL Hermes sources, an ISOLATED throw-away HERMES_HOME and a fake local provider with FICTIONAL
secrets. Never touches the user's profile.

    <hermes venv python> bench/experiment_aux_hook.py [--hermes-src DIR]

Safety checks (S*) print PASS/FAIL, performance checks (P*) print timings.
"""
import argparse
import asyncio
import copy
import json
import os
import random
import statistics
import string
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--hermes-src", default=os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "hermes-agent"))
args = ap.parse_args()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, args.hermes_src)
_tmp_home = tempfile.mkdtemp(prefix="censor_aux_exp_")
os.environ["HERMES_HOME"] = _tmp_home
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

from censor_core.engine import Censor  # noqa: E402
from censor_core.rules import Rule  # noqa: E402
from censor_core.walker import censor_request  # noqa: E402

CANARY = "je_suis_un_secret_vraiment_secret"
CANARY2 = "un_autre_secret_tres_long_42"
WORD = "Sibelga"
CENSOR = Censor.build(rules=[Rule(WORD, "AcmeCompany", 1)], secrets=[SECRET, SECRET2])
EMPTY = Censor.build()

# ---------------------------------------------------------------------------------------------- fake provider
GOT = []  # raw request bodies, in arrival order
GOT_LOCK = threading.Lock()
FAIL_FIRST = {"n": 0}


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0))).decode("utf-8", "replace")
        with GOT_LOCK:
            GOT.append(raw)
            fail = FAIL_FIRST["n"] > 0
            if fail:
                FAIL_FIRST["n"] -= 1
        if fail:
            body = b'{"error":{"message":"boom","type":"server_error"}}'
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            streaming = bool(json.loads(raw).get("stream"))
        except Exception:
            streaming = False
        if streaming:
            chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]}
            last = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            body = (f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(last)}\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
        else:
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "fake-model",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}/v1"

sys.path.insert(0, args.hermes_src)
from agent.auxiliary_client import async_call_llm, call_llm  # noqa: E402
from hermes_cli.plugins import get_plugin_manager  # noqa: E402

MGR = get_plugin_manager()


def aux(task, messages, **kw):
    return call_llm(task, provider="custom", base_url=BASE, api_key="fake-key", model="fake-model",
                    messages=messages, **kw)


# ---------------------------------------------------------------------------------------------- prototype
class Proto:
    """The mechanism to validate. ``restore=False`` shows what happens without the undo."""

    def __init__(self, censor=CENSOR, restore=True, boom=False):
        self.censor, self.restore, self.boom = censor, restore, boom
        self.saved = {}
        self.lock = threading.Lock()
        self.pre_calls = self.post_calls = 0
        self.pre_ms = []

    def pre(self, **kw):
        t0 = time.perf_counter()
        try:
            self.pre_calls += 1
            if self.boom:
                raise RuntimeError("simulated failure")
            msgs = kw.get("request_messages")
            if not isinstance(msgs, list) or self.censor.is_empty:
                return
            out, rep = censor_request({"messages": msgs}, self.censor)
            if not rep.changed:
                return
            saved = []
            for old, new in zip(msgs, out["messages"]):
                if old is new or not isinstance(old, dict) or not isinstance(new, dict):
                    continue
                for k, v in new.items():
                    if old.get(k) is not v:
                        saved.append((old, k, old.get(k)))
                        old[k] = v
            if self.restore:
                with self.lock:
                    self.saved.setdefault(kw.get("api_request_id"), []).extend(saved)
        finally:
            self.pre_ms.append((time.perf_counter() - t0) * 1000)

    def post(self, **kw):
        self.post_calls += 1
        if not self.restore:
            return
        with self.lock:
            saved = self.saved.pop(kw.get("api_request_id"), [])
        for d, k, orig in reversed(saved):
            d[k] = orig

    def __enter__(self):
        MGR._hooks.setdefault("pre_auxiliary_call", []).append(self.pre)
        MGR._hooks.setdefault("post_auxiliary_call", []).append(self.post)
        return self

    def __exit__(self, *a):
        MGR._hooks["pre_auxiliary_call"].remove(self.pre)
        MGR._hooks["post_auxiliary_call"].remove(self.post)


class NoOp(Proto):
    def pre(self, **kw):
        self.pre_calls += 1

    def post(self, **kw):
        self.post_calls += 1


def last_body():
    with GOT_LOCK:
        return GOT[-1]


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


# ---------------------------------------------------------------------------------------------- safety checks
def s1_basic_tasks():
    print("S1  hook fires and the provider receives only the replacement (several auxiliary tasks)")
    for task in ("compression", "title_generation", "session_search", "approval"):
        with Proto() as p:
            aux(task, [{"role": "system", "content": "sys"}, {"role": "user", "content": f"{WORD} and {CANARY}"}])
        b = last_body()
        check(f"task={task}", CANARY not in b and WORD not in b and "[SECRET]" in b and "AcmeCompany" in b,
              f"pre={p.pre_calls} post={p.post_calls}")


def s2_callers_objects_untouched():
    print("S2  the caller's own message objects are left untouched")
    for restore in (False, True):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": f"x {CANARY} y"}]
        snapshot = copy.deepcopy(msgs)
        with Proto(restore=restore):
            aux("compression", msgs)
        same = msgs == snapshot
        if restore:
            check("with undo: caller's dicts identical after the call", same)
        else:
            print(f"  [info] WITHOUT undo the caller's dicts {'are' if not same else 'are NOT'} altered "
                  f"(this is why the undo exists)")


def s3_shapes():
    print("S3  unusual message shapes: call succeeds, structure preserved, binary data intact")
    img = "data:image/png;base64," + "A" * 400
    shapes = [
        ("parts list with image", [{"role": "user", "content": [
            {"type": "text", "text": f"see {CANARY}"}, {"type": "image_url", "image_url": {"url": img}}]}]),
        ("assistant tool_calls + None content + tool msg", [
            {"role": "user", "content": f"run {CANARY}"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": json.dumps({"p": CANARY})}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": f"out {CANARY}"}]),
        ("no secret at all", [{"role": "user", "content": "nothing to hide"}]),
        ("empty content", [{"role": "user", "content": ""}]),
        ("unicode + json escapes", [{"role": "user", "content": f"é {json.dumps(CANARY)} ☃"}]),
    ]
    for name, msgs in shapes:
        before = copy.deepcopy(msgs)
        try:
            with Proto():
                aux("compression", msgs)
            b = last_body()
            sent = json.loads(b)
            ok = CANARY not in b and msgs == before and isinstance(sent.get("messages"), list)
            if name.startswith("parts"):
                ok = ok and img in b
            if name.startswith("assistant"):
                ok = ok and '"call_1"' in b
            check(name, ok)
        except Exception as exc:
            check(name, False, f"exception {type(exc).__name__}: {exc}")


def s4_failure_is_isolated():
    print("S4  a failing hook never breaks the auxiliary call (fail-open, as documented by Hermes)")
    with Proto(boom=True):
        try:
            r = aux("compression", [{"role": "user", "content": f"x {CANARY}"}])
            check("call returns normally when the hook raises", r is not None)
        except Exception as exc:
            check("call returns normally when the hook raises", False, type(exc).__name__)


def s5_retry():
    print("S5  retry after a server error: every attempt is censored")
    with GOT_LOCK:
        n0 = len(GOT)
    FAIL_FIRST["n"] = 1
    msgs = [{"role": "user", "content": f"retry {CANARY}"}]
    with Proto() as p:
        aux("compression", msgs)
    with GOT_LOCK:
        bodies = GOT[n0:]
    check("at least 2 attempts observed", len(bodies) >= 2, f"{len(bodies)} attempts")
    check("no attempt carries the secret", not any(CANARY in b for b in bodies))
    check("caller's dict restored", msgs[0]["content"] == f"retry {CANARY}")


def s6_streaming():
    print("S6  streaming auxiliary call (stream=True)")
    msgs = [{"role": "user", "content": f"stream {CANARY}"}]
    with Proto() as p:
        stream = aux("moa_aggregator", msgs, stream=True)
        got_before_iter = None
        with GOT_LOCK:
            got_before_iter = len(GOT)
        list(stream)
    b = last_body()
    check("provider received only the replacement", CANARY not in b and "[SECRET]" in b,
          f"pre={p.pre_calls} post={p.post_calls}")
    check("caller's dict restored", msgs[0]["content"] == f"stream {CANARY}")


def s7_async():
    print("S7  asynchronous auxiliary call")
    msgs = [{"role": "user", "content": f"async {CANARY}"}]

    async def go():
        return await async_call_llm("compression", provider="custom", base_url=BASE, api_key="fake-key",
                                    model="fake-model", messages=msgs)
    with Proto() as p:
        asyncio.run(go())
    b = last_body()
    check("provider received only the replacement", CANARY not in b and "[SECRET]" in b,
          f"pre={p.pre_calls} post={p.post_calls}")
    check("caller's dict restored", msgs[0]["content"] == f"async {CANARY}")


def s8_concurrency():
    print("S8  16 concurrent auxiliary calls x 5: no leak, no cross-talk, every caller restored")
    with GOT_LOCK:
        n0 = len(GOT)
    errors, restored_ok = [], []

    def worker(i):
        for j in range(5):
            s = CANARY if (i + j) % 2 == 0 else CANARY2
            msgs = [{"role": "user", "content": f"w{i}-{j} {s} {WORD}"}]
            try:
                aux("compression", msgs)
                restored_ok.append(msgs[0]["content"] == f"w{i}-{j} {s} {WORD}")
            except Exception as exc:
                errors.append(repr(exc))
    with Proto() as p:
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        t0 = time.perf_counter()
        [t.start() for t in ts]
        [t.join() for t in ts]
        dt = time.perf_counter() - t0
    with GOT_LOCK:
        bodies = GOT[n0:]
    check("no exception", not errors, errors[:1] and errors[0])
    check("80 requests received", len(bodies) == 80, str(len(bodies)))
    check("no secret and no rule word reached the provider",
          not any(CANARY in b or CANARY2 in b or WORD in b for b in bodies))
    check("all 80 caller dicts restored", len(restored_ok) == 80 and all(restored_ok))
    check("saved map empty afterwards (no leak of state)", not p.saved, f"{len(p.saved)} left")
    print(f"  [info] wall time {dt:.2f}s")


def s9_hook_timeouts_info():
    print("S9  (info) Hermes runs these hooks in a worker thread bounded by plugins.hook_callback_timeout")
    try:
        from hermes_cli.plugins import _resolve_hook_callback_timeout
        print(f"  [info] hook_callback_timeout = {_resolve_hook_callback_timeout()} s; a hook slower than that is "
              f"abandoned and the call goes out uncensored")
    except Exception as exc:
        print(f"  [info] cannot read the timeout ({type(exc).__name__})")


# ---------------------------------------------------------------------------------------------- performance
def make_text(size, rnd, words):
    parts, total = [], 0
    while total < size:
        line = " ".join(rnd.choices(words, k=12)) + "\n"
        parts.append(line)
        total += len(line)
    for _ in range(20):
        parts.insert(rnd.randrange(len(parts)), CANARY + " ")
    return "".join(parts)


def tok(rnd, n):
    return "".join(rnd.choices(string.ascii_letters + string.digits + "!#$%-_", k=n))


def p1_engine_cost():
    print("P1  engine cost on realistic compression payloads (pure Python engine, this machine)")
    rnd = random.Random(7)
    words = [tok(rnd, rnd.randint(3, 9)) for _ in range(5000)]
    print(f"  {'secrets':>8} {'payload':>10} {'first scan':>11} {'cached':>9}")
    for n in (10, 100, 1000):
        secrets = [SECRET] + [tok(rnd, rnd.randint(12, 40)) for _ in range(n - 1)]
        censor = Censor.build(rules=[Rule(WORD, "AcmeCompany", 1)], secrets=secrets)
        for size in (50_000, 200_000, 1_000_000):
            text = make_text(size, rnd, words)
            t0 = time.perf_counter()
            censor.apply(text)
            first = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            censor.apply(text)
            cached = (time.perf_counter() - t0) * 1000
            print(f"  {n:>8} {size:>10} {first:9.1f}ms {cached:7.2f}ms")


def timed_calls(n, payload_chars, hook_factory):
    text = ("lorem ipsum dolor sit amet " * (payload_chars // 27 + 1))[:payload_chars] + f" {CANARY}"
    times = []
    ctx = hook_factory()
    with ctx:
        for _ in range(n):
            msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]
            t0 = time.perf_counter()
            aux("compression", msgs)
            times.append((time.perf_counter() - t0) * 1000)
    return times


class Nothing:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def p2_end_to_end_overhead():
    print("P2  end-to-end overhead per auxiliary call (local fake provider: LLM latency is ~0, so this is the "
          "worst case; a real compression call takes seconds)")
    print(f"  {'payload':>9}  {'no hook':>10} {'no-op hooks':>12} {'real hook':>10}   (median ms / call, 30 calls)")
    for size in (2_000, 50_000, 300_000):
        timed_calls(3, size, Nothing)  # warm-up
        base = statistics.median(timed_calls(30, size, Nothing))
        noop = statistics.median(timed_calls(30, size, NoOp))
        real = statistics.median(timed_calls(30, size, Proto))
        print(f"  {size:>9}  {base:10.1f} {noop:12.1f} {real:10.1f}    "
              f"hook cost: +{real - base:5.1f} ms (of which merely having hooks: +{noop - base:5.1f} ms)")


def main():
    print(f"Hermes src: {args.hermes_src}\nIsolated HERMES_HOME: {_tmp_home}\n")
    print("=== SAFETY ===")
    for fn in (s1_basic_tasks, s2_callers_objects_untouched, s3_shapes, s4_failure_is_isolated, s5_retry,
               s6_streaming, s7_async, s8_concurrency, s9_hook_timeouts_info):
        try:
            fn()
        except Exception as exc:
            check(f"{fn.__name__} crashed", False, f"{type(exc).__name__}: {exc}")
    print("\n=== PERFORMANCE ===")
    p1_engine_cost()
    p2_end_to_end_overhead()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} safety checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
