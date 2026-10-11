"""Keep directly imported packages in the runtime installation contract."""

from pathlib import Path

from packaging.requirements import Requirement
import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


@pytest.fixture
def runtime_dependencies():
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    requirements = [Requirement(value) for value in project["dependencies"]]
    return {requirement.name.lower(): requirement for requirement in requirements}


@pytest.mark.parametrize("distribution", ["python-dotenv", "pycryptodome", "sqlalchemy"])
def test_direct_imports_are_declared_without_extras(runtime_dependencies, distribution):
    requirement = runtime_dependencies[distribution]
    assert requirement.marker is None
    assert requirement.extras == set()


@pytest.mark.parametrize("python_version,expected", [("3.10", True), ("3.11", False), ("3.12", False)])
def test_tomli_is_required_only_without_stdlib_tomllib(runtime_dependencies, python_version, expected):
    requirement = runtime_dependencies["tomli"]
    assert requirement.marker is not None
    assert requirement.marker.evaluate({"python_version": python_version}) is expected
