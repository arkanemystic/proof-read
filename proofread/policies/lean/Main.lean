/-
DRAFT: PENDING HUMAN REVIEW

policycheck: JSON-lines bridge. Each stdin line is
  {"id": <any>, "allow": [str], "action": <contracts.Action JSON>}
and produces one stdout line {"id": <same>, "failed": [policy ids]} or {"id":..., "error": str}.
This file is IO glue only; the policies live in Proofread/*.lean.
-/
import Lean.Data.Json
import Proofread

open Lean Proofread

def getStr (j : Json) (k : String) : Except String String :=
  match j.getObjVal? k with
  | .ok (.str s) => .ok s
  | .ok .null => .ok ""
  | .ok _ => .error s!"field {k}: expected string"
  | .error _ => .ok ""

def getStrList (j : Json) (k : String) : Except String (List String) :=
  match j.getObjVal? k with
  | .ok (.arr xs) => xs.toList.mapM (fun x => match x with
      | .str s => .ok s
      | _ => .error s!"field {k}: expected string array")
  | .ok .null => .ok []
  | .ok _ => .error s!"field {k}: expected array"
  | .error _ => .ok []

def getBool (j : Json) (k : String) (d : Bool) : Except String Bool :=
  match j.getObjVal? k with
  | .ok (.bool b) => .ok b
  | .ok _ => .error s!"field {k}: expected bool"
  | .error _ => .ok d

def parseAction (j : Json) : Except String Action := do
  let k ← getStr j "kind"
  let some kind := Kind.ofString? k | throw s!"unknown kind {k}"
  return {
    kind := kind
    path := ← getStr j "path"
    dst := ← getStr j "dst"
    addedLines := ← getStrList j "added_lines"
    addedLinesNfkc := ← getStrList j "added_lines_nfkc"
    section_ := ← getStr j "section"
    host := ← getStr j "host"
    attributed := ← getBool j "attributed" true
    protectedExtra := ← getStrList j "protected_extra" }

def handle (line : String) : Json :=
  match Json.parse line with
  | .error e => Json.mkObj [("id", Json.null), ("error", s!"bad json: {e}")]
  | .ok j =>
    let id := (j.getObjVal? "id").toOption.getD Json.null
    let r : Except String (List String) := do
      let a ← parseAction (← j.getObjVal? "action")
      let allow ← getStrList j "allow"
      return failed allow a
    match r with
    | .ok fs => Json.mkObj [("id", id), ("failed", Json.arr (fs.map Json.str).toArray)]
    | .error e => Json.mkObj [("id", id), ("error", e)]

def main : IO Unit := do
  let stdin ← IO.getStdin
  let stdout ← IO.getStdout
  repeat
    let line ← stdin.getLine
    if line.isEmpty then break
    let t := line.trimAsciiEnd.copy
    if t.isEmpty then continue
    stdout.putStrLn (handle t).compress
    stdout.flush
