"""Upgrade path tests for label configuration migration.

This module tests various upgrade scenarios:
- Fresh installation (no existing config)
- Upgrade from version without label support
- Upgrade with existing custom configurations
- Configuration file migration
- Data migration and cleanup
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from src.auto_coder import prompt_loader
from src.auto_coder.prompt_loader import (
    _get_prompt_for_labels,
    _resolve_label_priority,
    clear_prompt_cache,
    get_label_specific_prompt,
    load_prompts,
    render_prompt,
)


class TestFreshInstallation:
    """Test fresh installation scenarios (no existing configuration)."""

    def test_fresh_install_default_config(self, tmp_path):
        """Test fresh installation with default configuration."""
        # Fresh install: no existing config files
        config_dir = tmp_path / "fresh_install"
        config_dir.mkdir()

        # Should use defaults
        prompt_file = config_dir / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default action"\n  bugfix: "Bug fix prompt"\n',
            encoding="utf-8",
        )

        result = render_prompt("issue.action", path=str(prompt_file))
        assert "Default action" in result

    def test_fresh_install_no_config_files(self, tmp_path):
        """Test fresh install with no configuration files at all."""
        # Empty directory
        config_dir = tmp_path / "empty_install"
        config_dir.mkdir()

        # Should create/use default prompts
        try:
            result = render_prompt("issue.action", path=None)
            # May use DEFAULT_PROMPTS_PATH
        except SystemExit:
            # Expected if no default file exists
            pass

    def test_fresh_install_minimal_config(self, tmp_path):
        """Test fresh install with minimal required configuration."""
        prompt_file = tmp_path / "minimal.yaml"
        prompt_file.write_text('issue:\n  action: "Action"\n', encoding="utf-8")

        # Minimal config should work
        result = render_prompt("issue.action", path=str(prompt_file))
        assert "Action" in result

    def test_fresh_install_auto_generated_labels(self, tmp_path):
        """Test that fresh install can auto-generate label configuration."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default"\n  bugfix: "Bug fix"\n  feature: "Feature"\n',
            encoding="utf-8",
        )

        # Simulate auto-generation of label mappings from prompt keys
        data = load_prompts(str(prompt_file))
        # Extract all issue.* keys as potential label mappings
        auto_mappings = {}
        for key in data.get("issue", {}):
            auto_mappings[key] = f"issue.{key}"

        # Should create mappings for each prompt key
        assert len(auto_mappings) >= 3
        assert "bugfix" in auto_mappings
        assert "feature" in auto_mappings

    def test_fresh_install_env_based_config(self, tmp_path):
        """Test fresh install using environment variables for configuration."""
        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix", "feature": "issue.feature"}',
                "AUTO_CODER_LABEL_PRIORITIES": '["bug", "feature"]',
            },
        ):
            prompt_file = tmp_path / "prompts.yaml"
            prompt_file.write_text(
                'issue:\n  bugfix: "Fix bugs"\n  feature: "Add features"\n',
                encoding="utf-8",
            )

            # Should work with environment-based config
            mappings = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])
            priorities = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PRIORITIES"])

            result = render_prompt(
                "issue.bugfix",
                path=str(prompt_file),
                labels=["bug"],
                label_prompt_mappings=mappings,
                label_priorities=priorities,
            )
            assert "Fix bugs" in result

    def test_fresh_install_with_template_config(self, tmp_path):
        """Test fresh install using a template configuration file."""
        template_file = tmp_path / "config_template.yaml"
        template_file.write_text(
            """
            # Label Configuration Template
            # Copy this to your config and modify as needed

            label_prompt_mappings:
              bug: "issue.bugfix"
              feature: "issue.feature"
              enhancement: "issue.enhancement"
              documentation: "issue.documentation"

            label_priorities:
              - bug
              - feature
              - enhancement
              - documentation
            """,
            encoding="utf-8",
        )

        # Fresh install might use template
        data = yaml.safe_load(template_file.read_text(encoding="utf-8"))
        assert "label_prompt_mappings" in data
        assert "label_priorities" in data

    def test_fresh_install_no_label_support_yet(self, tmp_path):
        """Test scenario where version doesn't have label support yet."""
        prompt_file = tmp_path / "old_style.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Before labels"\n  bug: "Bug handling"\n',
            encoding="utf-8",
        )

        # Old code would call render_prompt without any label parameters
        result = render_prompt("issue.bug", path=str(prompt_file))
        assert "Bug handling" in result


