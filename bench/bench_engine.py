"""Engine benchmark with FICTIONAL data (no real secret).

    python bench/bench_engine.py

Measures index construction, scanning of fresh text and replay (cache) for 10/100/1000 secrets and texts of
100 KB / 1 MB. Compares, for information, with ``pyahocorasick`` if installed (a native dependency NOT required
by the plugin).
"""
import os
import random
import string
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from censor_core.engine import Censor  # noqa: E402
from censor_core.rules import Rule  # noqa: E402

rnd = random.Random(1)


def token(n):
    return "".join(rnd.choices(string.ascii_letters + string.digits + "!#$%-_", k=n))


WORDS = [token(rnd.randint(3, 9)) for _ in range(5000)]


def make_text(size, secrets, hits=20):
    parts, total = [], 0
    while total < size:
        line = " ".join(rnd.choices(WORDS, k=12)) + "\n"
        parts.append(line)
        total += len(line)
    for _ in range(hits):
        parts.insert(rnd.randrange(len(parts)), rnd.choice(secrets) + " ")
    return "".join(parts)


def main():
    try:
        import ahocorasick
        have_native = True
    except ImportError:
        have_native = False
    print(f"Python {sys.version.split()[0]} ; pyahocorasick {'present' if have_native else 'absent (not required)'}")
    print(f"{'secrets':>8} {'text':>10} {'build':>9} {'fresh scan':>11} {'replay':>9}" + ("   native(scan)" if have_native else ""))
    for n in (10, 100, 1000):
        secrets = [token(rnd.randint(12, 40)) for _ in range(n)]
        rules = [Rule(w, "x", i + 1) for i, w in enumerate(WORDS[:20])]
        for size in (100_000, 1_000_000):
            text = make_text(size, secrets)
            t0 = time.perf_counter()
            censor = Censor.build(rules=rules, secrets=secrets)
            build = time.perf_counter() - t0
            t0 = time.perf_counter()
            out = censor.apply(text)
            scan = time.perf_counter() - t0
            t0 = time.perf_counter()
            censor.apply(text)
            replay = time.perf_counter() - t0
            assert not any(s in out for s in secrets)
            native = ""
            if have_native:
                a = ahocorasick.Automaton()
                for s in secrets:
                    a.add_word(s, len(s))
                a.make_automaton()
                t0 = time.perf_counter()
                list(a.iter(text))
                native = f"   {(time.perf_counter() - t0) * 1000:8.1f} ms"
            print(f"{n:>8} {size:>10} {build * 1000:7.1f}ms {scan * 1000:9.1f}ms {replay * 1000:7.2f}ms{native}")


if __name__ == "__main__":
    main()
