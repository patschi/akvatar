"""Cross-file consistency checks.

These tests do not exercise runtime behavior; they pin the invariants that hold
*between* files - templates and their translation keys, JavaScript and the
translation subset it is handed, templates and the static assets they load, the
Dockerfile and the entry points it copies, and the CI pipeline and the values it
parses out of the source tree.  Each of these breaks silently: the application
starts fine and only misbehaves in the browser, in the container, or in CI.
"""

import re
from pathlib import Path

import pytest
import yaml

from src import APP_BASE_VERSION, APP_NAME, APP_VERSION
from src.app_static import static_cache
from src.config import DEFAULT_LOCALE
from src.i18n import _JS_KEYS, TRANSLATIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "src" / "templates"
JS_DIR = REPO_ROOT / "static" / "js"

TEMPLATES = sorted(TEMPLATE_DIR.rglob("*.html"))
APP_SOURCES = sorted((REPO_ROOT / "src").glob("*.py")) + sorted(REPO_ROOT.glob("*.py"))

# Matches a translation lookup with a literal key: t('some.key') / t("some.key").
# Dynamic lookups such as t('login.error_' + key) deliberately do not match.
_T_CALL = re.compile(r"\bt\(\s*['\"]([a-z0-9_.]+)['\"]\s*[,)]")
# Matches url_for('static', filename='css/style.css')
_STATIC_REF = re.compile(
    r"url_for\(\s*['\"]static['\"]\s*,\s*filename\s*=\s*['\"]([^'\"]+)['\"]"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class GitlabCiLoader(yaml.SafeLoader):
    """SafeLoader that tolerates GitLab's custom !reference tag."""


GitlabCiLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: loader.construct_sequence(node)
)


def load_pipeline() -> dict:
    """Parse .gitlab-ci.yml, keeping !reference nodes as plain lists."""
    return yaml.load(read(REPO_ROOT / ".gitlab-ci.yml"), Loader=GitlabCiLoader)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_templates_are_discovered():
    assert {path.name for path in TEMPLATES} >= {
        "dashboard.html",
        "login.html",
        "logged_out.html",
    }


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_template_compiles(flask_app, template):
    # The startup warm-up compiles all of them; a syntax error would only
    # surface there, after the container has already been rolled out.
    name = template.relative_to(TEMPLATE_DIR).as_posix()
    assert flask_app.jinja_env.get_template(name) is not None


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_translation_key_used_in_a_template_exists(template):
    english = TRANSLATIONS[DEFAULT_LOCALE]
    missing = sorted(
        key for key in _T_CALL.findall(read(template)) if key not in english
    )
    assert not missing, f"{template.name} uses unknown translation key(s): {missing}"


@pytest.mark.parametrize("source", APP_SOURCES, ids=lambda p: p.name)
def test_every_translation_key_used_in_python_exists(source):
    english = TRANSLATIONS[DEFAULT_LOCALE]
    missing = sorted(key for key in _T_CALL.findall(read(source)) if key not in english)
    assert not missing, f"{source.name} uses unknown translation key(s): {missing}"


# Assets an operator may drop in but that the repository does not ship.  The
# template guards each of them (app.py only sets custom_css when the file is
# actually present in the cache), so their absence is expected, not a 404.
OPTIONAL_STATIC_ASSETS = {"css/style.custom.css"}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_static_asset_referenced_by_a_template_is_cached(template):
    # Assets are served from the in-memory cache built at startup; a reference
    # to a file that is not there is a 404 in the browser, not a startup error.
    missing = sorted(
        ref
        for ref in _STATIC_REF.findall(read(template))
        if ref not in static_cache and ref not in OPTIONAL_STATIC_ASSETS
    )
    assert not missing, f"{template.name} references missing static file(s): {missing}"


def test_the_optional_custom_stylesheet_is_only_linked_when_present(flask_app):
    # app.py flips custom_css from the cache contents, so an operator-supplied
    # style.custom.css is picked up without a code change - and its absence
    # never produces a broken <link>.
    from src.app_static import static_cache as cache

    expected = "css/style.custom.css" in cache
    body = flask_app.test_client().get("/login").get_data(as_text=True)
    assert ("style.custom.css" in body) is expected


def test_templates_never_reference_an_external_origin():
    # Everything is bundled locally; an external <script> or <link> would be
    # blocked by the CSP anyway and would leak the user's IP to a third party.
    for template in TEMPLATES:
        body = read(template)
        for attribute in ("src=", "href="):
            for match in re.finditer(rf'{attribute}"(https?://[^"]+)"', body):
                pytest.fail(f"{template.name} loads an external URL: {match.group(1)}")


def test_the_dashboard_loads_the_bundled_cropper():
    assert "js/vendor/cropper.min.js" in read(TEMPLATE_DIR / "dashboard.html")


# ---------------------------------------------------------------------------
# JavaScript translation surface
# ---------------------------------------------------------------------------


