"""Input, code-safety and output guardrails. No LLM calls in here."""
import ast
import io
import json
import re
import shutil
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from schemas import AnalysisResult, ColumnInfo, DatasetProfile


class GuardrailError(ValueError):
    """A user-facing rejection with a friendly message."""


# --------------------------------------------------------------------------- input
ALLOWED_EXTENSIONS = {".csv", ".xlsx"}
_BLOCKED_QUESTION = re.compile(
    r"\b(poem|story|joke|song|essay|lyrics|recipe|delete|remove (the )?files?|rm -rf|format (the )?disk|shutdown|"
    r"api[ _-]?key|password|secret|credential|\.env|environment variables?|system prompt|"
    r"ignore (all |the )?(previous|prior|above) instructions|install)\b",
    re.IGNORECASE,
)


def check_upload(file_name: str, size_bytes: int, max_mb: int) -> str:
    """Return the lower-case extension, or raise GuardrailError."""
    ext = Path(file_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise GuardrailError(f"Only .csv and .xlsx files are supported (got '{ext or 'no extension'}').")
    if size_bytes == 0:
        raise GuardrailError("The file is empty.")
    if size_bytes > max_mb * 1024 * 1024:
        raise GuardrailError(f"The file is {size_bytes / 1024 / 1024:.1f} MB; the limit is {max_mb} MB.")
    return ext


def excel_sheets(data: bytes) -> list[str]:
    return pd.ExcelFile(io.BytesIO(data), engine="openpyxl").sheet_names


def load_table(data: bytes, file_name: str, max_rows: int, sheet: str | None = None) -> pd.DataFrame:
    if Path(file_name).suffix.lower() == ".xlsx":
        df = pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0, engine="openpyxl")
    else:
        try:
            df = pd.read_csv(io.BytesIO(data))
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(data), encoding="latin-1")
        except pd.errors.EmptyDataError:
            raise GuardrailError("The file is empty.") from None
    if df.empty or len(df.columns) == 0:
        raise GuardrailError("The file has no data rows.")
    if len(df) > max_rows:
        raise GuardrailError(f"The file has {len(df):,} rows; the limit is {max_rows:,}.")
    return df


def normalise_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """strip / lower / snake_case column names. Returns (new df, {original: new})."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for i, col in enumerate(df.columns):
        new = re.sub(r"[^0-9a-z]+", "_", str(col).strip().lower()).strip("_") or f"column_{i + 1}"
        if new[0].isdigit():
            new = f"col_{new}"
        base, n = new, 2
        while new in used:
            new, n = f"{base}_{n}", n + 1
        used.add(new)
        mapping[str(col)] = new
    return df.rename(columns=dict(zip(df.columns, mapping.values()))), mapping


def build_profile(df: pd.DataFrame, file_name: str, sheet_name: str | None = None) -> DatasetProfile:
    columns, dates = [], []
    for col in df.columns:
        s = df[col]
        samples = [str(v) for v in s.dropna().unique()[:3]]
        columns.append(ColumnInfo(name=str(col), dtype=str(s.dtype), n_null=int(s.isna().sum()), sample_values=samples))
        if pd.api.types.is_datetime64_any_dtype(s):
            dates.append(s)
        elif not pd.api.types.is_numeric_dtype(s):
            parsed = pd.to_datetime(s, errors="coerce", format="mixed")
            if parsed.notna().mean() > 0.9:
                dates.append(parsed)
    date_range = None
    if dates:
        d = dates[0].dropna()
        date_range = (d.min().date().isoformat(), d.max().date().isoformat())
    return DatasetProfile(
        file_name=file_name, sheet_name=sheet_name, n_rows=len(df), columns=columns, date_range=date_range
    )


def check_question(question: str) -> str:
    """Return the cleaned question, or raise GuardrailError with a friendly message."""
    q = question.strip()
    if not 5 <= len(q) <= 500:
        raise GuardrailError("Please ask a question between 5 and 500 characters.")
    if _BLOCKED_QUESTION.search(q):
        raise GuardrailError(
            "I can only answer analysis questions about the uploaded data — for example "
            "'Why did Electronics revenue drop in September?'"
        )
    return q


# ------------------------------------------------------------------ code execution
ALLOWED_IMPORTS = {"pandas", "numpy", "matplotlib", "json", "pathlib", "datetime", "math", "statistics", "openpyxl"}
_BLOCKED_NAMES = {"eval", "exec", "compile", "__import__", "breakpoint", "input", "globals", "locals", "vars"}
_BLOCKED_ATTRS = {"system", "popen", "rmtree", "unlink", "rmdir", "chmod", "chown", "symlink_to", "hardlink_to"}
_WRITE_METHODS = {"savefig", "to_csv", "to_excel", "to_json", "to_parquet", "write_text", "write_bytes"}
# Path-like absolute strings only: "/etc", "\\server", "C:\..", "~/x" — not matplotlib styles like hatch="//".
_ABS_PATH = re.compile(r"^(/[\w.~-]|\\\\\w|\\[\w.]|[A-Za-z]:[\\/]|~[\\/])")


def _string_constants(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _targets_outputs(node: ast.AST, assigned: dict[str, ast.AST], depth: int = 0) -> bool:
    """True if a write-target expression is built from a literal starting with 'outputs'."""
    if any(s.replace("\\", "/").startswith("outputs") for s in _string_constants(node)):
        return True
    if depth < 3:
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id in assigned:
                if _targets_outputs(assigned[n.id], assigned, depth + 1):
                    return True
    return False


def _write_mode(call: ast.Call) -> bool:
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and bool(set(mode.value) & set("wax+"))


def check_code_safety(code: str) -> list[str]:
    """Static AST check. Returns a list of violations (empty = safe to run)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError: {e.msg} (line {e.lineno})"]

    assigned: dict[str, ast.AST] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned[t.id] = n.value

    problems: list[str] = []
    for n in ast.walk(tree):
        line = getattr(n, "lineno", "?")
        if isinstance(n, ast.Import | ast.ImportFrom):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            if isinstance(n, ast.ImportFrom) and n.level:
                problems.append(f"line {line}: relative imports are not allowed")
            for name in names:
                if name.split(".")[0] not in ALLOWED_IMPORTS:
                    problems.append(f"line {line}: import of '{name}' is not allowed (allowed: {sorted(ALLOWED_IMPORTS)})")
        elif isinstance(n, ast.Name) and n.id in _BLOCKED_NAMES | {"__builtins__"}:
            problems.append(f"line {line}: use of '{n.id}' is not allowed")
        elif isinstance(n, ast.Attribute):
            if n.attr in _BLOCKED_ATTRS:
                problems.append(f"line {line}: '.{n.attr}' is not allowed")
            elif n.attr.startswith("__") and n.attr.endswith("__"):
                problems.append(f"line {line}: dunder attribute '{n.attr}' is not allowed")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value.strip()
            if _ABS_PATH.match(v) and len(v) > 1 and "\n" not in v:
                problems.append(f"line {line}: absolute path '{v[:60]}' is not allowed; use 'data/...' or 'outputs/...'")
            if re.search(r"(^|[\\/])\.\.([\\/]|$)", v):
                problems.append(f"line {line}: '..' in paths is not allowed")
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "open" and _write_mode(n):
                if not n.args or not _targets_outputs(n.args[0], assigned):
                    problems.append(f"line {line}: open() for writing is only allowed inside outputs/")
            elif isinstance(f, ast.Attribute) and f.attr in _WRITE_METHODS:
                target = n.args[0] if n.args else next(
                    (k.value for k in n.keywords if k.arg in {"fname", "path_or_buf", "excel_writer", "path", "data"}),
                    None,
                )
                # no target = returns a string (e.g. df.to_csv()) and writes nothing
                if target is not None and not _targets_outputs(target, assigned):
                    problems.append(f"line {line}: .{f.attr}() may only write inside outputs/")
            elif isinstance(f, ast.Attribute) and f.attr == "open" and _write_mode(n):
                if not _targets_outputs(f.value, assigned):
                    problems.append(f"line {line}: Path.open() for writing is only allowed inside outputs/")
    return list(dict.fromkeys(problems))


