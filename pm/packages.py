"""Package definitions for the tools hermes manages. Versions and hashes
live in pm/lock.json (written by `pm lock`), never here."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from pm.package import (
    InstallError,
    Package,
    StatePackage,
    _missing_reason,
    _probe_reason,
)
from pm.registry import register
from pm.store import ALL_TARGETS, Store, flatten_single_dir, merge_tree
from pm.update import (
    github_release_tags,
    llama_app_bucket_versions,
    llama_app_latest,
    martin_riedl_index,
    martin_riedl_versions,
    node_latest_versions,
    npm_dist_tags,
    pbs_build_tags,
)

_RUST_TRIPLE = {
    "linux-x64": "x86_64-unknown-linux-gnu",
    "linux-arm64": "aarch64-unknown-linux-gnu",
    "darwin-x64": "x86_64-apple-darwin",
    "darwin-arm64": "aarch64-apple-darwin",
}

_NODE_PLAT = {
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64",
    "darwin-x64": "darwin-x64",
    "darwin-arm64": "darwin-arm64",
}


class BinaryPackage(Package):
    """A downloaded archive exposing one binary. Covers most tools."""

    binary_rel: dict[str, str] = {}
    flatten = True
    probe_version = True
    # argv after the binary for the smoke probe. A package whose binary
    # rejects the GNU double-dash form (ffmpeg's BtbN autobuild) overrides.
    probe_args: list[str] = ["--version"]
    # Run the probe with cwd=binary.parent: dlopen'd backends (llama.cpp's
    # cudart) resolve their shared libraries from the working directory.
    probe_cwd = False

    def _rel(self, target: str) -> Optional[str]:
        return self.binary_rel.get(target) or self.binary_rel.get("posix")

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        if self.flatten:
            flatten_single_dir(staged)

    def binary(self, entry: Path, target: str) -> Optional[Path]:
        rel = self._rel(target)
        return entry / rel if rel else None

    def verify(self, entry: Path, target: str) -> str:
        """Return '' when the entry is usable on target, else why not:
        a missing binary, a wrong-arch binary, or a --version probe that
        fails to exec, times out, or exits nonzero."""
        binary = self.binary(entry, target)
        if binary is None:
            return "no binary_rel for this target"
        reason = self._binary_reason(binary, entry, target)
        if reason:
            return reason
        if not self.probe_version:
            return ""
        try:
            proc = subprocess.run(
                [str(binary), *self.probe_args],
                capture_output=True,
                timeout=60,
                cwd=str(binary.parent) if self.probe_cwd else None,
                env=self._probe_env(),
            )
        except OSError as e:
            return f"could not exec {binary} {' '.join(self.probe_args)}: {e}"
        except subprocess.TimeoutExpired:
            return f"{binary} {' '.join(self.probe_args)} timed out after 60s"
        if proc.returncode != 0:
            return _probe_reason(binary, proc)
        return ""

    def _probe_env(self) -> dict:
        """Deps' env composed in: npm's shim is `#!/usr/bin/env node` and
        must find the node it extends on PATH."""
        if not self.deps:
            return dict(os.environ)
        from pm.ensure import env_for

        return env_for(*self.deps)


@register
class Uv(BinaryPackage):
    name = "uv"
    internal = True
    binary_rel = {"posix": "uv"}

    def fetch_url(self, version: str, target: str) -> str:
        triple = _RUST_TRIPLE[target]
        ext = "tar.gz"
        return f"https://github.com/astral-sh/uv/releases/download/{version}/uv-{triple}.{ext}"

    def latest_versions(self, target: str, locked=None) -> list[str]:
        return github_release_tags("astral-sh/uv")


_MACOS_MANAGED_PYTHON_IDENTIFIER = "com.nousresearch.hermes.managed-python"


def _macos_sign_managed_python(python: Path) -> bool:
    """Give a downloaded Python a stable macOS code identity."""
    if platform.system() != "Darwin":
        return False

    codesign = shutil.which("codesign")
    if not codesign:
        return False

    requirement = (
        "=designated => identifier "
        f'"{_MACOS_MANAGED_PYTHON_IDENTIFIER}"'
    )
    try:
        signed = subprocess.run(
            [
                codesign,
                "--force",
                "--deep",
                "--sign",
                "-",
                "--timestamp=none",
                "--identifier",
                _MACOS_MANAGED_PYTHON_IDENTIFIER,
                "--requirements",
                requirement,
                str(python),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if signed.returncode != 0:
            return False
        verified = subprocess.run(
            [codesign, "--verify", "--deep", "--strict", str(python)],
            check=False,
            capture_output=True,
            text=True,
        )
        return verified.returncode == 0
    except Exception:
        return False


@register
class Python(BinaryPackage):
    """The payload interpreter (python-build-standalone install_only).
    Optional: dev installs use their own venv's python; bundles stage this
    and point the relocatable venv's pyvenv.cfg at it (pm adopt)."""

    name = "python"
    optional = True
    probe_version = False
    binary_rel = {"posix": "bin/python3"}

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        super().stage(store, staged, version, target)
        binary = self.binary(staged, target)
        if binary is not None:
            _macos_sign_managed_python(binary)

    def fetch_url(self, version: str, target: str) -> str:
        # lock version is "<python>+<release tag>", e.g. "3.11.13+202****0807"
        pyver, _, tag = version.partition("+")
        if not tag:
            raise InstallError(self.name, f"version {version!r} needs the +<release> tag")
        triple = _RUST_TRIPLE[target]
        return (
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{tag}/cpython-{pyver}+{tag}-{triple}-install_only.tar.gz"
        )

    def latest_versions(self, target: str, locked=None) -> list[str]:
        # Stay on the locked python minor line (3.11); bump only the
        # +<build-tag>. A major/minor bump is a deliberate decision, never
        # an auto-update.
        if not locked or "+" not in locked:
            return []
        pyver = locked.partition("+")[0]
        minor = ".".join(pyver.split(".")[:2])
        return [f"{pyver}+{tag}" for tag in pbs_build_tags(minor, target)]