def javascript_i18n_keys() -> set[str]:
    """Collect every I18N.<name> property the browser code reads."""
    keys: set[str] = set()
    for script in JS_DIR.rglob("*.js"):
        if "vendor" in script.parts:
            continue  # third-party bundle, not ours
        keys |= set(re.findall(r"\bI18N\.([A-Za-z0-9_]+)\b", read(script)))
    return keys


def test_javascript_reads_some_translations():
    assert len(javascript_i18n_keys()) > 20


def dynamically_referenced_keys() -> set[str]:
    """Translation keys resolved at runtime rather than through a literal t('...').

    ``login.html`` builds its message as ``t('login.error_' + error_key)``, so a
    regex scan cannot see those keys.  They are derived from the same allow-list
    the route uses, so adding a new error key automatically counts as used.
    """
    from src.web_routes import _VALID_ERROR_KEYS

    return {f"login.error_{key}" for key in _VALID_ERROR_KEYS}


def literally_referenced_keys() -> set[str]:
    """Every translation key reached through a literal t('...') call."""
    keys: set[str] = set()
    for source in [*TEMPLATES, *APP_SOURCES]:
        keys |= set(_T_CALL.findall(read(source)))
    return keys


def test_the_dynamic_login_error_keys_all_exist():
    english = TRANSLATIONS[DEFAULT_LOCALE]
    missing = sorted(key for key in dynamically_referenced_keys() if key not in english)
    assert not missing, f"login.html can render unknown key(s): {missing}"


def test_no_translation_key_is_unused():
    """Every shipped string must be reachable from somewhere.

    Dead strings accumulate quietly: they still have to be translated into every
    locale, and they make the catalog harder to reason about.  ``_code`` and
    ``_name`` are language-selector metadata, not translations, so they are
    exempt.
    """
    used = (
        literally_referenced_keys()
        | dynamically_referenced_keys()
        | set(_JS_KEYS)
        | {"_code", "_name"}
    )
    unused = sorted(set(TRANSLATIONS[DEFAULT_LOCALE]) - used)
    assert not unused, f"unused translation key(s): {unused}"


def test_no_translation_is_shipped_to_the_javascript_without_being_used():
    # Keeps the per-page JSON payload from accumulating dead entries.
    shipped = {key.replace(".", "_") for key in _JS_KEYS}
    unused = sorted(shipped - javascript_i18n_keys())
    assert not unused, f"_JS_KEYS ships key(s) the browser never reads: {unused}"


def test_every_translation_the_javascript_reads_is_shipped_to_it():
    # _JS_KEYS is the hand-maintained subset injected into the page; a key the
    # JS reads but that is missing from it renders as "undefined" in the UI.
    shipped = {key.replace(".", "_") for key in _JS_KEYS}
    missing = sorted(javascript_i18n_keys() - shipped)
    assert not missing, f"JavaScript reads untranslated key(s): {missing}"


# ---------------------------------------------------------------------------
# Version metadata
# ---------------------------------------------------------------------------


def test_the_base_version_is_a_plain_dotted_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_BASE_VERSION), APP_BASE_VERSION


def test_the_ci_awk_expression_can_parse_the_base_version():
    """The container build parses APP_BASE_VERSION out of src/__init__.py with awk.

    Reformatting that assignment (quotes, spacing, moving it into a function)
    would silently produce an empty image version instead of failing the build.
    """
    init_source = read(REPO_ROOT / "src" / "__init__.py")
    match = re.search(r'^APP_BASE_VERSION\s*=\s*"([^"]+)"', init_source, re.MULTILINE)
    assert match, "the CI awk expression would extract an empty version"
    assert match.group(1) == APP_BASE_VERSION


def test_the_runtime_version_combines_the_base_version_and_the_git_hash():
    assert APP_VERSION.startswith(f"{APP_BASE_VERSION}+")
    assert APP_NAME == "akvatar"


def test_the_user_agent_identifies_the_application():
    from src import USER_AGENT

    assert USER_AGENT == f"{APP_NAME}/v{APP_VERSION}"


# ---------------------------------------------------------------------------
# Packaging and container build
# ---------------------------------------------------------------------------


def test_every_top_level_entry_point_is_copied_into_the_image():
    # The Dockerfile lists files explicitly (there is no .dockerignore), so a
    # new entry point that is not added there is simply absent at runtime.
    dockerfile = read(REPO_ROOT / "Dockerfile")
    for entry_point in sorted(REPO_ROOT.glob("*.py")):
        assert entry_point.name in dockerfile, (
            f"{entry_point.name} is not COPYed into the container image"
        )


def test_the_image_copies_the_application_packages():
    dockerfile = read(REPO_ROOT / "Dockerfile")
    assert "COPY src/ src/" in dockerfile
    assert "COPY static/ static/" in dockerfile


def test_the_container_config_path_matches_the_documented_volume():
    dockerfile = read(REPO_ROOT / "Dockerfile")
    assert 'CONFIG_PATH="/data/config/config.yml"' in dockerfile
    assert '"/data/config"' in dockerfile


