/-
DRAFT: PENDING HUMAN REVIEW

Skip/xfail marker detection over raw lines. Each `pN` below is a hand-written matcher equal to
one regex of reference.SKIP_PATTERNS, anchored at a position (`prev` = preceding char, for `\b`).
Lines arrive NFC (raw) and NFKC-folded from the bridge, with non-ASCII non-word chars mapped to
U+0001; all pattern literals are ASCII so the mapping preserves every match.
-/
import Proofread.Strings

namespace Proofread.Markers
open Proofread.Str

/-- The maximal word run at the head of `s` equals one of `ws` (i.e. `(w1|w2..)\b`). -/
def tokenIn (ws : List String) (s : Chars) : Bool := ws.any (fun w => (span isWord s).1 == lit w)

/-- `lit \s* \. \s* (alts)\b` at the head of `s`. -/
def dotted (head : String) (alts : List String) (s : Chars) : Bool :=
  isPrefix (lit head) s &&
  match dropWs (s.drop head.length) with
  | '.' :: r => tokenIn alts (dropWs r)
  | _ => false

-- \bmark\s*\.\s*(skip|skipif|xfail)\b
def p1 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev && dotted "mark" ["skip", "skipif", "xfail"] s
-- \bskipif\b
def p2 (prev : Option Char) (s : Chars) : Bool := nonWordPrev prev && tokenIn ["skipif"] s
-- \bxfail\b
def p3 (prev : Option Char) (s : Chars) : Bool := nonWordPrev prev && tokenIn ["xfail"] s
-- \bimportorskip\b
def p4 (prev : Option Char) (s : Chars) : Bool := nonWordPrev prev && tokenIn ["importorskip"] s
-- \bpytest\s*\.\s*(skip|exit)\b
def p5 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev && dotted "pytest" ["skip", "exit"] s
-- \.\s*skip\s*\(
def p6 (_prev : Option Char) (s : Chars) : Bool :=
  match s with
  | '.' :: r =>
    let r1 := dropWs r
    isPrefix (lit "skip") r1 && isPrefix ['('] (dropWs (r1.drop 4))
  | _ => false
-- \bunittest\s*\.\s*(skip|skipIf|skipUnless|expectedFailure|SkipTest)\b
def p7 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev && dotted "unittest" ["skip", "skipIf", "skipUnless", "expectedFailure", "SkipTest"] s
-- \b(skipIf|skipUnless|expectedFailure|skipTest)\b
def p8 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev && tokenIn ["skipIf", "skipUnless", "expectedFailure", "skipTest"] s
-- \bSkipTest\b
def p9 (prev : Option Char) (s : Chars) : Bool := nonWordPrev prev && tokenIn ["SkipTest"] s
-- \b(raise\s+Skipped|Skipped\s*\()
def p10 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev &&
  ((isPrefix (lit "raise") s &&
      match dropWs1 (s.drop 5) with
      | some r => isPrefix (lit "Skipped") r
      | none => false)
   || (isPrefix (lit "Skipped") s && isPrefix ['('] (dropWs (s.drop 7))))
-- __unittest_skip__
def p11 (_prev : Option Char) (s : Chars) : Bool := isPrefix (lit "__unittest_skip__") s
-- \b_pytest\s*\.\s*(outcomes|skipping)\b
def p12 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev && dotted "_pytest" ["outcomes", "skipping"] s

def isModChar (c : Char) : Bool := isWord c || c == '.'

/-- `(pytest|unittest|_pytest[\w.]*)\s+` at the head of `s`; returns the rest after `\s+`. -/
def module (s : Chars) : Option Chars :=
  let (m, rest) := span isModChar s
  if m == lit "pytest" || m == lit "unittest" || isPrefix (lit "_pytest") m then dropWs1 rest
  else none

/-- `word\b` at the head of `s`, returning the rest. -/
def keyword (w : String) (s : Chars) : Option Chars :=
  if isPrefix (lit w) s then
    let r := s.drop w.length
    match r with
    | [] => some []
    | c :: _ => if isWord c then none else some r
  else none

-- \bfrom\s+(pytest|unittest|_pytest[\w.]*)\s+import\b.*\b(skip|mark|exit)\b
def p13 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev && isPrefix (lit "from") s &&
  match dropWs1 (s.drop 4) >>= module >>= keyword "import" with
  | some r => anyPos (fun pv t => nonWordPrev pv && tokenIn ["skip", "mark", "exit"] t) (some 't')
                (takeLine r)
  | none => false
-- \bimport\s+(pytest|unittest|_pytest[\w.]*)\s+as\b
def p14 (prev : Option Char) (s : Chars) : Bool :=
  nonWordPrev prev && isPrefix (lit "import") s &&
  (dropWs1 (s.drop 6) >>= module >>= keyword "as").isSome

def matchHere (prev : Option Char) (s : Chars) : Bool :=
  p1 prev s || p2 prev s || p3 prev s || p4 prev s || p5 prev s || p6 prev s || p7 prev s
  || p8 prev s || p9 prev s || p10 prev s || p11 prev s || p12 prev s || p13 prev s || p14 prev s

def lineHasSkip (line : String) : Bool := anyPos matchHere none line.toList

end Proofread.Markers