def _uv_lock_digest(path: Path) -> bytes:
    """sha256 of uv.lock, cached on (mtime_ns, size) — check() runs at
    every startup and uv.lock is megabyte-class."""
    import hashlib

    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _uv_lock_digest_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).digest()
    _uv_lock_digest_cache[path] = (key, digest)
    return digest


_uv_lock_digest_cache: dict[Path, tuple] = {}


def _file_digest(path: Path) -> bytes:
    """sha256 of a small always-present project file (no cache needed)."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return b""


def uv_cache_dir() -> Path:
    """The hermes-owned uv cache: machine-scoped and shared (keyed by
    content — two profiles reuse one cache), anchored to the DEFAULT
    hermes root like partials_root(). A bundle ships a seeded copy at
    the payload root (uv-cache/); the first call on a sealed install
    copies it out to the writable machine cache (the read-only payload
    can't serve uv's working cache), and a warm `uv sync --offline`
    from it is near-free (probed: 0.4s vs 1.2s cold) — the blow-away-
    on-update contract depends on it. uv's default cache location is
    per-user/platform-opinionated and never used by pm."""
    from hermes_constants import get_default_hermes_root

    machine_cache = get_default_hermes_root() / "cache" / "uv"
    marker = machine_cache / ".seeded"
    if not marker.is_file():
        # Seed from a shipped bundle cache when present (payload root =
        # store_root().parent on a sealed install).
        try:
            from pm.paths import store_root

            payload_cache = store_root().parent / "uv-cache"
            if payload_cache.is_dir():
                machine_cache.mkdir(parents=True, exist_ok=True)
                import shutil as _shutil

                for entry in payload_cache.iterdir():
                    if entry.name == ".seeded":
                        continue
                    dest = machine_cache / entry.name
                    if not dest.exists():
                        (
                            _shutil.copytree(entry, dest)
                            if entry.is_dir()
                            else _shutil.copy2(entry, dest)
                        )
        except OSError:
            pass  # seeding is best-effort; a cold sync still works
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1", encoding="utf-8")
        except OSError:
            pass
    return machine_cache


def uv_env(base_env: Optional[dict] = None) -> dict[str, str]:
    """Sanitized env for pm's internal uv invocations: user-level UV
    overrides and active-venv leakage must not steer which interpreter or
    install dir uv picks (the interpreter-hijack class, #83914), and
    ambient user/system uv config (uv.toml under XDG_CONFIG_HOME/DIRS)
    must not reach the subprocess either. User config discovery is
    redirected to an empty dir; HOME is left alone so caches, credentials,
    and git keep working (the #82446 isolation contract, which lived in
    the installers' run_locked_uv_sync() before the sync moved into pm).
    UV_CACHE_DIR always points at the hermes-owned cache (shipped with
    bundles, shared machine-wide) — never the user's ambient cache."""
    env = dict(os.environ if base_env is None else base_env)
    for key in list(env):
        if key.startswith("UV_") or key in (
            "VIRTUAL_ENV",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "PYTHONEXECUTABLE",
        ):
            env.pop(key)
    isolated = _isolated_config_dir()
    env["XDG_CONFIG_HOME"] = str(isolated)
    env["XDG_CONFIG_DIRS"] = str(isolated)
    env["UV_NO_CONFIG"] = "1"
    env["UV_CACHE_DIR"] = str(uv_cache_dir())
    return env


_ISOLATED_CONFIG_DIR: Optional[str] = None


def _isolated_config_dir() -> Path:
    """A per-process empty dir ambient uv config is redirected to. One
    shared empty dir is all that's needed (and all that ever gets
    created); it is never populated, so it cannot steer resolution."""
    import tempfile

    global _ISOLATED_CONFIG_DIR
    if _ISOLATED_CONFIG_DIR is None:
        _ISOLATED_CONFIG_DIR = tempfile.mkdtemp(prefix="pm-uv-")
    return Path(_ISOLATED_CONFIG_DIR)


@register
class Venv(StatePackage):
    """The project venv: pyproject.toml + uv.lock + enabled extras.
    Core-only installs use `uv sync --locked`; plugin members use the
    workspace union lock. `--locked` (not `--frozen`) is load-bearing for
    the core path: it asserts uv.lock is up-to-date with pyproject.toml,
    so a stale lock fails instead of silently installing an unreconciled
    set. Both paths build a candidate environment for the caller to select."""

    name = "venv"
    deps = ("uv",)

    def project_root(self) -> Path:
        from pm.paths import repo_root

        return repo_root()

    def venv_dir(self) -> Path:
        from hermes_cli.runtime_paths import selected_venv

        return selected_venv(self.project_root())

    def expected_stamp(self, extras: list[str], *, plugin_dirs=None) -> str:
        import hashlib
        import sys

        h = hashlib.sha256()
        root = self.project_root()
        h.update(_uv_lock_digest(root / "uv.lock"))
        # pyproject.toml is stamped too: editing it without re-locking
        # leaves uv.lock bytes untouched, and a stamp over the lock alone
        # would make sync_venv no-op past the `--locked` validation the
        # installers' tier-0 sync ran on every install.
        h.update(_file_digest(root / "pyproject.toml"))
        h.update(",".join(sorted(extras)).encode())
        h.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
        # Plugin members union into the venv — a changed member set must
        # re-sync even when extras and core lock are unchanged.
        from pm.workspace import enabled_member_dirs, members_stamp

        h.update(members_stamp(enabled_member_dirs() if plugin_dirs is None else plugin_dirs).encode())
        return h.hexdigest()

    def apply(self, extras: list[str], *, plugin_dirs=None) -> dict:
        """Prepare one complete environment; the caller commits its selection."""
        import sys
        import uuid
        from hermes_cli.runtime_paths import install_state_dir, runtime_facts_path
        from pm.ensure import uv as pm_uv
        from pm.lock import Facts
        from pm.workspace import enabled_member_dirs, lock_and_sync

        project = self.project_root()
        generation = install_state_dir(project) / "environments" / uuid.uuid4().hex
        candidate = generation / "venv"
        uv_bin, env = pm_uv()
        if uv_bin is None:
            raise InstallError(self.name, "uv is not installed")
        env["UV_PROJECT_ENVIRONMENT"] = str(candidate)
        env.pop("UV_NO_CONFIG", None)  # project indexes/sources belong to the project
        members = enabled_member_dirs() if plugin_dirs is None else plugin_dirs
        try:
            generation.mkdir(parents=True)
            (generation / ".lease-managed").touch()
            create = subprocess.run(
                [uv_bin, "venv", "--relocatable", "--python", sys.executable, str(candidate)],
                env=env, capture_output=True, text=True, timeout=120,
            )
            if create.returncode:
                raise InstallError(self.name, f"uv venv failed: {create.stderr[-600:]}")
            if members:
                prior = Facts(runtime_facts_path(project)).get("venv") or {}
                seed = (Path(prior["resolved_lock"]) if prior.get("resolved_lock")
                        else project / "uv.lock")
                lock_and_sync(members, extras, venv_dir=candidate, root=generation / "workspace",
                              seed_lock=seed, frozen=False, env=env)
                resolved_lock = generation / "workspace" / "uv.lock"
            else:
                cmd = [uv_bin, "sync", "--locked"]
                for extra in sorted(extras):
                    cmd += ["--extra", extra]
                proc = subprocess.run(
                    cmd, cwd=str(project), env=env, capture_output=True,
                    text=True, timeout=1800,
                )
                if proc.returncode:
                    raise InstallError(
                        self.name, f"uv sync exited {proc.returncode}: {proc.stderr[-400:]}"
                    )
                resolved_lock = project / "uv.lock"
            python = candidate / "bin/python"
            checked = subprocess.run(
                [uv_bin, "pip", "check", "--python", str(python)],
                env=env, capture_output=True, text=True, timeout=60,
            )
            if checked.returncode:
                raise InstallError(self.name, f"dependency validation failed: {checked.stderr[-600:]}")
        except BaseException:
            shutil.rmtree(generation, ignore_errors=True)
            raise
        (generation / ".lease-managed").touch()
        return {"environment": candidate, "resolved_lock": resolved_lock}


@register
class Nodejs(BinaryPackage):
    name = "node"
    binary_rel = {"posix": "bin/node"}

    def fetch_url(self, version: str, target: str) -> str:
        plat = _NODE_PLAT[target]
        return f"https://nodejs.org/dist/v{version}/node-v{version}-{plat}.tar.xz"

    def latest_versions(self, target: str, locked=None) -> list[str]:
        return node_latest_versions()


@register
class Npm(BinaryPackage):
    name = "npm"
    deps = ("node",)
    binary_rel = {"posix": "bin/npm"}
    flatten = False
    probe_version = False
    url = "https://registry.npmjs.org/npm/-/npm-{version}.tgz"

    def unpack(self, archive: Path, staged: Path, target: str) -> None:
        """npm installs itself using the node it extends: a plain unpack
        resolves the cli from dirname(process.execPath) and finds node's
        bundled npm instead. --offline pins the bytes to the verified
        tarball; --ignore-scripts + a sanitized env keep user npm/node
        config out of the staging."""
        from pm.ensure import _installed_location, _lockfile
        from pm.registry import get_package

        node = get_package("node")
        location = _installed_location(node, _lockfile(), target)
        if location is None:
            raise InstallError(self.name, "npm extends node, which is not installed")
        facts, store = location
        node_fact = facts.get("node")
        if node_fact is None:
            raise InstallError(self.name, "npm extends node, which is not installed")
        node_bin = node.binary(store.entry(node_fact["entry"]), target)
        if node_bin is None or not node_bin.is_file():
            raise InstallError(self.name, "node's entry is missing its binary")
        bundled_cli = node_bin.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if not bundled_cli.is_file():
            raise InstallError(self.name, "node's entry is missing its bundled npm-cli.js")

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.lower().startswith("npm_config_")
            and key not in ("NODE_OPTIONS", "NODE_PATH", "NODE_ENV")
        }
        env["npm_config_cache"] = str(archive.parent / ".npm-cache")

        staged.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                str(node_bin), str(bundled_cli), "install", "--global",
                "--prefix", str(staged), "--offline", "--ignore-scripts",
                "--no-audit", "--no-fund", str(archive),
            ],
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
        )
        if proc.returncode != 0:
            raise InstallError(
                self.name, f"self-install exited {proc.returncode}: {proc.stderr[-400:]}"
            )

    def latest_versions(self, target: str, locked=None) -> list[str]:
        latest = npm_dist_tags("npm").get("latest")
        return [latest] if latest else []


