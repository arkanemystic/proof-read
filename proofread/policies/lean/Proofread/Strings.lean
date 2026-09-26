/-
DRAFT: PENDING HUMAN REVIEW

Raw string helpers over `List Char`, structural recursion only, so every function reduces in the
kernel (`decide`) and uses no custom axioms.
-/
namespace Proofread.Str

abbrev Chars := List Char

/-- `p` is a prefix of `s`. -/
def isPrefix : Chars → Chars → Bool
  | [], _ => true
  | _ :: _, [] => false
  | a :: p, b :: s => a == b && isPrefix p s

def isSuffix (p s : Chars) : Bool := isPrefix p.reverse s.reverse

def lit (s : String) : Chars := s.toList

/-- Exactly Python's `str.isspace()` set (equal to `re` `\s` for str patterns). -/
def isWs (c : Char) : Bool :=
  let n := c.toNat
  (9 ≤ n && n ≤ 13) || (28 ≤ n && n ≤ 32) || n == 0x85 || n == 0xa0 || n == 0x1680
  || (0x2000 ≤ n && n ≤ 0x200a) || n == 0x2028 || n == 0x2029 || n == 0x202f || n == 0x205f
  || n == 0x3000

/-- `re` `\w` on ASCII is `[A-Za-z0-9_]`. Non-ASCII, non-space characters count as word
characters here; the bridge maps every non-ASCII non-word character to U+0001 beforehand
(Unicode category tables are not available in Lean core). -/
def isWord (c : Char) : Bool :=
  let n := c.toNat
  (48 ≤ n && n ≤ 57) || (65 ≤ n && n ≤ 90) || (97 ≤ n && n ≤ 122) || n == 95
  || (n ≥ 128 && !isWs c)

def dropWs : Chars → Chars
  | [] => []
  | c :: s => if isWs c then dropWs s else c :: s

/-- `\s+`: at least one whitespace char, then the rest after all leading whitespace. -/
def dropWs1 : Chars → Option Chars
  | [] => none
  | c :: s => if isWs c then some (dropWs s) else none

def strip (s : Chars) : Chars := (dropWs (dropWs s).reverse).reverse

def dropWhileEq (c : Char) : Chars → Chars
  | [] => []
  | d :: s => if d == c then dropWhileEq c s else d :: s

/-- Maximal leading run satisfying `p`, and the rest. -/
def span (p : Char → Bool) : Chars → Chars × Chars
  | [] => ([], [])
  | c :: s => if p c then let r := span p s; (c :: r.1, r.2) else ([], c :: s)

def takeLine : Chars → Chars
  | [] => []
  | c :: s => if c == '\n' then [] else c :: takeLine s

/-- Split on a separator character, keeping empty segments (Python `str.split(sep)`). -/
def splitOn (sep : Char) : Chars → List Chars
  | [] => [[]]
  | c :: s =>
    let rest := splitOn sep s
    if c == sep then [] :: rest
    else match rest with
      | [] => [[c]]
      | r :: rs => (c :: r) :: rs

/-- Some suffix position satisfies `f prev suffix` (`prev` = the char before the position). -/
def anyPos (f : Option Char → Chars → Bool) : Option Char → Chars → Bool
  | prev, [] => f prev []
  | prev, c :: s => f prev (c :: s) || anyPos f (some c) s

def nonWordPrev : Option Char → Bool
  | none => true
  | some c => !isWord c

end Proofread.Str
