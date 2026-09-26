/-
DRAFT: PENDING HUMAN REVIEW

Normalized effect record, mirroring proofread.contracts.Action (only the fields policies read).
`addedLinesNfkc` carries the NFKC fold of `addedLines` (computed by the bridge).

`ActionC` is the same record with every string as `List Char`. The policies are computed on
`ActionC`; the `String` versions are `policy ∘ Action.toC`. Kernel conjectures (biject-oss) state
goals directly on `ActionC` literals, because reducing `String.toList` on a UTF-8 string literal in
the kernel costs far more than the policy itself.
-/
namespace Proofread

inductive Kind where
  | write | delete | rename | chmod | exec | net | attemptOutside
  deriving DecidableEq, Repr, Inhabited

def Kind.ofString? : String → Option Kind
  | "write" => some .write
  | "delete" => some .delete
  | "rename" => some .rename
  | "chmod" => some .chmod
  | "exec" => some .exec
  | "net" => some .net
  | "attempt_outside" => some .attemptOutside
  | _ => none

structure Action where
  kind : Kind
  path : String := ""
  dst : String := ""
  addedLines : List String := []
  addedLinesNfkc : List String := []
  section_ : String := ""
  host : String := ""
  attributed : Bool := true
  protectedExtra : List String := []
  deriving Repr, Inhabited

structure ActionC where
  kind : Kind
  path : List Char := []
  dst : List Char := []
  addedLines : List (List Char) := []
  addedLinesNfkc : List (List Char) := []
  section_ : List Char := []
  host : List Char := []
  attributed : Bool := true
  protectedExtra : List (List Char) := []
  deriving Repr, Inhabited

def Action.toC (a : Action) : ActionC :=
  { kind := a.kind, path := a.path.toList, dst := a.dst.toList,
    addedLines := a.addedLines.map String.toList, addedLinesNfkc := a.addedLinesNfkc.map String.toList,
    section_ := a.section_.toList, host := a.host.toList, attributed := a.attributed,
    protectedExtra := a.protectedExtra.map String.toList }

end Proofread