@register
class Gh(BinaryPackage):
    name = "gh"
    optional = True
    binary_rel = {"posix": "bin/gh"}

    def fetch_url(self, version: str, target: str) -> str:
        osname, arch = target.split("-")
        plat = {"linux": "linux", "darwin": "macOS"}[osname]
        arch = {"x64": "amd64", "arm64": "arm64"}[arch]
        ext = "zip" if osname == "darwin" else "tar.gz"
        return (
            f"https://github.com/cli/cli/releases/download/v{version}/"
            f"gh_{version}_{plat}_{arch}.{ext}"
        )

    def latest_versions(self, target: str, locked=None) -> list[str]:
        return github_release_tags("cli/cli", strip_prefix="v")


@register
class Ffmpeg(BinaryPackage):
    """Static ffmpeg. GPLv3 builds; always bundled.
    optional=False: ffmpeg is a required runtime tool. Sealed bundles ship
    it baked into the payload (post_update skips provisioning sealed
    installs — the artifact is atomic); dev installs get it re-ensured by
    step_provision_runtimes when the pin bumps. Linux + macOS:
    ffmpeg.martin-riedl.de (uniform ZIP, published sha256; single-binary —
    no ffprobe)."""

    name = "ffmpeg"
    optional = False
    # The per-target build streams can drift in PATCH. The lockfile version
    # label is major.minor; each target's exact patch lives in its artifact urls.
    version_style = "minor"
    # martin-riedl zips are a single `ffmpeg` file at the zip root.
    binary_rel = {"posix": "ffmpeg"}
    flatten = True
    # `-version` is accepted by every ffmpeg build.
    probe_args = ["-version"]

    def fetch_url(self, version: str, target: str) -> str:
        osname, arch = target.split("-")
        epoch = martin_riedl_index().get(target, {}).get(version)
        if epoch is not None:
            osdir = "macos" if osname == "darwin" else "linux"
            source_arch = "amd64" if arch == "x64" else arch
            return f"https://ffmpeg.martin-riedl.de/download/{osdir}/{source_arch}/{epoch}_{version}/ffmpeg.zip"
        # Existing installs read exact URLs from the lockfile. Re-pinning
        # must never silently substitute a different version or target.
        raise InstallError(self.name, f"no advertised {version} artifact for {target}",
                           "retry when the upstream index is available, or keep the existing pin")

    def latest_versions(self, target: str, locked=None) -> list[str]:
        return martin_riedl_versions(target)



