"""Profile copy surfaces respect the install/profile bucket split.

Runtime artifacts belong to an install; config, sessions and skills belong
to a profile. Profile clone/export never copies install artifacts.
"""

import pytest


class TestProfileCopyExclusions:
    @pytest.mark.parametrize(
        "excluded", [".hermes-runtime", "node", "hermes-agent", "profiles"]
    )
    def test_clone_all_excludes_install_artifacts(self, excluded):
        from hermes_cli.profiles import _CLONE_ALL_DEFAULT_EXCLUDE_ROOT

        assert excluded in _CLONE_ALL_DEFAULT_EXCLUDE_ROOT

    @pytest.mark.parametrize("excluded", [".hermes-runtime", "node"])
    def test_export_excludes_managed_runtimes(self, excluded):
        from hermes_cli.profiles import _DEFAULT_EXPORT_INCLUDE_ROOT

        # Export allow-lists: an install artifact outside the allow-list is excluded.
        assert excluded not in _DEFAULT_EXPORT_INCLUDE_ROOT

    @pytest.mark.parametrize("excluded", [".hermes-runtime", "node"])
    def test_distribution_excludes_managed_runtimes(self, excluded):
        from hermes_cli.profile_distribution import USER_OWNED_EXCLUDE

        assert excluded in USER_OWNED_EXCLUDE

    def test_profile_state_is_still_copied(self):
        """The exclusions must not swallow actual profile data."""
        from hermes_cli.profiles import _CLONE_ALL_DEFAULT_EXCLUDE_ROOT

        for kept in ("config.yaml", "skills", "memories", "SOUL.md"):
            assert kept not in _CLONE_ALL_DEFAULT_EXCLUDE_ROOT
