import ast
from collections import defaultdict
from importlib.util import resolve_name
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src/personal_health_lab"
MODULES = {
    "adapters",
    "application",
    "health_data",
    "health_import",
    "overview",
    "package_root",
    "resting_hr_analysis",
    "runtime",
    "storage",
    "synthetic_export",
}
ALLOWED_DEPENDENCIES = {
    "adapters": {"application", "package_root"},
    "application": {"health_import", "overview", "package_root", "resting_hr_analysis"},
    "health_data": set(),
    "health_import": {"health_data", "storage"},
    "overview": {"package_root", "storage"},
    "package_root": {"runtime"},
    "resting_hr_analysis": {"health_data", "storage"},
    "runtime": set(),
    "storage": {"health_data", "package_root"},
    "synthetic_export": set(),
}

_TARGET_MODULE_NAMES = {"_runtime": "runtime"}


def _source_module(path: Path) -> str | None:
    relative = path.relative_to(PACKAGE_ROOT)
    if len(relative.parts) > 1:
        candidate = relative.parts[0]
        return candidate if candidate in MODULES else None
    if relative.name == "__init__.py":
        return "package_root"
    if relative.name == "_runtime.py":
        return "runtime"
    return None


def _is_project_module(qualified_name: str) -> bool:
    parts = qualified_name.split(".")
    if not parts or parts[0] != "personal_health_lab":
        return False
    module_path = PACKAGE_ROOT.joinpath(*parts[1:])
    return module_path.is_dir() or module_path.with_suffix(".py").is_file()


def _project_imports() -> tuple[dict[str, set[str]], list[str]]:
    dependencies: dict[str, set[str]] = defaultdict(set)
    deep_imports: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        source_module = _source_module(path)
        if source_module is None:
            continue
        relative_path = path.relative_to(PACKAGE_ROOT).with_suffix("")
        module_parts = ("personal_health_lab", *relative_path.parts)
        if module_parts[-1] == "__init__":
            module_parts = module_parts[:-1]
        package = ".".join(module_parts[:-1] if path.name != "__init__.py" else module_parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative_name = "." * node.level + (node.module or "")
                    base_module = resolve_name(relative_name, package)
                elif node.module is not None:
                    base_module = node.module
                else:
                    continue
                imported_modules.append(base_module)
                imported_modules.extend(
                    candidate
                    for alias in node.names
                    if _is_project_module(candidate := f"{base_module}.{alias.name}")
                )
            for imported_module in imported_modules:
                parts = imported_module.split(".")
                if not parts or parts[0] != "personal_health_lab":
                    continue
                target_module = (
                    "package_root"
                    if len(parts) == 1
                    else _TARGET_MODULE_NAMES.get(parts[1], parts[1])
                )
                if target_module == source_module or target_module not in MODULES:
                    continue
                dependencies[source_module].add(target_module)
                if len(parts) > 2:
                    deep_imports.append(
                        f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno} {imported_module}"
                    )
    return dependencies, deep_imports


def test_modules_only_import_allowed_project_modules() -> None:
    dependencies, _ = _project_imports()
    violations = {
        module: imported - ALLOWED_DEPENDENCIES[module]
        for module, imported in dependencies.items()
        if imported - ALLOWED_DEPENDENCIES[module]
    }
    assert violations == {}


def test_cross_module_deep_imports_are_forbidden() -> None:
    _, deep_imports = _project_imports()
    assert deep_imports == []


def test_project_module_dependency_graph_is_acyclic() -> None:
    dependencies, _ = _project_imports()
    visited: set[str] = set()
    active: set[str] = set()

    def visit(module: str) -> None:
        if module in active:
            raise AssertionError(f"Abhängigkeitszyklus bei {module}")
        if module in visited:
            return
        active.add(module)
        for dependency in dependencies[module]:
            visit(dependency)
        active.remove(module)
        visited.add(module)

    for module in MODULES:
        visit(module)