@register
class Ripgrep(BinaryPackage):
    name = "ripgrep"
    binary_rel = {"posix": "rg"}

    def fetch_url(self, version: str, target: str) -> str:
        triple = _RUST_TRIPLE[target].replace("-unknown-linux-gnu", "-unknown-linux-musl")
        return (
            f"https://github.com/BurntSushi/ripgrep/releases/download/{version}/"
            f"ripgrep-{version}-{triple}.tar.gz"
        )

    def latest_versions(self, target: str, locked=None) -> list[str]:
        return github_release_tags("BurntSushi/ripgrep")


@register
class CuaDriver(BinaryPackage):
    name = "cua-driver"
    optional = True
    binary_rel = {"posix": "cua-driver"}

    def fetch_url(self, version: str, target: str) -> str:
        arch = {
            "darwin-x64": "darwin-universal",
            "darwin-arm64": "darwin-universal",
            "linux-x64": "linux-x86_64",
            "linux-arm64": "linux-arm64",
        }[target]
        return (
            f"https://github.com/trycua/cua/releases/download/cua-driver-rs-v{version}/"
            f"cua-driver-rs-{version}-{arch}-binary.tar.gz"
        )

    def latest_versions(self, target: str, locked=None) -> list[str]:
        return github_release_tags("trycua/cua", strip_prefix="cua-driver-rs-v")

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        flatten_single_dir(staged)
        sdk = staged / "sdk"
        if sdk.is_dir():
            shutil.rmtree(sdk, ignore_errors=True)

    def _probe_env(self) -> dict:
        """The probe IS a first run: it must not mint telemetry state."""
        try:
            from tools.computer_use.cua_backend import cua_driver_child_env

            return cua_driver_child_env()
        except Exception:
            return dict(os.environ, CUA_DRIVER_RS_TELEMETRY_ENABLED="0")



