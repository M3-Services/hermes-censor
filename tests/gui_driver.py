"""Drive one scenario of the Tk window in a FRESH process (as in production). Output: JSON on stdout."""
import json
import sys

sys.path.insert(0, sys.argv[1])
from censor_core.kpx_helper import HelperError, _ask_gui  # noqa: E402

scenario = sys.argv[2]
seen = {}


def ready(root, entry, ok, cancel):
    seen["show"] = entry.cget("show")
    if scenario == "typed":
        entry.insert(0, "Fictional-password é€")
        ok.invoke()
    elif scenario == "cancel":
        cancel.invoke()
    elif scenario == "empty":
        ok.invoke()


try:
    hook = None if scenario == "timeout" else ready
    pw = _ask_gui("test", 1 if scenario == "timeout" else 10, _on_ready=hook)
    out = {"result": pw, "show": seen.get("show")}
except HelperError as exc:
    out = {"error": exc.code, "show": seen.get("show")}
sys.stdout.buffer.write(json.dumps(out, ensure_ascii=True).encode("ascii"))