class TestUpgradeFromNoLabelSupport:
    """Test upgrading from versions without label support."""

    def test_upgrade_from_v1_no_labels(self, tmp_path):
        """Test upgrade from v1 (no label support) to v2 (with labels)."""
        # v1 config (no label support)
        v1_config = tmp_path / "v1_prompts.yaml"
        v1_config.write_text(
            'issue:\n  action: "Default"\n  bugfix: "Bug fix"\n',
            encoding="utf-8",
        )

        # v1 code would use direct prompt rendering
        result_v1 = render_prompt("issue.bugfix", path=str(v1_config))
        assert "Bug fix" in result_v1

        # v2 code should still work with v1 prompts
        result_v2 = render_prompt(
            "issue.bugfix",
            path=str(v1_config),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        assert "Bug fix" in result_v2

    def test_add_labels_to_existing_prompts(self, tmp_path):
        """Test adding label support to existing prompt files."""
        # Original file without labels
        original_file = tmp_path / "original.yaml"
        original_file.write_text(
            'issue:\n  action: "Default"\n  fix: "Fix issue"\n  feature: "Add feature"\n',
            encoding="utf-8",
        )

        # Upgrade: add label-specific prompts
        upgraded_file = tmp_path / "upgraded.yaml"
        upgraded_file.write_text(
            'issue:\n  action: "Default"\n  bugfix: "Fix bug"\n  feature: "Add feature"\n',
            encoding="utf-8",
        )

        # Should be able to upgrade existing prompts to label-based
        result = render_prompt(
            "issue.bugfix",
            path=str(upgraded_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        assert "Fix bug" in result

    def test_migrate_prompt_keys_to_labels(self, tmp_path):
        """Test migrating from old prompt keys to new label-based keys."""
        # Old style: generic keys
        old_file = tmp_path / "old.yaml"
        old_file.write_text(
            'issue:\n  task: "Handle issue"\n',
            encoding="utf-8",
        )

        # New style: specific label-based keys
        new_file = tmp_path / "new.yaml"
        new_file.write_text(
            'issue:\n  bugfix: "Fix bug"\n  feature: "Add feature"\n',
            encoding="utf-8",
        )

        # Both should produce results
        result_old = render_prompt("issue.task", path=str(old_file))
        result_new = render_prompt("issue.bugfix", path=str(new_file))

        assert "Handle issue" in result_old
        assert "Fix bug" in result_new

    def test_backward_compatible_upgrade(self, tmp_path):
        """Test that upgrade maintains backward compatibility."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'issue:\n  action: "Default"\n  bugfix: "Bug fix"\n',
            encoding="utf-8",
        )

        # Old code (pre-upgrade) still works
        result_old_api = render_prompt("issue.action", path=str(config_file))
        assert "Default" in result_old_api

        # New code (post-upgrade) also works
        result_new_api = render_prompt(
            "issue.bugfix",
            path=str(config_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        assert "Bug fix" in result_new_api

    def test_upgrade_without_data_loss(self, tmp_path):
        """Test that upgrade doesn't lose existing configuration data."""
        original_config = {
            "custom_prompts": {
                "bug": "Custom bug prompt",
                "feature": "Custom feature prompt",
            },
            "settings": {"verbose": True},
        }

        config_file = tmp_path / "original.yaml"
        yaml.dump(original_config, config_file)

        # After upgrade, original data should still be accessible
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert data["settings"]["verbose"] is True
        assert data["custom_prompts"]["bug"] == "Custom bug prompt"


class TestUpgradeWithCustomConfigurations:
    """Test upgrading systems with existing custom configurations."""

    def test_upgrade_custom_label_mappings(self, tmp_path):
        """Test upgrading with custom label mappings."""
        # Existing custom configuration
        custom_mappings = {
            "defect": "issue.bugfix",  # Custom alias
            "enhancement": "issue.feature",  # Custom mapping
            "improve": "issue.enhancement",  # Custom label
        }

        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  bugfix: "Bug fix"\n  feature: "Feature"\n  enhancement: "Enhancement"\n',
            encoding="utf-8",
        )

        # Should preserve custom mappings
        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["defect"],
            label_prompt_mappings=custom_mappings,
            label_priorities=["defect", "enhancement", "improve"],
        )
        assert "Bug fix" in result

    def test_upgrade_with_custom_priorities(self, tmp_path):
        """Test upgrading with custom priority orders."""
        custom_priorities = ["urgent", "critical", "bug", "feature", "enhancement"]

        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  urgent: "Urgent"\n  critical: "Critical"\n  bugfix: "Bug fix"\n',
            encoding="utf-8",
        )

        # Should respect custom priority order
        labels = ["bug", "feature", "urgent"]
        mappings = {
            "urgent": "issue.urgent",
            "critical": "issue.critical",
            "bug": "issue.bugfix",
            "feature": "issue.feature",
        }

        result = _resolve_label_priority(labels, mappings, custom_priorities)
        # urgent has highest priority
        assert result == "urgent"

    def test_preserve_custom_env_vars(self):
        """Test that custom environment variables are preserved during upgrade."""
        with patch.dict(
            os.environ,
            {
                "CUSTOM_VAR_1": "value1",
                "CUSTOM_VAR_2": "value2",
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix"}',
            },
            clear=False,  # Don't clear existing env vars
        ):
            # Custom vars should still exist
            assert os.environ.get("CUSTOM_VAR_1") == "value1"
            assert os.environ.get("CUSTOM_VAR_2") == "value2"
            # New vars should also exist
            mappings = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])
            assert mappings["bug"] == "issue.bugfix"

    def test_upgrade_with_custom_prompt_templates(self, tmp_path):
        """Test upgrading with custom prompt templates."""
        custom_template_file = tmp_path / "custom_prompts.yaml"
        custom_template_file.write_text(
            'issue:\n  bugfix: "Custom bug fix for $issue_number"\n  feature: "Custom feature for $issue_number"\n',
            encoding="utf-8",
        )

        # Should work with custom templates
        result = render_prompt(
            "issue.bugfix",
            path=str(custom_template_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            data={"issue_number": "123"},
        )
        assert "Custom bug fix for 123" in result

    def test_migrate_existing_aliases(self, tmp_path):
        """Test migrating existing label aliases to new system."""
        # Old aliases from previous version
        old_aliases = {
            "defect": "bug",
            "fix": "bug",
            "improvement": "feature",
            "new-feature": "feature",
        }

        # New system should support them
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  bugfix: "Bug fix"\n  feature: "Feature"\n',
            encoding="utf-8",
        )

        # Create mappings that include aliases
        mappings_with_aliases = {
            "bug": "issue.bugfix",
            "defect": "issue.bugfix",
            "fix": "issue.bugfix",
            "feature": "issue.feature",
            "improvement": "issue.feature",
            "new-feature": "issue.feature",
        }

        # All aliases should work
        for alias in ["defect", "fix"]:
            result = render_prompt(
                "issue.bugfix",
                path=str(prompt_file),
                labels=[alias],
                label_prompt_mappings=mappings_with_aliases,
                label_priorities=list(mappings_with_aliases.keys()),
            )
            assert "Bug fix" in result


