/-
DRAFT: PENDING HUMAN REVIEW

Normalized effect record, mirroring proofread.contracts.Action (only the fields policies read).
`addedLinesNfkc` carries the NFKC fold of `addedLines` (computed by the bridge).
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

end Proofread
