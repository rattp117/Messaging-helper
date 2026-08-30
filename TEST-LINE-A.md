# Test Report — LINE Module A (LineChannel + webhook/media server)

> **FINAL VERDICT (2026-08-30 re-check): PASS.** Luna's fix for both defects found below has landed in `channels/line_webhook.py:_handle_callback`. Re-verified: diff read, 4 fresh adversarial probes run against the fix, and the full Module A subset re-run with `tests/test_line_a_gaps.py` **unedited**. All 5 previously-failing tests now pass; 0 failures anywhere in scope. Full detail in "Fix verification (2026-08-30)" near the end of this report — that section is authoritative. The Summary/Failures/Recommendation sections immediately below are preserved as originally written from the pre-fix run, for the record of what was found and why.

## Summary (as of the original pre-fix run — superseded, see verdict above)

- **New adversarial test file:** `tests/test_line_a_gaps.py` — 49 tests
- **Module A subset run** (`test_line_channel.py` + `test_line_webhook.py` + `test_line_a_gaps.py` + the 3 rewritten pre-existing tests, `-m "not telegram_only and not llm_only"`):
  - Total: 112 (24 + 36 + 49 + 3)
  - Passed: 107
  - Failed: **5** (all in `tests/test_line_a_gaps.py`, all newly discovered, none regressions)
  - Skipped: 0 Module-A-relevant (1 unrelated pre-existing skip in `test_channels.py:257`, not touched by this branch)
