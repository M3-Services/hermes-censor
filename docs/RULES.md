# Plain rules - syntax and semantics

The rules file (`rules_file`, default `<HERMES_HOME>/plugin-data/hermes-censor/rules.txt`) holds **one rule per
line**: `<pattern>:<replacement>`. It is **UTF-8** encoded (a BOM is tolerated). It is reloaded automatically when it
changes (checked at most every 2 s). **It is never used to export KeePassXC values**: secrets live only in memory,
in the engine index.

## Line format

| Line | Effect |
|---|---|
| `fox:animal` | replaces `fox` with `animal` |
| `fox:` | **deletes** `fox` (empty replacement) |
| `a:b:c` | pattern `a`, replacement `b:c` (the replacement may contain ":") |
| `https\://host.test:https://[internal]` | pattern `https://host.test` (escaped colon) |
| `# text` | comment ("#" in **column 0** only) |
| `\#label:x` | pattern `#label` (escaped leading "#") |
| *(blank or whitespace-only line)* | ignored |

- **Separator**: the first unescaped ":".
- **Recognised escapes, and only these**: `\:` -> `:`, `\\` -> `\`, and `\#` at the very start of a line. Any other
  backslash is literal (`C\:\new\dir` = `C:\new\dir`). Windows example: `C\:\\Users\\alice:[folder]`.
- **Significant whitespace**: `fox :x` has the pattern `fox ` (with the space). Only the trailing `\r` (CRLF) is
  stripped.
- No multi-line patterns; no regular expressions: search is **literal**.

## Invalid rules and duplicates

| Case | Handling |
|---|---|
| no unescaped ":" | line ignored, issue `MISSING_SEPARATOR` -> **DEGRADED** state |
| empty pattern (`:x`) | line ignored, issue `EMPTY_PATTERN` -> **DEGRADED** |
| same pattern, same replacement | a single copy kept, no issue |
| same pattern, different replacement | **the first rule is kept**, issue `CONFLICT` (informational, does not change the state) |
| unreadable / non-UTF-8 file | last valid version kept if there is one; **DEGRADED** |

Diagnostics quote the **line number and a code, never the content** of the rule.

## Priorities, overlaps, deterministic result

The text is scanned **once**; replacements are **never rescanned** (no cascade: `a:b` + `b:c` turns `ab` into `bc`,
not `cc`).

1. **A KeePassXC secret always takes priority** over a plain rule: any rule that overlaps a secret (even partially)
   is dropped for that occurrence.
2. Between secrets, or between rules: **the leftmost match wins, then the longest**, then file order. Overlapping
   matches are dropped.
3. Deletion (`fox:`) does not normalise whitespace: `a fox here` -> `a  here`.

## Unicode and case

- No normalisation is applied to the text (text outside matches stays byte for byte).
- Every pattern is indexed under its **NFC and NFD** forms (precomposed `e-acute`, or `e` + combining accent).
- Case is significant by default. `ignore_case: true` makes **plain rules** case-insensitive (character-by-character
  case folding that never changes positions); **secrets always stay case-sensitive**.

## JSON forms

Tool results and tool-call arguments are often JSON strings, in which `"`, `\` and non-ASCII characters are
escaped. Every pattern is therefore also indexed under its JSON-escaped forms (ASCII and UTF-8), with the replacement
escaped the same way: the JSON stays valid after filtering.

## What the filter does not do

It does not detect a pattern that is **reworded, encoded (base64, URL...), split across messages** or fragmented in a
stream. See `LIMITATIONS.md`.
