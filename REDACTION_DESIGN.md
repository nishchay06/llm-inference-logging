# PII Redaction Design — lightweight, in-house

Scrub PII from the inference-log **previews** before they are logged, so raw
user secrets never reach the telemetry (which is shipped over the broker,
aggregated in `/stats`, and shown in the dashboard's log explorer).

## Approach (from the established tools, minus the heavy deps)
- **Redact at the source** — inside the SDK, before the `InferenceLog` is
  emitted (mirrors Langfuse's client-side masking; the value never leaves the
  process un-scrubbed).
- **Regex detectors + typed-token replacement** — the deterministic workhorse
  (Datadog SDS / CloudWatch style). No Presidio/NER: previews are short, we want
  zero heavy dependencies, and structured PII is exactly what regex does well.
- **Preserve utility** — replace with a *typed* token (`[EMAIL]`, `[CARD]`) so
  the log stays readable/debuggable, rather than blanking it.
- **Fail safe** — if the scrubber ever raises, return `[REDACTED]`, never the raw
  text.

## Where it plugs in
One choke point: `sdk/tracing.py::_preview` — the single helper that builds every
preview (input, output, chat + stream + error). It becomes
`redact(text) → truncate`, so **every** preview is scrubbed and no call site can
forget. Redact runs on the full text *before* truncation (so a value near the
200-char cut can't leak a half-match). `TracedClient` is otherwise unchanged.

Redaction lives in a new `sdk/redaction.py` (`redact(text) -> str`) so the rules
are isolated and unit-testable.

## Detectors (regex, applied in this order)
| Token | Matches | Note |
|---|---|---|
| `[API_KEY]` | `sk-…` / `pk-…` / `rk-…` (len ≥ 10), `AKIA…` (AWS) | first, before generic patterns |
| `[EMAIL]` | `user@host.tld` | |
| `[CARD]` | 13–19 digit runs (spaces/dashes ok) **that pass Luhn** | Luhn check avoids clobbering arbitrary numbers |
| `[SSN]` | `NNN-NN-NNNN` (US) | |
| `[IP]` | IPv4 dotted quad | |
| `[PHONE]` | US `(NNN) NNN-NNNN` / `NNN-NNN-NNNN` (+ optional country code) or `+` followed by 9–15 digits | last (greediest); constrained to avoid matching dates/plain numbers |

Order matters: API keys and email before the numeric detectors; **cards before
phone** so a 16-digit card isn't mislabeled; phone last.

## Scope
- **Applies to `input_preview` / `output_preview`** in the inference logs — the
  telemetry that gets shipped, aggregated, and displayed.
- The chatbot's `messages` table stores the *full* conversation (the app's own
  data the user owns) and is left as-is — a documented scope choice; redacting
  app data too would be a natural extension.
- Always-on (safe default); a toggle would be a trivial add.

## Known limitations (honest, documented)
Regex is imperfect: it won't catch unstructured PII (names, addresses) — that's
the Presidio/NER upgrade — and phone/number heuristics can have false positives.
Accepted for a lightweight in-house scrubber; noted in the README.

## TDD
- `sdk/redaction.py`: each detector (email, SSN, IP, API key, Luhn-valid card,
  phone) → correct token; an **invalid-Luhn** number is *not* tagged `[CARD]`;
  clean text is unchanged; multiple PII in one string all scrubbed.
- Wrapper integration (`test_tracing.py`): a chat whose input/output contains an
  email / card produces an `InferenceLog` whose previews contain the token and
  **not** the raw value.
