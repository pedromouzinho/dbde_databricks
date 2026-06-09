"""
Code Interpreter — executa codigo Python em sandbox seguro.
Usa subprocess com timeout, restricoes de imports, e directorio temporario.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import logging
import os
try:
    import resource
except ImportError:  # pragma: no cover - unavailable on some platforms
    resource = None
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Dict, Any, Optional, List

from config_databricks import (
    CODE_INTERPRETER_TIMEOUT,
    CODE_INTERPRETER_MAX_OUTPUT,
    CODE_INTERPRETER_ENABLED,
    CODE_INTERPRETER_MAX_INPUT_FILE_BYTES,
)

logger = logging.getLogger(__name__)

RESULT_MARKER = "__CODE_RESULT__:"
MAX_CODE_CHARS = 20000
MAX_RETURN_FILE_BYTES = 10_000_000
MAX_UPLOADED_FILE_BYTES = CODE_INTERPRETER_MAX_INPUT_FILE_BYTES
_MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin"
_CODE_CPU_LIMIT_SECONDS = 120
_CODE_MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024  # 1.5GB for Excel/PPTX

# Imports seguros permitidos.
ALLOWED_IMPORTS = {
    "math", "statistics", "decimal", "fractions",
    "datetime", "calendar", "time",
    "json", "csv", "re", "string", "textwrap",
    "collections", "itertools", "functools", "operator",
    "os.path",
    "random", "hashlib", "uuid",
    "pandas", "numpy", "matplotlib", "matplotlib.pyplot",
    "seaborn", "plotly", "plotly.express", "plotly.graph_objects",
    "openpyxl", "pyxlsb", "xlrd", "xlsxwriter", "python_calamine",
    "duckdb",
    "pdfplumber", "pypdf", "pdfminer", "docx",
    "base64", "copy", "pprint", "typing",
}

# Imports bloqueados por segurança.
BLOCKED_IMPORTS = {
    "subprocess", "shutil", "socket", "http", "urllib",
    "requests", "httpx", "aiohttp",
    "ctypes", "multiprocessing", "threading",
    "signal", "resource", "gc",
    "importlib", "builtins", "__builtins__",
    "pickle", "shelve", "marshal",
    "code", "codeop", "compileall",
    "webbrowser", "antigravity",
}

_BLOCKED_IMPORT_NAMES = {
    "system", "popen", "exec", "eval", "remove", "unlink", "rmdir",
    "rmtree", "Popen", "run", "call", "check_output", "check_call",
}
_BLOCKED_CALLS = {
    "exec", "eval", "compile", "__import__", "globals", "locals",
    "getattr", "setattr", "delattr",
}
_BLOCKED_ATTR_CALLS = {
    "os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir",
    "shutil.rmtree", "shutil.move", "shutil.copy", "subprocess.Popen",
    "subprocess.run", "subprocess.call", "pathlib.Path.unlink", "pathlib.Path.rmdir",
}


def _is_import_allowed(module_name: str) -> bool:
    if not module_name:
        return True
    mod = module_name.strip()
    blocked_match = any(mod == b or mod.startswith(f"{b}.") for b in BLOCKED_IMPORTS)
    if blocked_match:
        return False
    allowed_match = any(mod == a or mod.startswith(f"{a}.") for a in ALLOWED_IMPORTS)
    return allowed_match


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    return ""


def _validate_code(code: str) -> Optional[str]:
    """Valida o codigo antes de executar. Retorna erro ou None se OK."""
    if not code or not code.strip():
        return "Codigo vazio."
    if len(code) > MAX_CODE_CHARS:
        return "Codigo demasiado longo (max 20.000 chars)."

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Erro de sintaxe: {e.msg} (linha {e.lineno})"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = str(alias.name or "").strip()
                if not _is_import_allowed(mod):
                    return f"Import bloqueado por seguranca: {mod}"
        elif isinstance(node, ast.ImportFrom):
            mod = str(node.module or "").strip()
            if not _is_import_allowed(mod):
                return f"Import bloqueado por seguranca: {mod}"
            for alias in node.names or []:
                name = str(alias.name or "").strip()
                if name in _BLOCKED_IMPORT_NAMES:
                    return f"Import de funcao bloqueada por seguranca: from {mod} import {name}"
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _BLOCKED_CALLS:
                return f"Funcao bloqueada por seguranca: {name}"
            if name in _BLOCKED_ATTR_CALLS:
                return f"Funcao bloqueada por seguranca: {name}"

    lowered = code.lower()
    dangerous_snippets = [
        "open('/",
        "open(\"/",
        "path('/",
        "path(\"/",
    ]
    for snippet in dangerous_snippets:
        if snippet in lowered:
            return f"Acesso absoluto ao filesystem bloqueado por seguranca: {snippet}"
    return None


def _guess_mime(filename: str) -> str:
    fname = (filename or "").lower()
    if fname.endswith(".png"):
        return "image/png"
    if fname.endswith(".jpg") or fname.endswith(".jpeg"):
        return "image/jpeg"
    if fname.endswith(".svg"):
        return "image/svg+xml"
    if fname.endswith(".gif"):
        return "image/gif"
    if fname.endswith(".csv"):
        return "text/csv"
    if fname.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if fname.endswith(".xlsb"):
        return "application/vnd.ms-excel.sheet.binary.macroenabled.12"
    if fname.endswith(".xls"):
        return "application/vnd.ms-excel"
    if fname.endswith(".json"):
        return "application/json"
    if fname.endswith(".pdf"):
        return "application/pdf"
    if fname.endswith(".html") or fname.endswith(".htm"):
        return "text/html"
    return "application/octet-stream"


def _build_subprocess_env(tmpdir: str) -> Dict[str, str]:
    return {
        "PATH": _MINIMAL_PATH,
        "HOME": tmpdir,
        "TMPDIR": tmpdir,
        "PYTHONPATH": "",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLCONFIGDIR": tmpdir,
    }


def _set_resource_limits() -> None:
    """Limit CPU time and virtual memory for the child process."""
    if resource is None:
        return
    try:
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (_CODE_CPU_LIMIT_SECONDS, _CODE_CPU_LIMIT_SECONDS),
        )
        if hasattr(resource, "RLIMIT_AS"):
            resource.setrlimit(
                resource.RLIMIT_AS,
                (_CODE_MEMORY_LIMIT_BYTES, _CODE_MEMORY_LIMIT_BYTES),
            )
    except (AttributeError, ValueError, OSError):
        pass


def _path_within_root(path: str, root: str) -> bool:
    real_path = os.path.realpath(path)
    real_root = os.path.realpath(root)
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def _is_safe_symlink_source(path: str, root: str) -> bool:
    if not os.path.islink(path):
        return True
    return _path_within_root(path, root)


def _runner_script(tmpdir: str, code_b64: str) -> str:
    # Wrapper em subprocess isolado; bloqueia path absoluto e serializa resultado final.
    return textwrap.dedent(
        f"""
        import os
        import io
        import json
        import base64
        import shutil
        import traceback
        import contextlib
        import builtins

        TMPDIR = {tmpdir!r}
        os.chdir(TMPDIR)
        os.makedirs(os.path.join(TMPDIR, "mnt", "data"), exist_ok=True)
        _real_root = os.path.realpath(TMPDIR)
        for _name in list(os.listdir(TMPDIR)):
            _src = os.path.join(TMPDIR, _name)
            if not os.path.isfile(_src) or _name.startswith("_"):
                continue
            _real_src = os.path.realpath(_src)
            if os.path.islink(_src) and not (_real_src == _real_root or _real_src.startswith(_real_root + os.sep)):
                continue
            _dst = os.path.join(TMPDIR, "mnt", "data", _name)
            try:
                os.symlink(_src, _dst)
            except Exception:
                try:
                    shutil.copy2(_src, _dst)
                except Exception:
                    pass
        _before = set(os.listdir(TMPDIR))
        _generated_plot_files = []

        def _remap_data_path(path_like):
            try:
                s = os.fspath(path_like)
            except Exception:
                return path_like
            s = str(s)
            if s == "/mnt/data":
                return os.path.join("mnt", "data")
            if s.startswith("/mnt/data/"):
                return os.path.join("mnt", "data", s[len("/mnt/data/"):])
            return path_like

        def _safe_path(path_like):
            remapped = _remap_data_path(path_like)
            p = os.path.realpath(os.path.join(TMPDIR, str(remapped)))
            root = os.path.realpath(TMPDIR)
            if not (p == root or p.startswith(root + os.sep)):
                raise PermissionError("Acesso fora do sandbox nao permitido.")
            return p

        _orig_open = builtins.open
        def _safe_open(file, *args, **kwargs):
            return _orig_open(_safe_path(file), *args, **kwargs)
        builtins.open = _safe_open
        _orig_io_open = io.open
        def _safe_io_open(file, *args, **kwargs):
            return _orig_io_open(_safe_path(file), *args, **kwargs)
        io.open = _safe_io_open

        try:
            import pandas as _pd
            def _patch_reader(_fn_name):
                _fn = getattr(_pd, _fn_name, None)
                if not callable(_fn):
                    return
                def _wrapped(path_or_buf, *args, **kwargs):
                    return _fn(_remap_data_path(path_or_buf), *args, **kwargs)
                setattr(_pd, _fn_name, _wrapped)
            for _reader in ("read_csv", "read_excel", "read_table", "read_parquet", "read_feather"):
                _patch_reader(_reader)
        except Exception:
            pass

        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            _orig_show = plt.show
            def _patched_show(*args, **kwargs):
                fig = plt.gcf()
                fname = f"plot_{{len(_generated_plot_files)}}.png"
                fpath = _safe_path(fname)
                fig.savefig(fpath, dpi=150, bbox_inches="tight")
                _generated_plot_files.append(fname)
                plt.close(fig)
            plt.show = _patched_show
        except Exception:
            pass

        user_code = base64.b64decode({code_b64!r}).decode("utf-8", errors="replace")
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        success = True
        error = None

        _uploaded_files = [
            _n for _n in sorted(os.listdir(TMPDIR))
            if os.path.isfile(os.path.join(TMPDIR, _n)) and not _n.startswith("_")
        ]

        # A5: Bootstrap DuckDB with all uploaded files as tables
        _duckdb_tables = {{}}
        _db = None
        try:
            import duckdb as _duckdb_mod
            _db = _duckdb_mod.connect(database=":memory:")
            for _uf in _uploaded_files:
                _uf_path = os.path.join(TMPDIR, _uf)
                _uf_lower = _uf.lower()
                _tbl_name = _uf.rsplit(".", 1)[0] if "." in _uf else _uf
                _tbl_name = "".join(c if c.isalnum() or c == "_" else "_" for c in _tbl_name)
                if not _tbl_name or _tbl_name[0].isdigit():
                    _tbl_name = "t_" + _tbl_name
                try:
                    if _uf_lower.endswith(".parquet"):
                        _db.execute(f'CREATE TABLE "{{_tbl_name}}" AS SELECT * FROM read_parquet(?)', [_uf_path])
                    elif _uf_lower.endswith(".csv") or _uf_lower.endswith(".tsv"):
                        _db.execute(f'CREATE TABLE "{{_tbl_name}}" AS SELECT * FROM read_csv_auto(?)', [_uf_path])
                    else:
                        continue
                    _duckdb_tables[_tbl_name] = _uf
                except Exception:
                    pass
        except ImportError:
            pass
        except Exception:
            pass

        glb = {{
            "__name__": "__main__",
            "UPLOADED_FILES": _uploaded_files,
            "DEFAULT_FILE": (_uploaded_files[0] if _uploaded_files else ""),
            "DATA_DIR": os.path.join(TMPDIR, "mnt", "data"),
            "DB": _db,
            "DUCKDB_TABLES": _duckdb_tables,
        }}
        loc = {{}}
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            try:
                exec(compile(user_code, "<user_code>", "exec"), glb, loc)
            except Exception as exc:
                success = False
                error = f"{{exc.__class__.__name__}}: {{exc}}"
                traceback.print_exc(file=stderr_buf)

        # A5: Close DuckDB connection after user code
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass

        after = set(os.listdir(TMPDIR))
        generated = []
        for name in sorted(after - _before):
            if name.startswith("_"):
                continue
            p = _safe_path(name)
            if os.path.isfile(p):
                generated.append(name)
        for p in _generated_plot_files:
            if p not in generated:
                generated.append(p)

        payload = {{
            "success": success,
            "stdout": stdout_buf.getvalue(),
            "stderr": stderr_buf.getvalue(),
            "error": error,
            "generated_files": generated,
        }}
        print({RESULT_MARKER!r} + json.dumps(payload, ensure_ascii=False))
        """
    ).strip() + "\n"


async def execute_code(code: str, uploaded_files: Optional[Dict[str, bytes]] = None) -> Dict[str, Any]:
    """
    Executa codigo Python em subprocess isolado.

    Args:
        code: Código Python a executar.
        uploaded_files: Dict de {filename: bytes} para disponibilizar ao código.

    Returns:
        Dict com stdout, stderr, files (ficheiros gerados), images (base64).
    """
    if not CODE_INTERPRETER_ENABLED:
        return {"success": False, "error": "Code interpreter desativado."}

    validation_error = _validate_code(code)
    if validation_error:
        return {"success": False, "error": validation_error}

    with tempfile.TemporaryDirectory(prefix="dbde_code_") as tmpdir:
        tmp_path = Path(tmpdir)

        if uploaded_files:
            for filename, content in uploaded_files.items():
                safe_name = Path(str(filename or "input.bin")).name
                data = content if isinstance(content, (bytes, bytearray)) else b""
                if not safe_name or not data:
                    continue
                if len(data) > MAX_UPLOADED_FILE_BYTES:
                    logger.warning("Code interpreter skipped oversized input file: %s", safe_name)
                    continue
                (tmp_path / safe_name).write_bytes(bytes(data))

        code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        script = _runner_script(tmpdir, code_b64)
        script_path = tmp_path / "_runner.py"
        script_path.write_text(script, encoding="utf-8")

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=_build_subprocess_env(tmpdir),
                preexec_fn=_set_resource_limits if os.name != "nt" else None,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=CODE_INTERPRETER_TIMEOUT)
            raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
            raw_stderr = stderr_bytes.decode("utf-8", errors="replace")

            payload: Dict[str, Any] = {}
            user_stdout = raw_stdout
            if RESULT_MARKER in raw_stdout:
                head, marker_payload = raw_stdout.rsplit(RESULT_MARKER, 1)
                user_stdout = head.rstrip()
                try:
                    payload = json.loads(marker_payload.strip())
                except json.JSONDecodeError:
                    payload = {}

            stdout = user_stdout
            stderr = raw_stderr
            success = bool(payload.get("success", proc.returncode == 0))
            error = payload.get("error")
            if payload.get("stderr"):
                stderr = str(payload.get("stderr"))
            if payload.get("stdout"):
                if stdout:
                    stdout = f"{stdout}\n{payload.get('stdout')}".strip()
                else:
                    stdout = str(payload.get("stdout"))

            if len(stdout) > CODE_INTERPRETER_MAX_OUTPUT:
                stdout = stdout[:CODE_INTERPRETER_MAX_OUTPUT] + "\n... (output truncado)"
            if len(stderr) > CODE_INTERPRETER_MAX_OUTPUT:
                stderr = stderr[:CODE_INTERPRETER_MAX_OUTPUT] + "\n... (output truncado)"

            generated_files = []
            images_b64 = []
            for fname in payload.get("generated_files", []) or []:
                safe_name = Path(str(fname)).name
                if safe_name.startswith("_"):
                    continue
                fpath = tmp_path / safe_name
                if not fpath.exists() or not fpath.is_file():
                    continue
                size = fpath.stat().st_size
                if size > MAX_RETURN_FILE_BYTES:
                    logger.warning("Code interpreter skipped oversized output file: %s", safe_name)
                    continue
                data_b64 = base64.b64encode(fpath.read_bytes()).decode("ascii")
                record = {
                    "filename": safe_name,
                    "data": data_b64,
                    "size": size,
                    "mime_type": _guess_mime(safe_name),
                }
                if safe_name.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".gif")):
                    images_b64.append(record)
                else:
                    generated_files.append(record)

            return {
                "success": success and proc.returncode == 0,
                "stdout": stdout,
                "stderr": stderr or None,
                "return_code": proc.returncode,
                "error": error,
                "files": generated_files,
                "images": images_b64,
            }
        except asyncio.TimeoutError:
            if proc:
                proc.kill()
            return {"success": False, "error": f"Timeout: codigo excedeu {CODE_INTERPRETER_TIMEOUT} segundos."}
        except Exception as e:
            return {"success": False, "error": f"Erro de execucao: {str(e)}"}
