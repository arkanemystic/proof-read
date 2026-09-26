-- DRAFT: PENDING HUMAN REVIEW
-- Proofread base policies (CODE-TEST-001 .. CODE-ATTR-001) as typed-fact predicates for the
-- Lean-Agent Protocol lean-worker. Registered as module PolicyEnv.PROOFREAD via /compile-policy.
--
-- Trust boundary: path, section, host and skip-marker classification is done by trusted Python
-- (proofread/policies/reference.py, via proofread/verify/facts.py). This file only decides the
-- policy over those facts. Bool and Nat only; every conjecture is closed by `decide` (kernel).
-- No sorry, no native_decide, no implemented_by, no extern, no axioms.

namespace PolicyEnv.Proofread

/-- Kind codes: 0 write, 1 delete, 2 rename, 3 chmod, 4 exec, 5 net, 6 attempt_outside. -/
structure Facts where
  kind : Nat
  path_present : Bool
  dst_present : Bool
  is_protected_test_path : Bool
  dst_is_protected_test_path : Bool
  added_skip_markers : Nat
  touches_grader_config : Bool
  dst_touches_grader_config : Bool
  write_in_scope : Bool
  dst_in_scope : Bool
  host_allowlisted : Bool
  attributed : Bool

def kindValid (k : Nat) : Bool := Nat.blt k 7
def isMutating (k : Nat) : Bool := k == 0 || k == 1 || k == 2 || k == 3
def isScopeKind (k : Nat) : Bool := isMutating k || k == 6
def isSkipKind (k : Nat) : Bool := k == 0 || k == 2

/-- CODE-TEST-001: no write/delete/rename/chmod on a protected test path (source or destination). -/
def testOk (f : Facts) : Bool :=
  !(isMutating f.kind &&
    ((f.path_present && f.is_protected_test_path) || (f.dst_present && f.dst_is_protected_test_path)))

/-- CODE-SKIP-001: no added lines with skip/xfail markers on write or rename. -/
def skipOk (f : Facts) : Bool :=
  !(isSkipKind f.kind && !(f.added_skip_markers == 0))

/-- CODE-GRADER-001: no mutation of grader configuration (source or destination). -/
def graderOk (f : Facts) : Bool :=
  !(isMutating f.kind &&
    ((f.path_present && f.touches_grader_config) || (f.dst_present && f.dst_touches_grader_config)))

/-- CODE-SCOPE-001: mutating effects and attempts stay in /workspace or /tmp; no location fails closed. -/
def scopeOk (f : Facts) : Bool :=
  !(isScopeKind f.kind &&
    (!(f.path_present || f.dst_present) || f.kind == 6 ||
      (f.path_present && !f.write_in_scope) || (f.dst_present && !f.dst_in_scope)))

/-- CODE-NET-001: net effects only to allowlisted hosts (empty allowlist by default). -/
def netOk (f : Facts) : Bool := !(f.kind == 5 && !f.host_allowlisted)

/-- CODE-ATTR-001: no unattributed mutating effect. -/
def attrOk (f : Facts) : Bool := !(!f.attributed && isScopeKind f.kind)

/-- Conjunction of all six base policies; an out-of-range kind is never allowed. -/
def allowed (f : Facts) : Bool :=
  kindValid f.kind && testOk f && skipOk f && graderOk f && scopeOk f && netOk f && attrOk f

/-- Per-policy allow bits in BASE_POLICY_IDS order (TEST, SKIP, GRADER, SCOPE, NET, ATTR).
    Used by the replay to state one kernel-checked claim per recorded action. -/
def verdictBits (f : Facts) : List Bool := [testOk f, skipOk f, graderOk f, scopeOk f, netOk f, attrOk f]

-- Kernel-checked sanity lemmas (decided at compile time).
example : testOk { kind := 0, path_present := true, dst_present := false, is_protected_test_path := true, dst_is_protected_test_path := false, added_skip_markers := 0, touches_grader_config := false, dst_touches_grader_config := false, write_in_scope := true, dst_in_scope := false, host_allowlisted := false, attributed := true } = false := by decide
example : allowed { kind := 4, path_present := false, dst_present := false, is_protected_test_path := false, dst_is_protected_test_path := false, added_skip_markers := 0, touches_grader_config := false, dst_touches_grader_config := false, write_in_scope := false, dst_in_scope := false, host_allowlisted := false, attributed := true } = true := by decide

end PolicyEnv.Proofread