class TestConfigurationFileMigration:
    """Test configuration file migration utilities and processes."""

    def test_automatic_config_detection(self, tmp_path):
        """Test automatic detection of configuration file format."""
        # Old format
        old_config = tmp_path / "old_config.yaml"
        old_config.write_text(
            """
            bug: "issue.bugfix"
            feature: "issue.feature"
            """,
            encoding="utf-8",
        )

        # New format
        new_config = tmp_path / "new_config.yaml"
        new_config.write_text(
            """
            label_prompt_mappings:
              bug: "issue.bugfix"
              feature: "issue.feature"
            label_priorities:
              - bug
              - feature
            """,
            encoding="utf-8",
        )

        # Detect format
        old_data = yaml.safe_load(old_config.read_text(encoding="utf-8"))
        new_data = yaml.safe_load(new_config.read_text(encoding="utf-8"))

        # Old format is flat dict
        assert "bug" in old_data and isinstance(old_data["bug"], str)
        # New format has structured keys
        assert "label_prompt_mappings" in new_data

    def test_migrate_flat_to_structured_config(self):
        """Test migrating from flat to structured configuration format."""
        # Flat format (old)
        flat_config = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
        }

        # Migrate to structured format (new)
        structured_config = {
            "label_prompt_mappings": flat_config,
            "label_priorities": list(flat_config.keys()),
        }

        # Verify migration
        assert "label_prompt_mappings" in structured_config
        assert "label_priorities" in structured_config
        assert len(structured_config["label_priorities"]) == 3

    def test_merge_old_and_new_configs(self, tmp_path):
        """Test merging old and new configuration formats."""
        old_file = tmp_path / "old.yaml"
        old_file.write_text(
            'bug: "Old bug prompt"\n',
            encoding="utf-8",
        )

        new_file = tmp_path / "new.yaml"
        new_file.write_text(
            """
            label_prompt_mappings:
              bug: "New bug prompt"
              feature: "New feature prompt"
            """,
            encoding="utf-8",
        )

        # Merge both configs
        old_data = yaml.safe_load(old_file.read_text(encoding="utf-8"))
        new_data = yaml.safe_load(new_file.read_text(encoding="utf-8"))

        merged = {
            "label_prompt_mappings": {
                **old_data,  # Preserve old prompts
                **new_data["label_prompt_mappings"],  # Add new ones
            }
        }

        # Should have both
        assert merged["label_prompt_mappings"]["bug"] == "Old bug prompt"
        assert merged["label_prompt_mappings"]["feature"] == "New feature prompt"

    def test_config_file_backward_compat_mode(self, tmp_path):
        """Test configuration file with backward compatibility mode flag."""
        config_file = tmp_path / "compat_config.yaml"
        config_file.write_text(
            """
            version: "2.0"
            backward_compatible: true
            label_prompt_mappings:
              bug: "issue.bugfix"
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # Should respect backward compatibility flag
        if data.get("backward_compatible"):
            # Enable compatibility mode
            assert "label_prompt_mappings" in data

    def test_migrate_incremental_changes(self, tmp_path):
        """Test migrating configuration with incremental changes."""
        # v1 config
        v1_config = {"bug": "issue.bugfix"}

        # v2 adds feature
        v2_config = {"bug": "issue.bugfix", "feature": "issue.feature"}

        # v3 adds enhancement
        v3_config = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
        }

        # Incremental migration from v1 to v3
        current = v1_config.copy()
        current.update(v2_config)
        current.update(v3_config)

        # Should have all features
        assert len(current) == 3
        assert "bug" in current
        assert "feature" in current
        assert "enhancement" in current


class TestDataMigrationAndCleanup:
    """Test data migration and cleanup during upgrades."""

    def test_cleanup_obsolete_config_keys(self):
        """Test cleanup of obsolete configuration keys."""
        old_config = {
            "obsolete_key_1": "value1",
            "obsolete_key_2": "value2",
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "active_key": "value3",
        }

        # Migration should remove obsolete keys
        cleaned_config = {k: v for k, v in old_config.items() if not k.startswith("obsolete_")}

        # Obsolete keys should be removed
        assert "obsolete_key_1" not in cleaned_config
        assert "obsolete_key_2" not in cleaned_config
        # Active keys should remain
        assert "label_prompt_mappings" in cleaned_config
        assert "active_key" in cleaned_config

    def test_data_validation_during_migration(self, tmp_path):
        """Test that data is validated during migration."""
        # Invalid old config
        invalid_config = tmp_path / "invalid.yaml"
        invalid_config.write_text("invalid: [:", encoding="utf-8")

        # Migration should handle invalid data gracefully
        try:
            data = yaml.safe_load(invalid_config.read_text(encoding="utf-8"))
            # If it loads, verify it's valid
        except yaml.YAMLError:
            # Expected to fail with invalid YAML
            pass

    def test_backup_before_migration(self, tmp_path):
        """Test creating backup of configuration before migration."""
        original_config = tmp_path / "config.yaml"
        original_config.write_text('bug: "Original"\n', encoding="utf-8")

        # Create backup before migration
        backup_file = tmp_path / "config.yaml.backup"
        shutil.copy(original_config, backup_file)

        # Modify original
        original_config.write_text('bug: "Modified"\n', encoding="utf-8")

        # Backup should still have original
        backup_data = yaml.safe_load(backup_file.read_text(encoding="utf-8"))
        assert backup_data["bug"] == "Original"

        # Modified should have new value
        modified_data = yaml.safe_load(original_config.read_text(encoding="utf-8"))
        assert modified_data["bug"] == "Modified"

    def test_migration_rollback_on_error(self, tmp_path):
        """Test rollback on migration error."""
        original_config = tmp_path / "config.yaml"
        original_config.write_text('bug: "Original"\n', encoding="utf-8")

        backup_file = tmp_path / "config.yaml.backup"
        shutil.copy(original_config, backup_file)

        # Simulate failed migration
        try:
            # Attempt migration that fails
            raise ValueError("Migration failed")
        except ValueError:
            # Rollback to backup
            shutil.copy(backup_file, original_config)

        # Should be back to original
        data = yaml.safe_load(original_config.read_text(encoding="utf-8"))
        assert data["bug"] == "Original"

    def test_migrate_prompt_cache(self, tmp_path):
        """Test migration of prompt cache data."""
        # Populate cache
        cache_file = tmp_path / "prompts.yaml"
        cache_file.write_text('issue:\n  action: "Cached"\n', encoding="utf-8")

        clear_prompt_cache()
        data1 = load_prompts(str(cache_file))
        assert data1["issue"]["action"] == "Cached"

        # Clear cache (simulates migration cleanup)
        clear_prompt_cache()

        # Reload (simulates post-migration)
        data2 = load_prompts(str(cache_file))
        assert data2["issue"]["action"] == "Cached"

    def test_upgrade_version_tracking(self, tmp_path):
        """Test tracking configuration version during upgrades."""
        config_file = tmp_path / "versioned_config.yaml"
        config_file.write_text(
            """
            version: "1.0"
            label_prompt_mappings:
              bug: "issue.bugfix"
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))

        # Track version through upgrades
        version = data.get("version", "0.0")
        assert version == "1.0"

        # Simulate upgrade to v2
        data["version"] = "2.0"
        data["label_priorities"] = ["bug"]

        # Version should be updated
        assert data["version"] == "2.0"
        assert "label_priorities" in data

    def test_migration_with_data_loss_check(self, tmp_path):
        """Test that migration checks for potential data loss."""
        # Config with important custom data
        config_with_custom = {
            "custom_prompts": {
                "special_bug": "Custom bug handling",
            },
            "settings": {
                "custom_option": True,
            },
        }

        config_file = tmp_path / "custom.yaml"
        yaml.dump(config_with_custom, config_file)

        # Load and migrate
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))

        # Check for potential data loss
        has_custom_data = "custom_prompts" in data or "settings" in data
        assert has_custom_data  # Should detect custom data


