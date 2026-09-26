/-
DRAFT: PENDING HUMAN REVIEW

Path canonicalization (posixpath.normpath semantics) and classification over raw strings.
Input paths are NFC-normalized by the bridge; everything else happens here.
-/
import Proofread.Strings

namespace Proofread.Paths
open Proofread.Str

def normStep (st : List Chars) (c : Chars) : List Chars :=
  if c == [] || c == lit "." then st
  else if c == lit ".." then st.tail
  else c :: st

def joinSlash : List Chars → Chars
  | [] => []
  | [c] => c
  | c :: cs => c ++ '/' :: joinSlash cs

/-- canon_path: drop NUL, resolve relative against /workspace, collapse `.`, `..`, `//`. -/
def canon (p : Chars) : Chars :=
  let cs := p.filter (· != '\x00')
  match cs with
  | [] => []
  | c :: _ =>
    let abs := if c == '/' then cs else lit "/workspace/" ++ cs
    let st := (splitOn '/' abs).foldl normStep []
    '/' :: joinSlash st.reverse

def parts (p : Chars) : List Chars := (splitOn '/' p).filter (· != [])

def isUnder (p root : Chars) : Bool := p == root || isPrefix (root ++ ['/']) p

def workspace : Chars := lit "/workspace"
def tmp : Chars := lit "/tmp"

def basename (p : Chars) : Chars := (parts p).getLast?.getD []

def isTestBasename (b : Chars) : Bool :=
  (isPrefix (lit "test_") b && isSuffix (lit ".py") b && b.length ≥ 8)
  || isSuffix (lit "_test.py") b
  || b == lit "conftest.py"

/-- `p` is canonical. Manifest entries are canonicalized (non-empty raw entries only). -/
def isProtectedTest (manifest : List Chars) (p : Chars) : Bool :=
  p != [] &&
  (((manifest.filter (· != [])).map canon).contains p
   || (isUnder p workspace &&
       (isTestBasename (basename p) || ((parts p).drop 1).dropLast.contains (lit "tests"))))

def graderBasenames : List Chars :=
  ["pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "noxfile.py", "Makefile",
   "makefile", "GNUmakefile", "sitecustomize.py", "usercustomize.py"].map lit

def initTestDirs : List Chars := ["tests", "test", "testing"].map lit

def sections (s : Chars) : List Chars :=
  if s == [] then [[]] else (splitOn ',' s).map strip

def setupCfgHit (sec : Chars) : Bool :=
  (sections sec).any (fun x => x == lit "tool:pytest" || x == lit "<top>" || x == [])

def pyprojectHit (sec : Chars) : Bool :=
  (sections sec).any (fun x => isPrefix (lit "tool.pytest") x || x == lit "<top>" || x == [])

/-- `p` is canonical. -/
def isGraderPath (p sec : Chars) : Bool :=
  let ps := parts p
  let b := ps.getLast?.getD []
  let ds := ps.dropLast
  p != [] &&
  (graderBasenames.contains b || isSuffix (lit ".pth") b || isSuffix (lit ".pyc") b
   || isSuffix (lit ".pyo") b
   || ds.contains (lit ".github")
   || (b == lit "__init__.py" && ds.any initTestDirs.contains)
   || (b == lit "setup.cfg" && setupCfgHit sec)
   || (b == lit "pyproject.toml" && pyprojectHit sec))

def inScope (p : Chars) : Bool := p != [] && (isUnder p workspace || isUnder p tmp)

end Proofread.Paths
