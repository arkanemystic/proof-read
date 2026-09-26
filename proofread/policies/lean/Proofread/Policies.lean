/-
DRAFT: PENDING HUMAN REVIEW

The six base policies as computable Bool functions over one Action (true = compliant), with Prop
wrappers and Decidable instances. Semantics mirror proofread/policies/reference.py.
-/
import Proofread.Action
import Proofread.Paths
import Proofread.Markers

namespace Proofread
open Proofread.Str Proofread.Paths Proofread.Markers

def Kind.isMutating : Kind → Bool
  | .write | .delete | .rename | .chmod => true
  | _ => false

def Kind.isScope (k : Kind) : Bool := k.isMutating || k == .attemptOutside

def Kind.isSkip : Kind → Bool
  | .write | .rename => true
  | _ => false

/-! Policies on `ActionC` (strings as `List Char`). -/

namespace ActionC

/-- Canonical targets: path and dst when non-empty. -/
def targets (a : ActionC) : List Chars := ([a.path, a.dst].filter (· != [])).map canon

end ActionC

/-- CODE-TEST-001 -/
def codeTest001C (a : ActionC) : Bool :=
  !(a.kind.isMutating && a.targets.any (isProtectedTest a.protectedExtra))

/-- CODE-SKIP-001 -/
def codeSkip001C (a : ActionC) : Bool :=
  !(a.kind.isSkip && (a.addedLines.any lineHasSkipC || a.addedLinesNfkc.any lineHasSkipC))

/-- CODE-GRADER-001 -/
def codeGrader001C (a : ActionC) : Bool :=
  !(a.kind.isMutating && a.targets.any (fun p => isGraderPath p a.section_))

/-- CODE-SCOPE-001 (a scoped kind with no location fails closed). -/
def codeScope001C (a : ActionC) : Bool :=
  !(a.kind.isScope &&
    (a.targets.isEmpty || a.kind == .attemptOutside || a.targets.any (fun p => !inScope p)))

/-- Host normal form: strip whitespace, drop trailing dots (lowercasing is done by the bridge). -/
def normHostC (h : Chars) : Chars := (dropWhileEq '.' (strip h).reverse).reverse

/-- CODE-NET-001 -/
def codeNet001C (allow : List Chars) (a : ActionC) : Bool :=
  match a.kind with
  | .net =>
    let h := normHostC a.host
    h != [] && (allow.map normHostC).contains h
  | _ => true

/-- CODE-ATTR-001 -/
def codeAttr001C (a : ActionC) : Bool := !(!a.attributed && a.kind.isScope)

/-! The same policies on `Action`, by definition `policyC ∘ Action.toC`. -/

def normHost (h : String) : Chars := normHostC h.toList

def codeTest001 (a : Action) : Bool := codeTest001C a.toC
def codeSkip001 (a : Action) : Bool := codeSkip001C a.toC
def codeGrader001 (a : Action) : Bool := codeGrader001C a.toC
def codeScope001 (a : Action) : Bool := codeScope001C a.toC
def codeNet001 (allow : List String) (a : Action) : Bool := codeNet001C (allow.map String.toList) a.toC
def codeAttr001 (a : Action) : Bool := codeAttr001C a.toC

/-- Failed policy ids. -/
def failed (allow : List String) (a : Action) : List String :=
  let checks : List (String × Bool) :=
    [("CODE-ATTR-001", codeAttr001 a), ("CODE-GRADER-001", codeGrader001 a),
     ("CODE-NET-001", codeNet001 allow a), ("CODE-SCOPE-001", codeScope001 a),
     ("CODE-SKIP-001", codeSkip001 a), ("CODE-TEST-001", codeTest001 a)]
  (checks.filter (fun c => !c.2)).map Prod.fst

def allOk (allow : List String) (a : Action) : Bool := (failed allow a).isEmpty

/-- CODE-SKIP-001 depends on the added lines only through the set of lines (raw and NFKC together).
So it equals the conjunction of the policy over any list of chunks that covers exactly that set.
biject-oss relies on this to split a large write into several kernel conjectures. -/
theorem codeSkip001C_chunks (a : ActionC) (cs : List (List Chars))
    (hcov : ∀ x, x ∈ a.addedLines ++ a.addedLinesNfkc ↔ ∃ c ∈ cs, x ∈ c) :
    codeSkip001C a = cs.all (fun c => codeSkip001C { kind := a.kind, addedLines := c }) := by
  simp only [codeSkip001C]
  cases hk : a.kind.isSkip
  · simp
  · apply Bool.eq_iff_iff.mpr
    simp only [Bool.true_and, Bool.not_or, Bool.and_eq_true, Bool.not_eq_true', List.any_eq_false,
      List.all_eq_true, List.any_nil, Bool.or_false]
    constructor
    · rintro ⟨h1, h2⟩ c hc x hx
      rcases List.mem_append.mp ((hcov x).mpr ⟨c, hc, hx⟩) with h | h
      · exact h1 x h
      · exact h2 x h
    · intro h
      refine ⟨fun x hx => ?_, fun x hx => ?_⟩
      · obtain ⟨c, hc, hxc⟩ := (hcov x).mp (List.mem_append_left _ hx)
        exact h c hc x hxc
      · obtain ⟨c, hc, hxc⟩ := (hcov x).mp (List.mem_append_right _ hx)
        exact h c hc x hxc

/-! Prop wrappers with Decidable instances. -/

def TestOk (a : Action) : Prop := codeTest001 a = true
def SkipOk (a : Action) : Prop := codeSkip001 a = true
def GraderOk (a : Action) : Prop := codeGrader001 a = true
def ScopeOk (a : Action) : Prop := codeScope001 a = true
def NetOk (allow : List String) (a : Action) : Prop := codeNet001 allow a = true
def AttrOk (a : Action) : Prop := codeAttr001 a = true
def Compliant (allow : List String) (a : Action) : Prop := allOk allow a = true

instance (a : Action) : Decidable (TestOk a) := inferInstanceAs (Decidable (_ = true))
instance (a : Action) : Decidable (SkipOk a) := inferInstanceAs (Decidable (_ = true))
instance (a : Action) : Decidable (GraderOk a) := inferInstanceAs (Decidable (_ = true))
instance (a : Action) : Decidable (ScopeOk a) := inferInstanceAs (Decidable (_ = true))
instance (l : List String) (a : Action) : Decidable (NetOk l a) :=
  inferInstanceAs (Decidable (_ = true))
instance (a : Action) : Decidable (AttrOk a) := inferInstanceAs (Decidable (_ = true))
instance (l : List String) (a : Action) : Decidable (Compliant l a) :=
  inferInstanceAs (Decidable (_ = true))

end Proofread