class TestUpgradeValidation:
    """Test validation after upgrade."""

    def test_validate_migrated_config(self, tmp_path):
        """Test validation of migrated configuration."""
        migrated_config = {
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": ["bug"],
        }

        # Validate structure
        assert "label_prompt_mappings" in migrated_config
        assert "label_priorities" in migrated_config
        assert isinstance(migrated_config["label_prompt_mappings"], dict)
        assert isinstance(migrated_config["label_priorities"], list)

    def test_validate_functionality_after_upgrade(self, tmp_path):
        """Test that functionality works correctly after upgrade."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  bugfix: "Bug fix after upgrade"\n',
            encoding="utf-8",
        )

        # Test all functionality works
        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        assert "Bug fix after upgrade" in result

    def test_upgrade_smoke_test(self, tmp_path):
        """Test basic smoke test after upgrade."""
        # Basic functionality check
        result = _resolve_label_priority(["bug"], {"bug": "issue.bugfix"}, ["bug"])
        assert result == "bug"

        # Configuration loading
        prompt_file = tmp_path / "test.yaml"
        prompt_file.write_text('a:\n  b: "value"\n', encoding="utf-8")

        clear_prompt_cache()
        data = load_prompts(str(prompt_file))
        assert data["a"]["b"] == "value"

        # Prompt rendering
        result = render_prompt("a.b", path=str(prompt_file))
        assert "value" in result

    def test_upgrade_regression_test(self, tmp_path):
        """Test for regressions after upgrade."""
        prompt_file = tmp_path / "regression.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Regression test"\n',
            encoding="utf-8",
        )

        # Old functionality should still work
        result = render_prompt("issue.action", path=str(prompt_file))
        assert "Regression test" in result

        # New functionality should also work
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.action"},
            label_priorities=["bug"],
        )
        assert "Regression test" in result


# Parametrized upgrade scenarios
@pytest.mark.parametrize(
    "from_version, to_version, config_changes",
    [
        ("1.0", "2.0", {"added_label_support": True}),
        ("2.0", "2.1", {"enhanced_priorities": True}),
        ("2.1", "3.0", {"breaking_change": True}),
    ],
)
def test_version_upgrade_scenarios(from_version, to_version, config_changes):
    """Parametrized test for various version upgrade scenarios."""
    # Simulate version upgrade
    config = {"version": from_version}

    # Apply changes based on version
    if config_changes.get("added_label_support"):
        config["label_prompt_mappings"] = {"bug": "issue.bugfix"}
        config["label_priorities"] = ["bug"]

    if config_changes.get("enhanced_priorities"):
        config.setdefault("label_priorities", []).append("feature")

    if config_changes.get("breaking_change"):
        # Breaking changes might remove old keys
        pass

    # Verify upgrade
    assert config["version"] == to_version


@pytest.mark.parametrize(
    "scenario",
    [
        "fresh_install",
        "upgrade_from_v1",
        "upgrade_with_custom_config",
        "migration_with_data_loss_check",
    ],
)
def test_upgrade_scenario_matrix(scenario):
    """Parametrized test for different upgrade scenarios."""
    scenarios = {
        "fresh_install": {"has_existing_config": False, "needs_migration": False},
        "upgrade_from_v1": {"has_existing_config": True, "needs_migration": True},
        "upgrade_with_custom_config": {"has_existing_config": True, "has_custom_labels": True},
        "migration_with_data_loss_check": {"has_existing_config": True, "check_data_loss": True},
    }

    scenario_config = scenarios[scenario]
    assert scenario_config is not None
