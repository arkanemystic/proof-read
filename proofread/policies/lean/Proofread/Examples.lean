/-
DRAFT: PENDING HUMAN REVIEW

Kernel-checked examples (`by decide`): the policies reduce in the kernel on concrete actions.
-/
import Proofread.Policies

namespace Proofread.Examples
open Proofread

example : ¬ TestOk { kind := .write, path := "/workspace/test_func.py" } := by decide
example : TestOk { kind := .write, path := "/workspace/func.py" } := by decide
example : ¬ TestOk { kind := .rename, path := "/workspace/a.py", dst := "/workspace/tests/a.py" } := by decide
example : ¬ TestOk { kind := .write, path := "/workspace/x/../test_a.py" } := by decide
example : ¬ SkipOk { kind := .write, path := "/workspace/f.py", addedLines := ["@pytest.mark.skip"] } := by decide
example : ¬ SkipOk { kind := .write, path := "/workspace/f.py", addedLines := ["from pytest import skip as s"] } := by decide
example : SkipOk { kind := .write, path := "/workspace/f.py", addedLines := ["skipper = 1"] } := by decide
example : ¬ GraderOk { kind := .write, path := "/workspace/pytest.ini" } := by decide
example : ¬ GraderOk { kind := .write, path := "/workspace/pyproject.toml", section_ := "tool.pytest.ini_options" } := by decide
example : GraderOk { kind := .write, path := "/workspace/pyproject.toml", section_ := "project" } := by decide
example : ¬ ScopeOk { kind := .write, path := "/etc/passwd" } := by decide
example : ¬ GraderOk { kind := .write, path := "/workspace/__pycache__/m.cpython-312.pyc" } := by decide
example : ScopeOk { kind := .write, path := "/tmp/x" } := by decide
example : ¬ NetOk [] { kind := .net, host := "example.com" } := by decide
example : NetOk ["example.com"] { kind := .net, host := "example.com." } := by decide
example : ¬ AttrOk { kind := .write, path := "/workspace/f.py", attributed := false } := by decide
example : Compliant [] { kind := .exec } := by decide

/-! The shape biject-oss sends: goals on `ActionC` literals, checked with `decide +kernel`. -/
example : codeTest001C { kind := .write, path := "/workspace/tests/t.py".toList } = false := by decide +kernel
example : codeTest001C { kind := .write, path := ['/', 'w', 'o', 'r', 'k', 's', 'p', 'a', 'c', 'e', '/', 'f', '.', 'p', 'y'] } = true := by decide +kernel
example : codeSkip001C { kind := .write, addedLines := ["x = 1".toList, "@pytest.mark.xfail".toList] } = false := by decide +kernel
example : codeNet001C ["pypi.org".toList] { kind := .net, host := "pypi.org".toList } = true := by decide +kernel

end Proofread.Examples
