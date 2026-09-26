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

/-- Canonical targets: path and dst when non-empty. -/
def Action.targets (a : Action) : List Chars :=
  ([a.path.toList, a.dst.toList].filter (· != [])).map canon

/-- CODE-TEST-001 -/
def codeTest001 (a : Action) : Bool :=
  !(a.kind.isMutating && a.targets.any (isProtectedTest (a.protectedExtra.map String.toList)))

/-- CODE-SKIP-001 -/
def codeSkip001 (a : Action) : Bool :=
  !(a.kind.isSkip && (a.addedLines.any lineHasSkip || a.addedLinesNfkc.any lineHasSkip))

/-- CODE-GRADER-001 -/
def codeGrader001 (a : Action) : Bool :=
  !(a.kind.isMutating && a.targets.any (fun p => isGraderPath p a.section_.toList))

/-- CODE-SCOPE-001 (a scoped kind with no location fails closed). -/
def codeScope001 (a : Action) : Bool :=
  !(a.kind.isScope &&
    (a.targets.isEmpty || a.kind == .attemptOutside || a.targets.any (fun p => !inScope p)))

/-- Host normal form: strip whitespace, drop trailing dots (lowercasing is done by the bridge). -/
def normHost (h : String) : Chars := (dropWhileEq '.' (strip h.toList).reverse).reverse

/-- CODE-NET-001 -/
def codeNet001 (allow : List String) (a : Action) : Bool :=
  match a.kind with
  | .net =>
    let h := normHost a.host
    h != [] && (allow.map normHost).contains h
  | _ => true

/-- CODE-ATTR-001 -/
def codeAttr001 (a : Action) : Bool := !(!a.attributed && a.kind.isScope)

/-- Failed policy ids. -/
def failed (allow : List String) (a : Action) : List String :=
  let checks : List (String × Bool) :=
    [("CODE-ATTR-001", codeAttr001 a), ("CODE-GRADER-001", codeGrader001 a),
     ("CODE-NET-001", codeNet001 allow a), ("CODE-SCOPE-001", codeScope001 a),
     ("CODE-SKIP-001", codeSkip001 a), ("CODE-TEST-001", codeTest001 a)]
  (checks.filter (fun c => !c.2)).map Prod.fst

def allOk (allow : List String) (a : Action) : Bool := (failed allow a).isEmpty

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