class LlamaCpp(BinaryPackage):
    """One llama.cpp backend build. Backends are dlopen'd plugins, so a
    usable engine is one archive per (target, backend).

    Backend is a HARDWARE choice, not a target, so each backend is its own
    optional package and the runtime asks for the one this machine can
    use. Version is llama.cpp's rolling release tag without the `b`.
    """

    optional = True
    on_path = False
    binary_rel = {"posix": "llama-server"}
    flatten = False
    # --version is llama-server's liveness proof AND the check that the
    # backend's shared libraries resolve: a CUDA build with no cudart
    # beside it fails here rather than at first chat.
    probe_cwd = True

    backend: str = ""
    # Release-asset infix per target, or absent where upstream ships none.
    assets: dict[str, str] = {}

    @property
    def gaps(self) -> dict[str, str]:  # type: ignore[override]
        return {
            target: f"llama.cpp publishes no {self.backend} build for {target}"
            for target in ALL_TARGETS
            if target not in self.assets
        }

    def _asset_names(self, version: str, target: str) -> list[str]:
        return [f"llama-b{version}-bin-{self.assets[target]}.tar.gz"]

    def fetch_urls(self, version: str, target: str) -> list[str]:
        return [
            f"https://github.com/ggml-org/llama.cpp/releases/download/b{version}/{asset}"
            for asset in self._asset_names(version, target)
        ]

    def fetch_url(self, version: str, target: str) -> str:
        return self.fetch_urls(version, target)[0]

    def latest_versions(self, target: str, locked=None) -> list[str]:
        # Resolve from the llama.app installer bucket (the installer's own
        # updater pointer + version index — no API token, no rate limit):
        # the `latest` pointer is the authoritative "next version"; the
        # bucket tree supplies the full candidate list. Artifacts still
        # come from the llama.cpp GitHub releases (1:1 tag correspondence).
        latest = llama_app_latest()
        if latest is not None:
            return [latest, *llama_app_bucket_versions()]
        return llama_app_bucket_versions() or github_release_tags(
            "ggml-org/llama.cpp", strip_prefix="b"
        )

    def known_sha256(self, version: str, url: str) -> Optional[str]:
        """GitHub's release API serves every asset's digest, so pinning a
        280 MB engine costs one API call instead of the download."""
        return _github_release_digests("ggml-org/llama.cpp", f"b{version}").get(
            url.rsplit("/", 1)[-1]
        )

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        """Some archives nest the binaries under build/bin; hoist them so
        binary_rel is one path for every target."""
        if (staged / self.binary(staged, target).name).is_file():
            return
        found = sorted(staged.rglob(self.binary(staged, target).name))
        if not found:
            raise InstallError(self.name, "archive contains no llama-server")
        merge_tree(found[0].parent, staged)


