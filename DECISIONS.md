# DECISIONS

D-000 (S0) CLAUDE.md already contained section 1 verbatim when the run started; kept as is.

D-001 (P1) First start of the 4.5h sprint prompt at T0=2026-09-26T08:19:28Z. DEADLINE_UTC 12:49:15Z is later than T0+4:20, so no phase compression.
D-002 (P1) CLAUDE.md overwritten with section 1 of the sprint prompt verbatim.
D-003 (P1) Action keeps an extra field `protected_extra` (task manifest test paths, absolute) beside the fields named in the prompt. Reason: policies judge single actions, and CODE-TEST-001 needs the manifest without extra verifier state.
D-004 (P1) Verdict adds `source` and `detail` beside the named fields for debugging; failed_policies contains "FAILCLOSED" when the verifier could not decide.
D-005 (P1) The Genome schema lives in proofread/contracts.py (shared by W4 and W5); genome/schema.py re-exports it and adds helpers. AddedPolicy is limited to three DSL kinds (deny_path_glob, deny_added_line_regex, deny_exec_regex) so added guardrails are purely additive.
D-006 (P1) Sandbox protocol adds checkpoint/restore (enforce-mode rollback) and export_files (pristine grader input) beside start/run/read/write/snapshot/stop.