def prepare_sandbox(work_dir: Path, df: pd.DataFrame, file_name: str) -> str:
    """Write a CSV copy of the (column-normalised) upload to sandbox/data/ — the original is never touched —
    and empty sandbox/outputs/. Returns the path relative to the sandbox, e.g. 'data/sales_2026.csv'."""
    data_dir = work_dir / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    clear_outputs(work_dir)
    safe_stem = re.sub(r"[^0-9A-Za-z_-]+", "_", Path(file_name).stem).strip("_") or "data"
    df.to_csv(data_dir / f"{safe_stem}.csv", index=False)
    return f"data/{safe_stem}.csv"


def clear_outputs(work_dir: Path) -> None:
    out = work_dir / "outputs"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)


# -------------------------------------------------------------------------- output
def load_result(path: Path) -> tuple[AnalysisResult | None, str | None]:
    """Validate outputs/result.json. Returns (result, None) or (None, error message for the agents)."""
    if not path.exists():
        return None, "outputs/result.json was not written."
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"outputs/result.json is not valid JSON: {e}"
    try:
        result = AnalysisResult.model_validate(raw)
    except ValidationError as e:
        lines = [f"- {'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        return None, "result.json failed AnalysisResult validation:\n" + "\n".join(lines)
    missing = [c.path for c in result.charts if not (path.parent.parent / c.path).exists()]
    if missing:
        return None, f"result.json lists charts that were not saved: {missing}"
    return result, None


_NUMBER = re.compile(r"(?<![A-Za-z_\d.])[-−]?\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> list[str]:
    return [m.group(0) for m in _NUMBER.finditer(text)]


def _to_float(token: str) -> float:
    return float(token.replace(",", "").replace("−", "-"))


def ungrounded_numbers(text: str, result: AnalysisResult) -> list[str]:
    """Numbers in `text` that cannot be traced to result.json (allowing rounding, sign, %, lakh/crore)."""
    base = [*result.metrics.values(), *(f.value for f in result.findings)]
    for s in [result.question, *result.metrics, *(f.label for f in result.findings), *(c.title for c in result.charts)]:
        base.extend(_to_float(t) for t in _numbers_in(s))
    candidates = set()
    for v in base:
        for scaled in (v, v * 100, v / 100, v / 1e3, v / 1e5, v / 1e7):
            candidates.update((scaled, abs(scaled)))

    bad = []
    for token in _numbers_in(text):
        n = abs(_to_float(token))
        if n.is_integer() and (n <= 12 or 1900 <= n <= 2100):  # list numbering, months, years
            continue
        decimals = len(token.split(".")[1]) if "." in token else 0
        tol = 0.5 * 10 ** -decimals + 1e-9
        if not any(abs(n - c) <= tol or (c and abs(n - c) / abs(c) < 0.005) for c in candidates):
            bad.append(token)
    return bad


def same_results(a: AnalysisResult, b: AnalysisResult) -> bool:
    """Reproducibility: identical metrics and findings."""
    return a.metrics == b.metrics and [f.model_dump() for f in a.findings] == [f.model_dump() for f in b.findings]