def _github_release_digests(repo: str, tag: str) -> dict[str, str]:
    import json
    import os
    import urllib.request

    cached = _release_digest_cache.get((repo, tag))
    if cached is not None:
        return cached
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {"User-Agent": "hermes-pm"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers),
            timeout=120,
        ) as resp:
            release = json.load(resp)
    except Exception:
        return {}
    digests = {}
    for asset in release.get("assets", []):
        digest = (asset.get("digest") or "").partition("sha256:")[2]
        if digest:
            digests[asset["name"]] = digest
    _release_digest_cache[(repo, tag)] = digests
    return digests


_release_digest_cache: dict[tuple, dict] = {}


@register
class LlamaCppCuda(LlamaCpp):
    """Upstream publishes no prebuilt Linux CUDA archive at current tags, so
    NVIDIA Linux users run the vulkan build."""

    name = "llamacpp-cuda"
    backend = "cuda"


@register
class LlamaCppVulkan(LlamaCpp):
    name = "llamacpp-vulkan"
    backend = "vulkan"
    assets = {
        "linux-x64": "ubuntu-vulkan-x64",
        "linux-arm64": "ubuntu-vulkan-arm64",
    }


@register
class LlamaCppMetal(LlamaCpp):
    """macOS archives are unified builds with Metal compiled in."""

    name = "llamacpp-metal"
    backend = "metal"
    assets = {"darwin-x64": "macos-x64", "darwin-arm64": "macos-arm64"}


@register
class LlamaCppCpu(LlamaCpp):
    name = "llamacpp-cpu"
    backend = "cpu"
    assets = {
        "linux-x64": "ubuntu-x64",
        "linux-arm64": "ubuntu-arm64",
        "darwin-x64": "macos-x64",
        "darwin-arm64": "macos-arm64",
    }