- **Status (pre-fix): FAIL** — every numbered AC (AC5–AC14) passes on its own literal Given/When/Then, but adversarial probing of the security perimeter (`POST /callback`'s JSON-handling path) found a real, reproducible gap against R-A2's own normative text and §3.4's documented response contract: a correctly-signed request with certain malformed bodies returns `500` instead of the spec's only two documented outcomes (`200`/`400`).
- **Everything else probed — signature timing-safety, header case, mutated-body rejection, path traversal (9-item extended corpus), reply-buffer boundaries, contextvar isolation under real concurrency, quick-reply/postback-data boundaries, FIFO ordering (with start/end instrumentation), and outbound no-retry parity with `telegram.py` — held up. No other defects found.**

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_line_channel.py` (Luna) | 24 | AC7, AC8, AC9, AC11, AC13, AC14 |
| `tests/test_line_webhook.py` (Luna) | 36 | AC5, AC6, AC10, AC11, AC12 |
| `tests/test_line_a_gaps.py` (Vera, new) | 49 (44 pass / 5 fail) | Adversarial: AC5, AC6 (+R-A2/§3.4 gap), AC7, AC8, AC9, AC10, AC12, plus retry/backoff and ordering probes beyond any single AC |
| `tests/test_channels.py` (rewritten, 1 test) | — | ABC-shape regression for the now-real `LineChannel` |
| `tests/test_charts.py` (rewritten, 1 test) | — | ABC-shape regression (`send_image` default vs abstract) |
| `tests/test_deliverables.py` (rewritten, 1 test) | — | Deliverable-presence check, stub→real |

## AC coverage

| AC | Description | Test(s) | Result |
|---|---|---|---|
| AC5 | Correct sig → 200 + enqueue; wrong/missing sig → 400, nothing enqueued/sent | `test_line_webhook.py::test_verify_signature_*`, `test_callback_*`; `test_line_a_gaps.py::test_verify_signature_malformed_base64_signature_is_false_not_a_crash`, `test_verify_signature_correct_but_wrong_length_signature_is_false`, `test_verify_signature_unicode_body_round_trips`, `test_verify_signature_binary_non_utf8_body_does_not_raise`, `test_header_case_insensitivity_lowercase_as_line_actually_sends_it`, `test_signature_over_mutated_body_whitespace_only_difference_rejected`, `test_signature_valid_for_body_a_rejected_when_events_appended`, `test_oversized_payload_rejected_gracefully_not_a_crash_or_hang`, `test_verify_signature_uses_compare_digest_structurally` | **PASS** (literal AC scenario). See "Security probe table" for the adjacent R-A2 gap. |
| AC6 | 200 returned before handler runs; 3 events processed FIFO by one worker | `test_line_webhook.py::test_callback_returns_200_before_any_handler_runs`, `test_worker_processes_events_fifo_in_order`; `test_line_a_gaps.py::test_second_users_handler_does_not_start_until_first_users_dispatch_fully_completes`, `test_interleaved_users_preserve_strict_global_enqueue_order`, `test_same_user_two_messages_process_and_flush_in_order` | **PASS** |
| AC7 | 2 sends in one event → 1 reply call, 2 objects, that event's replyToken, no push/ledger | `test_line_channel.py::test_two_sends_in_one_reply_context_batch_into_one_reply_call`; `test_line_a_gaps.py::test_exactly_five_reply_objects_survive_with_no_warning`, `test_exactly_six_reply_objects_drops_only_the_sixth`, `test_reply_dropped_on_network_transport_error_not_just_bad_status`, `test_reply_context_resets_via_finally_even_when_handler_raises`, `test_two_concurrent_reply_scopes_never_cross_contaminate_buffers` | **PASS** |
| AC8 | Push with no reply context → Push API + ledger increment | `test_line_channel.py::test_send_with_no_active_context_pushes_and_increments_ledger`; `test_line_a_gaps.py::test_push_network_transport_error_does_not_increment_ledger` | **PASS** |
| AC9 | `send_actionable` → quickReply, verbatim data, 13-item cap + WARN | `test_line_channel.py::test_send_actionable_*`; `test_line_a_gaps.py::test_exactly_thirteen_buttons_all_survive_no_warning`, `test_exactly_fourteen_buttons_drops_only_the_fourteenth`, `test_postback_data_exactly_300_chars_no_warning`, `test_postback_data_301_chars_warns_but_still_sent_verbatim` | **PASS** |
| AC10 | Postback → `on_callback(userId, data, "", …)` verbatim, prefix router unmodified | `test_line_webhook.py::test_postback_data_routed_verbatim_with_empty_source_text` (×4 prefixes); `test_line_a_gaps.py::test_callback_data_verbatim_round_trip_send_actionable_to_on_callback` (×5, including a 310-char payload) | **PASS** |
| AC11 | `send_image` writes token file, sends text+image pair, `GET /media/{token}.png` serves bytes, traversal/expired → 404 | `test_line_channel.py::test_send_image_*`; `test_line_webhook.py::test_media_get_*`; `test_line_a_gaps.py::` 9-variant extended traversal corpus, `test_media_get_directory_traversal_cannot_escape_media_dir_even_with_valid_token_shape`, `test_media_no_directory_listing_on_bare_media_path`, `test_token_regex_charset_edges` | **PASS** |
| AC12 | TTL cleanup deletes aged files, later GET → 404, never raises | `test_line_webhook.py::test_cleanup_expired_media_*`; `test_line_a_gaps.py::test_media_ttl_boundary_file_just_under_ttl_survives_just_over_is_removed` | **PASS** |
| AC13 | 6 inherited base-default degradations, no crash | `test_line_channel.py::test_degradations_use_base_defaults` | **PASS** |
| AC14 | Rich menu create+upload+set-default at startup, fail-open on missing image/API failure | `test_line_channel.py::test_register_rich_menu_*`; `test_line_a_gaps.py::test_rich_menu_registration_failure_makes_no_retry_attempts` | **PASS** |

## Security probe table

| Probe | Result | Evidence |
|---|---|---|
| Wrong secret / empty sig / missing header | Rejects, 400 | `test_line_webhook.py` (existing) |
| Malformed base64 signature (garbage chars, all-`=`, control bytes, 5000-char string, empty) | Rejects, never raises | `test_verify_signature_malformed_base64_signature_is_false_not_a_crash` |
| Signature truncated / with trailing garbage | Rejects | `test_verify_signature_correct_but_wrong_length_signature_is_false` |
| Sig computed over MUTATED body (content mutation) | Rejects, raw-bytes-before-parse confirmed | `test_signature_valid_for_body_a_rejected_when_events_appended` |
| Sig computed over MUTATED body (**whitespace-only reformatting**, JSON-equivalent) | Rejects — proves verification binds to exact raw socket bytes, not a re-serialized/normalized form | `test_signature_over_mutated_body_whitespace_only_difference_rejected` |
| Unicode body (Thai text, this channel's primary market) | Signs/verifies correctly | `test_verify_signature_unicode_body_round_trips` |
| Binary/all-byte-value body | `verify_signature` itself never raises | `test_verify_signature_binary_non_utf8_body_does_not_raise` |
| Oversized payload (2 MB) | Rejected gracefully (aiohttp's own 1 MiB `client_max_size` → 413), no hang/crash | `test_oversized_payload_rejected_gracefully_not_a_crash_or_hang` |
| **Correct sig, garbage JSON: well-formed JSON, wrong top-level shape** (`[1,2,3]`, `null`, `42`, `"str"`) | ~~FAIL — 500~~ **FIXED, now PASS** — `isinstance(payload, dict)` guard → 400+WARN | See Failures §1 + "Fix verification" |
| **Correct sig, garbage JSON: invalid-UTF8 body** | ~~FAIL — 500~~ **FIXED, now PASS** — `except (JSONDecodeError, UnicodeDecodeError)` → 400+WARN | See Failures §2 + "Fix verification" |
| Correct sig, `{"events": "abc"}` (dict-shaped, non-list `events`) | 200 returned; garbage chars enqueued but the worker's broad `except Exception` swallows the resulting per-character `AttributeError`s — no crash, no calls made | `test_callback_events_key_not_a_list_does_not_crash_the_worker` |
| Timing side-channel on signature compare | `hmac.compare_digest` used structurally (not `==`) | `test_verify_signature_uses_compare_digest_structurally` |
| Header case-insensitivity (spec §2.1 itself writes the header lowercase; LINE/proxies may vary casing) | Accepted regardless of case (aiohttp `CIMultiDict`) | `test_header_case_insensitivity_lowercase_as_line_actually_sends_it` |
| Media path traversal: `../`, url-encoded `..%2f`, backslash `..\`, url-encoded backslash `%5c`, null byte `%00`, doubled-dot `....//`, fully percent-encoded `%2e%2e%2f`, 200-char token, bare `.png` | All 404, none 500, none leak content | `test_media_path_traversal_and_charset_edge_corpus_returns_404_or_400` (9 variants) + existing `test_line_webhook.py` corpus (4 more variants) |
| Token charset edges: dot, space, `+`, `/`, `=`, newline, embedded null, non-ASCII, exactly-64 vs 65 chars | Regex-level: charset-invalid chars rejected; length boundary exact | `test_token_regex_charset_edges` |
| Positive-control traversal (planted secret file just outside `media_dir`) | Never reachable via any traversal string, content never appears in a response | `test_media_get_directory_traversal_cannot_escape_media_dir_even_with_valid_token_shape` |
| No directory listing on `/media/` or `/media` | 404, no listing | `test_media_no_directory_listing_on_bare_media_path` |
| Media TTL exact boundary (`age >= ttl` semantics) | Just-under survives, just-over deleted | `test_media_ttl_boundary_file_just_under_ttl_survives_just_over_is_removed` |
| Reply buffer overflow: exactly 5 (no warn) vs exactly 6 (drop 6th only) | Confirmed: first N survive, only the true overflow item(s) drop | `test_exactly_five_reply_objects_survive_with_no_warning`, `test_exactly_six_reply_objects_drops_only_the_sixth` |
| Reply dropped on transport-level failure (not just bad HTTP status) | Never falls back to push | `test_reply_dropped_on_network_transport_error_not_just_bad_status` |
| Reply-context leak after handler exception | `finally`-based reset confirmed — a later unrelated `send()` correctly falls through to push, not a stale buffer | `test_reply_context_resets_via_finally_even_when_handler_raises` |
| Contextvar isolation under **real concurrency** (`asyncio.gather`, not the sequential worker) | Two tasks' buffers never cross-contaminate, regardless of interleaved awaits | `test_two_concurrent_reply_scopes_never_cross_contaminate_buffers` |
| Push failure via network-level exception (not HTTP status) → ledger | Never increments | `test_push_network_transport_error_does_not_increment_ledger` |
| httpx retry/backoff on 429/5xx for push / reply / rich-menu-create | **Confirmed: zero retries on all three** — exactly 1 HTTP attempt each, matching `channels/telegram.py`'s own precedent (only the *inbound* `getUpdates` poll loop backs off; no outbound send ever retries) | `test_push_failure_makes_exactly_one_http_attempt_no_retry_loop`, `test_reply_failure_makes_exactly_one_http_attempt_no_retry_loop`, `test_rich_menu_registration_failure_makes_no_retry_attempts` — **not a gap, matches documented design** |
| Quick-reply 13-item boundary: exactly 13 (no warn) vs exactly 14 (drop 14th only) | Confirmed | `test_exactly_thirteen_buttons_all_survive_no_warning`, `test_exactly_fourteen_buttons_drops_only_the_fourteenth` |
| Postback `data` 300/301-char boundary | 300: no warning. 301: WARN logged, **still sent verbatim, not truncated** (matches AC9's "verbatim" contract — truncation is never mentioned) | `test_postback_data_exactly_300_chars_no_warning`, `test_postback_data_301_chars_warns_but_still_sent_verbatim` |
| Callback_data verbatim round-trip through a simulated postback event | Byte-for-byte identical from `send_actionable`'s emitted `data` through `LineWebhookServer.process_event` into `on_callback`, across 5 payload shapes incl. a 310-char one | `test_callback_data_verbatim_round_trip_send_actionable_to_on_callback` |
| Per-user ordering (2 msgs, same user) | In order | `test_same_user_two_messages_process_and_flush_in_order` |
| Interleaved users (A1,B1,A2,B2 enqueue order) | Processed in exact global enqueue order — no per-user lane, matches R-A3's documented "global, hence per-user" design | `test_interleaved_users_preserve_strict_global_enqueue_order` |
| Strict serialization proof (start/end timestamps, not just final order) | User B's handler provably does **not start** until user A's dispatch (handler + flush) has **fully ended** | `test_second_users_handler_does_not_start_until_first_users_dispatch_fully_completes` |

## Failures

### 1. `test_callback_valid_signature_well_formed_json_wrong_top_level_shape_never_500s` (4 parametrized cases)

- **What was tested:** A correctly-signed `POST /callback` body that is valid JSON but not `{"events": [...]}`-shaped (a bare JSON array, `null`, a number, or a string at the top level).
- **AC/rule violated:** §3.4 ("`POST /callback`: `200 OK`... `400` on missing/invalid signature or unparseable body" — no third outcome is documented) / R-A2.
- **Input:** Correctly-signed bodies `[1,2,3]`, `null`, `42`, `"just a string"`.
- **Expected:** `200` or `400` (the only two documented outcomes for this endpoint).
- **Actual:** `500 Internal Server Error` for all four.
- **Stack trace / output:**
  ```
  File "channels/line_webhook.py", line 144, in _handle_callback
      events = payload.get("events") or []
               ^^^^^^^^^^^
  AttributeError: 'list' object has no attribute 'get'   # (also 'NoneType', 'int', 'str' for the other 3 cases)
  ```
- **Suspected cause:** `src/habit_assistant/channels/line_webhook.py:144` — `payload.get("events")` assumes `payload` is always a `dict` after a successful `json.loads`, which is only true when the top-level JSON value is an object. aiohttp turns the unhandled `AttributeError` into a `500` automatically.
- **Why it matters beyond "just a status code":** LINE's own webhook delivery infrastructure treats `5xx` as "temporary failure, please redeliver" — a single malformed/adversarial delivery with a valid signature could trigger repeated redelivery attempts against this endpoint, and a `500` is a strictly worse signal on the public perimeter than a clean `400`.
- **Suggested fix (for Luna, not applied by me):** guard with `isinstance(payload, dict)` (or wrap the whole parse+shape step in one `try/except (json.JSONDecodeError, AttributeError, TypeError)`) and return `400` for any non-dict top level, consistent with the "unparseable body" bucket in §3.4.

### 2. `test_callback_valid_signature_invalid_utf8_body_returns_400_not_500`

- **What was tested:** A correctly-signed `POST /callback` body that is not valid UTF-8 at all (`b"\x80\x81\x82\x83"`).
- **AC/rule violated:** R-A2's own words: *"A body that fails JSON parsing → `400`."* This body unambiguously fails to parse as JSON.
- **Input:** raw bytes `b"\x80\x81\x82\x83"`, correctly HMAC-signed.
- **Expected:** `400`.
- **Actual:** `500 Internal Server Error`.
- **Stack trace / output:**
  ```
  File "channels/line_webhook.py", line 140, in _handle_callback
      payload = json.loads(raw)
  File ".../json/__init__.py", line 341, in loads
      s = s.decode(detect_encoding(s), 'surrogatepass')
  UnicodeDecodeError: 'utf-8' codec can't decode byte 0x80 in position 0: invalid start byte
  ```
- **Suspected cause:** `src/habit_assistant/channels/line_webhook.py:139-143` — `json.loads` on invalid-UTF8/UTF-16/UTF-32-looking byte sequences can raise `UnicodeDecodeError` during its internal encoding-detection step, not just `json.JSONDecodeError`. The `except` clause only names `json.JSONDecodeError`, so this exception type falls through uncaught. (Note: not every invalid-UTF8 byte string triggers this — CPython's `json.detect_encoding` heuristic means some malformed bytes raise `JSONDecodeError` instead, which *is* caught correctly; `\x80\x81\x82\x83` specifically reproduces the gap. Verified directly against this environment's stdlib before writing the assertion.)
- **Suggested fix:** widen the except clause to `except (json.JSONDecodeError, UnicodeDecodeError):` (or decode defensively first with `errors="replace"`/explicit try before `json.loads`).

**Both failures share one root cause and one fix location** (`_handle_callback`'s try/except around lines 139–144) — recommend Luna fix both together in one pass.

## Regressions detected

None. `test_line_channel.py` (24/24) and `test_line_webhook.py` (36/36) are unchanged and fully green; the 3 rewritten pre-existing tests (`test_channels.py`, `test_charts.py`, `test_deliverables.py`) all pass and are faithful rewrites (see below). All 5 failures are newly written adversarial tests exposing a pre-existing gap, not something my changes broke.

## The 3 rewritten pre-existing tests — faithfulness review

Reviewed via `git diff` against each file's pre-branch version (manual code review, not a new test — the rewritten tests themselves already run and pass in the subset above):

| File | Old assertion | New assertion | Faithful? |
|---|---|---|---|
| `tests/test_channels.py` | `test_line_channel_run_stub_accepts_on_callback_kwarg`: stub's `run()` must raise `NotImplementedError` even with `on_callback` passed | **Removed**, replaced with a comment pointing at the two new Module-A test files | **Yes** — the old test was pinning down stub behavior that no longer exists (the class isn't a stub anymore); removing it (not leaving a false assertion) and pointing at its replacement coverage is the correct move, not a coverage loss. `run()`'s real behavior (incl. `on_callback` handling) is now covered by `test_line_webhook.py`'s FIFO/dispatch tests. |
| `tests/test_charts.py` | `test_line_channel_stub_still_imports_and_is_a_valid_channel_subclass`: bare `LineChannel()` must raise `NotImplementedError`, while still being `issubclass(LineChannel, Channel)` | `test_line_channel_is_a_valid_channel_subclass_with_a_concrete_send_image`: constructs a **real** `LineChannel` and asserts `isinstance(channel, Channel)` | **Yes** — same regression target (ABC-shape: `send_image`'s concrete-not-abstract status doesn't break subclassing), same assertion strength, updated for the fact that construction now succeeds instead of raising. Equal-or-stronger: old test could only prove `issubclass`; new test proves a real instance also satisfies `isinstance`. |
| `tests/test_deliverables.py` | `test_line_channel_stub_documents_webhook_requirement`: file exists, mentions "webhook", contains literal string `"NotImplementedError"` | `test_line_channel_is_implemented_via_the_webhook_server`: file exists, mentions "webhook", contains `"class LineChannel"`, does **not** contain `"NotImplementedError"` | **Yes** — inverts the one assertion that was stub-specific (presence→absence of `NotImplementedError`) and adds a real-implementation marker (`class LineChannel`), while keeping the two assertions that were never stub-specific (file exists, mentions webhook) unchanged. |

All three are faithful, equal-or-stronger rewrites of their original intent — no coverage was silently dropped.

## Tree state (parallel-development note)

This is a shared (non-isolated) worktree; `git status` at test time shows Modules B/C actively mid-edit on `core/routing.py`, `core/query.py`, `core/confirmation.py`, `core/review.py`, `core/health.py` (Module B — no-LLM), and `core/jobs.py`, `core/audit.py`, `core/audit_view.py`, `core/commands.py` (Module C — digest/quota, `/digest` command wiring), plus their own in-flight test files (`test_line_b_gaps.py`, `test_line_c_gaps.py`, `test_line_d_gaps.py`, `test_line_no_llm.py`). None of these touch `channels/line.py` or `channels/line_webhook.py`. The known noise Archi flagged — `test_v110_m3_gaps.py`'s flake and `test_refactor_s3.py`'s in-flight `/digest` matcher table (Module C) — was **not** re-run as part of this pass; it's outside Module A's file ownership and irrelevant to this verdict. This report's subset (`test_line_channel.py` + `test_line_webhook.py` + `test_line_a_gaps.py` + the 3 rewrites) was run in isolation from the rest of the gate and is unaffected by the concurrent B/C edits.

## Fix verification (2026-08-30)

Luna's fix landed in `src/habit_assistant/channels/line_webhook.py:_handle_callback`. Re-check performed foreground, in the worktree, with `PYTHONPATH` set to `<worktree>/src` and the main-repo venv Python (same setup as the whole rest of this report — no `uv`, no production-code edits by me).

**1. Diff read (current source, lines 139–164):**

```python
try:
    payload = json.loads(raw)
except (json.JSONDecodeError, UnicodeDecodeError):
    # R-A2: "a body that fails JSON parsing -> 400". json.loads can
    # raise either on malformed JSON text (JSONDecodeError) or, via
    # its own internal encoding-detection step, on invalid-UTF8/
    # UTF-16/UTF-32-looking byte sequences (UnicodeDecodeError) --
    # both mean "this body cannot be parsed", same outcome.
    logger.warning("LINE webhook body failed to parse as JSON; dropping request")
    return web.Response(status=400)
if not isinstance(payload, dict):
    # §3.4 documents exactly two outcomes for POST /callback (200/
    # 400) -- valid JSON whose top level isn't an object (a bare
    # array/null/number/string) is not the documented
    # `{"events": [...]}` shape, so it belongs in the same
    # "unparseable body" 400 bucket as a JSON syntax error, not an
    # unhandled 500 from `payload.get(...)` on a non-dict.
    logger.warning(
        "LINE webhook body parsed but top-level JSON value is not an object (got %s); dropping request",
        type(payload).__name__,
    )
    return web.Response(status=400)
events = payload.get("events") or []
for event in events:
    await self.queue.put(event)
return web.Response(status=200)
```

This is exactly the two changes recommended in Failures §1/§2 below: the `except` clause now names both `json.JSONDecodeError` and `UnicodeDecodeError`, and a new `isinstance(payload, dict)` guard catches the non-dict-top-level case before `payload.get(...)` can raise. Both new branches log a WARN and return `400`, matching every other rejection path in this handler (signature failure, JSON-parse failure) — **the 400 choice is applied consistently: every rejection reason in `_handle_callback` now produces the same shape of response (400 + one WARN log), and the function has exactly two possible outcomes, 200 or 400, matching §3.4 exactly with no third code path left.** The pre-existing `{"events": "abc"}` case (dict-shaped payload, non-list `events`) is untouched, as Archi noted — `payload.get("events") or []` still yields the string itself in that case, still enqueues characters, still 200 — this was never part of the bug (it doesn't crash the handler) and correctly wasn't touched.

**2. Fresh adversarial probes against the fix** (via a real aiohttp `TestClient`/`TestServer`, correctly-signed requests throughout):

| Probe | Result | Verdict |
|---|---|---|
| Signed **empty body** (`b""`) | `400` (empty string fails `json.loads`, caught by the widened except) | Correct per R-A2 — an empty body is not parseable JSON |
| Valid signed body with **no `Content-Type` header at all** | `200`, unaffected | Correct — the handler never inspects `Content-Type`, only reads raw bytes |
| `{"events": null}` | `200`, 0 events enqueued (`None or []` → `[]`) | Correct, unchanged pre-existing behavior, not part of the bug |
| **Deeply nested junk mixed into a valid events list** — 7 items: 1 legit message event, a 500-levels-deep nested dict, a nested list `[1,2,[3,4,[5,6]]]`, a bare string, an int, `null`, and 1 more legit message event | Handler: `200`, all 7 enqueued (no per-item shape validation at this layer, by design). Worker: drained all 7 — the list/string/int/None items each raised `AttributeError` inside `process_event` (caught by the worker's own pre-existing broad `except Exception`, logged, loop continues); the deep dict was simply skipped via the normal "no `source.type==user`" path (dicts have `.get`, so no exception at all there); **both legit events were still processed, in order** (`legit-before` then `legit-after`) | Worker survives; ordering preserved around the junk; no crash anywhere in the pipeline |

No new breakage found. The fix is scoped exactly to the payload-shape/encoding validation in `_handle_callback` and does not touch (or need to touch) the worker's own pre-existing per-event robustness.

**3. Full rerun.** `tests/test_line_a_gaps.py` is **byte-identical** to what I wrote during the original pass (Archi confirmed unedited; I did not touch it either) — re-running it against the fixed code is therefore a clean confirmation, not a retest of a moving target:

```
tests/test_line_a_gaps.py                                            49 passed in 1.73s
tests/test_line_channel.py + test_line_webhook.py + test_line_a_gaps.py
  + test_channels.py + test_charts.py + test_deliverables.py
  (-m "not telegram_only and not llm_only")                          152 passed, 1 skipped, 30 deselected in 5.67s
```

152 = 147 (previously passing) + 5 (previously failing, now fixed) — exact reconciliation with the pre-fix run, confirming nothing else moved. The 1 skip is the same pre-existing, unrelated `test_channels.py:257` skip noted throughout this report; the 30 deselections are `telegram_only`-marked tests in `test_channels.py`, correctly excluded from the LINE gate.

## Recommendation

**PASS. Module A is ready to ship / integrate.** Both defects found during adversarial testing (Failures §1, §2 below) are fixed in `channels/line_webhook.py:_handle_callback`, verified via diff review, 4 fresh probes targeting exactly the kind of input that could newly break, and a full rerun of the unedited adversarial suite plus the rest of the Module A subset — 152/152 passing, 0 failures, 1 unrelated pre-existing skip. All 10 owned ACs (AC5–AC14) pass. The 3 rewritten pre-existing tests remain faithful. No regressions. Module A's own file set (`channels/line.py`, `channels/line_webhook.py`) needs no further changes from this tester's perspective.