def test_the_uv_version_is_the_same_in_the_dockerfile_and_the_pipeline():
    # Renovate updates both in one commit; a drift here means the CI lint jobs
    # and the image build resolve dependencies with different tooling.
    dockerfile = read(REPO_ROOT / "Dockerfile")
    pipeline = read(REPO_ROOT / ".gitlab-ci.yml")

    docker_uv = re.search(r"ghcr\.io/astral-sh/uv:([0-9.]+)", dockerfile)
    ci_uv = re.search(r'UV_VERSION:\s*"([0-9.]+)"', pipeline)

    assert docker_uv and ci_uv
    assert docker_uv.group(1) == ci_uv.group(1)


def test_the_builder_and_runtime_python_versions_stay_aligned():
    # CONTRIBUTING.md documents this as a deliberate, coordinated upgrade: the
    # builder compiles wheels the distroless runtime has to be able to import.
    dockerfile = read(REPO_ROOT / "Dockerfile")
    builder = re.search(r"FROM python:(\d+)\.(\d+)-slim-(\w+)", dockerfile)
    runtime = re.search(r"FROM gcr\.io/distroless/python3-debian(\d+)", dockerfile)

    assert builder and runtime
    debian_codenames = {"trixie": "13", "bookworm": "12"}
    assert debian_codenames[builder.group(3)] == runtime.group(1)


def test_the_declared_python_floor_matches_the_container_python():
    pyproject = yaml_safe_toml(REPO_ROOT / "pyproject.toml")
    dockerfile = read(REPO_ROOT / "Dockerfile")
    builder = re.search(r"FROM python:(\d+\.\d+)-slim", dockerfile)

    assert builder
    assert pyproject["project"]["requires-python"] == f">={builder.group(1)}"


def yaml_safe_toml(path: Path) -> dict:
    """Parse a TOML file (named for symmetry with the YAML helpers above)."""
    import tomllib

    return tomllib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# CI pipeline
# ---------------------------------------------------------------------------


def test_the_pipeline_defines_a_test_stage():
    pipeline = load_pipeline()
    assert "test" in pipeline["stages"]


def test_at_least_one_job_runs_in_the_test_stage():
    pipeline = load_pipeline()
    jobs_in_test_stage = [
        name
        for name, job in pipeline.items()
        if isinstance(job, dict) and job.get("stage") == "test"
    ]
    assert jobs_in_test_stage, "the test stage is declared but has no jobs"


def test_the_test_stage_runs_before_the_container_is_built():
    # Otherwise a failing test would only be reported after the image had
    # already been pushed to the registry.
    stages = load_pipeline()["stages"]
    assert stages.index("test") < stages.index("build-container")


def test_the_container_build_cannot_skip_the_earlier_stages():
    # push-container-gitlab must rely on stage ordering alone.  A `needs:` on it
    # would let kaniko start before lint and test finished, silently removing
    # the gate.  (test-pytest itself declares needs: [] so it runs in parallel
    # with lint rather than after it - that does not weaken the gate.)
    assert "needs" not in load_pipeline()["push-container-gitlab"]


def test_the_test_job_runs_the_suite_with_the_locked_dependencies():
    pipeline = load_pipeline()
    script = " ".join(pipeline["test-pytest"]["script"])
    # --frozen makes a stale uv.lock a hard failure instead of a silent re-resolve.
    assert "--frozen" in script
    assert "pytest" in script


def test_the_test_job_publishes_junit_and_coverage_reports():
    reports = load_pipeline()["test-pytest"]["artifacts"]["reports"]
    assert reports["junit"]
    assert reports["coverage_report"]["coverage_format"] == "cobertura"


def test_the_lint_jobs_cover_the_test_suite_too():
    pipeline = load_pipeline()
    for job in ("lint-ruff-check", "lint-ruff-format"):
        script = " ".join(pipeline[job]["script"])
        assert "tests/" in script, f"{job} does not lint tests/"


def test_the_test_suite_is_part_of_the_declared_dev_dependency_group():
    pyproject = yaml_safe_toml(REPO_ROOT / "pyproject.toml")
    dev_group = pyproject["dependency-groups"]["dev"]
    assert any(spec.startswith("pytest==") for spec in dev_group)


def test_dev_dependencies_are_pinned_like_the_runtime_dependencies():
    pyproject = yaml_safe_toml(REPO_ROOT / "pyproject.toml")
    for spec in pyproject["dependency-groups"]["dev"]:
        assert "==" in spec, f"{spec} is not pinned to an exact version"


def test_the_lockfile_is_in_sync_with_pyproject():
    # `uv sync --frozen` in CI fails on drift; catching it here names the cause.
    lock = read(REPO_ROOT / "uv.lock")
    pyproject = yaml_safe_toml(REPO_ROOT / "pyproject.toml")
    for spec in (
        pyproject["project"]["dependencies"] + pyproject["dependency-groups"]["dev"]
    ):
        name, _, version = spec.partition("==")
        name = re.split(r"[\[<>=!;]", name)[0].strip().lower()
        assert f'name = "{name}"' in lock.lower(), f"{name} is missing from uv.lock"
        if version:
            assert f'version = "{version}"' in lock, (
                f"{name} is pinned to {version} but uv.lock has a different version"
            )
