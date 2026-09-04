"""Unit tests for quota surplus-based high score backend selection."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.claude_usage_checker import ClaudeUsageQuota, ClaudeUsageWindow
from auto_coder.cli_helpers import (
    create_cloud_backend_manager,
    create_high_score_backend_manager,
    create_high_score_cloud_backend_manager,
)
from auto_coder.codex_usage_checker import CodexResetCredits, CodexWeeklyUsage, parse_codex_weekly_usage
from auto_coder.issue_processor import _process_issue_cloud_backend, _process_issue_high_score_cloud
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.quota_selector import (
    WEEK_SECONDS,
    BackendQuotaEvaluation,
    calculate_quota_surplus,
    evaluate_backend_quota,
    linear_planned_remaining_ratio,
    rank_high_score_backends_by_quota,
)


class TestLinearPlannedRemainingRatio:
    """Test calculations for planned remaining quota and quota surplus."""

    def test_linear_planned_ratio_at_start_of_week(self):
        """At 7 days remaining, planned ratio is 1.0 (100%)."""
        ratio = linear_planned_remaining_ratio(7 * 86400, WEEK_SECONDS)
        assert pytest.approx(ratio, rel=1e-4) == 1.0

    def test_linear_planned_ratio_at_half_week(self):
        """At 3.5 days remaining, planned ratio is 0.5 (50%)."""
        ratio = linear_planned_remaining_ratio(3.5 * 86400, WEEK_SECONDS)
        assert pytest.approx(ratio, rel=1e-4) == 0.5

    def test_linear_planned_ratio_at_reset(self):
        """At 0 seconds remaining, planned ratio is 0.0 (0%)."""
        ratio = linear_planned_remaining_ratio(0, WEEK_SECONDS)
        assert ratio == 0.0

    def test_linear_planned_ratio_clamping(self):
        """Values beyond bounds [0, WEEK_SECONDS] are clamped."""
        assert linear_planned_remaining_ratio(-100, WEEK_SECONDS) == 0.0
        assert linear_planned_remaining_ratio(10 * 86400, WEEK_SECONDS) == 1.0
        assert linear_planned_remaining_ratio(100, 0) == 0.0

    def test_calculate_quota_surplus(self):
        """Test surplus calculation for positive, zero, and negative values."""
        # 35% actual vs 28.57% planned -> +6.43% surplus
        surplus = calculate_quota_surplus(0.35, 2.0 / 7.0)
        assert pytest.approx(surplus, rel=1e-3) == 0.06428

        # 40% actual vs 57.14% planned -> -17.14% surplus
        surplus_negative = calculate_quota_surplus(0.40, 4.0 / 7.0)
        assert pytest.approx(surplus_negative, rel=1e-3) == -0.17143

        # Exactly on plan
        assert calculate_quota_surplus(0.5, 0.5) == 0.0


class TestQuotaSurplusSelection:
    """Test candidate backend evaluation and ranking by quota surplus."""

    @pytest.fixture
    def fixed_now(self):
        return datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    def test_independent_reset_schedules_and_surplus_preference(self, fixed_now):
        """Test Codex Cloud vs Claude Cloud independent reset schedules from prompt example.

        Codex Cloud:
          time until reset: 2 days
          actual remaining: 35%
          planned remaining: 2 / 7 = 28.6%
          surplus: +6.4%

        Claude Cloud:
          time until reset: 4 days
          actual remaining: 40%
          planned remaining: 4 / 7 = 57.1%
          surplus: -17.1%

        Codex Cloud should be preferred due to larger positive surplus.
        """
        codex_reset = fixed_now + timedelta(days=2)
        claude_reset = fixed_now + timedelta(days=4)

        codex_usage = CodexWeeklyUsage(
            remaining_percent=35.0,
            reset_at=codex_reset,
            days_until_reset=2,
            minimum_remaining_percent=5.0,
        )

        claude_quota = ClaudeUsageQuota(
            seven_day=ClaudeUsageWindow(
                utilization=60.0,  # 40% remaining
                resets_at=claude_reset.isoformat(),
            ),
            is_quota_insufficient=False,
        )

        config = LLMBackendConfiguration(
            backends={
                "codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud"),
                "claude-routine": BackendConfig(name="claude-routine", backend_type="claude-routine"),
            }
        )

        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=codex_usage), patch("auto_coder.claude_usage_checker.check_claude_usage", return_value=claude_quota), patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="test-token"):

            ranked = rank_high_score_backends_by_quota(
                {"claude-routine", "codex-cloud"},
                config=config,
                now=fixed_now,
            )

            assert ranked == ["codex-cloud", "claude-routine"]

    def test_nearest_reset_not_selected_when_quota_exhausted(self, fixed_now):
        """Test that a backend with nearest reset is NOT selected when its quota is lagging behind.

        Codex: 2 days until reset, 10% remaining (planned: 28.6%, surplus: -18.6%)
        Claude: 4 days until reset, 80% remaining (planned: 57.1%, surplus: +22.9%)

        Claude should be preferred even though Codex resets sooner.
        """
        codex_reset = fixed_now + timedelta(days=2)
        claude_reset = fixed_now + timedelta(days=4)

        codex_usage = CodexWeeklyUsage(
            remaining_percent=10.0,
            reset_at=codex_reset,
            days_until_reset=2,
            minimum_remaining_percent=5.0,
        )

        claude_quota = ClaudeUsageQuota(
            seven_day=ClaudeUsageWindow(
                utilization=20.0,  # 80% remaining
                resets_at=claude_reset.isoformat(),
            ),
            is_quota_insufficient=False,
        )

        config = LLMBackendConfiguration(
            backends={
                "codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud"),
                "claude-routine": BackendConfig(name="claude-routine", backend_type="claude-routine"),
            }
        )

        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=codex_usage), patch("auto_coder.claude_usage_checker.check_claude_usage", return_value=claude_quota), patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="test-token"):

            ranked = rank_high_score_backends_by_quota(
                {"codex-cloud", "claude-routine"},
                config=config,
                now=fixed_now,
            )

            # Claude must be preferred first within the equal-priority group.
            assert ranked == ["claude-routine", "codex-cloud"]

    def test_backend_ahead_and_behind_consumption_curve(self, fixed_now):
        """Verify surplus calculation for backends ahead and behind planned consumption."""
        # Backend A: 3 days until reset (planned = 3/7 = 42.86%), actual = 70% -> surplus = +27.14% (behind consumption curve)
        # Backend B: 5 days until reset (planned = 5/7 = 71.43%), actual = 50% -> surplus = -21.43% (ahead of consumption curve)
        reset_a = fixed_now + timedelta(days=3)
        reset_b = fixed_now + timedelta(days=5)

        eval_a = BackendQuotaEvaluation(
            backend_name="backend_a",
            is_eligible=True,
            actual_remaining_ratio=0.70,
            time_until_reset_seconds=3 * 86400,
            planned_remaining_ratio=3.0 / 7.0,
            quota_surplus=calculate_quota_surplus(0.70, 3.0 / 7.0),
            reset_at=reset_a,
        )
        eval_b = BackendQuotaEvaluation(
            backend_name="backend_b",
            is_eligible=True,
            actual_remaining_ratio=0.50,
            time_until_reset_seconds=5 * 86400,
            planned_remaining_ratio=5.0 / 7.0,
            quota_surplus=calculate_quota_surplus(0.50, 5.0 / 7.0),
            reset_at=reset_b,
        )

        assert eval_a.quota_surplus > 0
        assert eval_b.quota_surplus < 0

        with patch("auto_coder.quota_selector.evaluate_backend_quota") as mock_eval:
            mock_eval.side_effect = lambda backend_name, **kwargs: eval_a if backend_name == "backend_a" else eval_b

            ranked = rank_high_score_backends_by_quota({"backend_b", "backend_a"}, now=fixed_now)
            assert ranked == ["backend_a", "backend_b"]

    def test_ordered_candidates_keep_priority_after_eligibility_filtering(self, fixed_now):
        """Quota scores cannot reorder eligible members of an ordered list."""
        evaluations = {
            "backend-a": BackendQuotaEvaluation(backend_name="backend-a", is_eligible=False),
            "backend-b": BackendQuotaEvaluation(backend_name="backend-b", quota_surplus=-0.9),
            "backend-c": BackendQuotaEvaluation(backend_name="backend-c", quota_surplus=0.9),
        }

        with patch(
            "auto_coder.quota_selector.evaluate_backend_quota",
            side_effect=lambda backend_name, **kwargs: evaluations[backend_name],
        ):
            ranked = rank_high_score_backends_by_quota(["backend-a", "backend-b", "backend-c"], now=fixed_now)

        assert ranked == ["backend-b", "backend-c"]

    def test_quota_ranking_stays_inside_equal_priority_group(self, fixed_now):
        """A later group cannot cross an earlier ordered priority boundary."""
        evaluations = {
            "backend-a": BackendQuotaEvaluation(backend_name="backend-a", quota_surplus=-0.8),
            "backend-b": BackendQuotaEvaluation(backend_name="backend-b", quota_surplus=-0.4),
            "backend-c": BackendQuotaEvaluation(backend_name="backend-c", quota_surplus=0.9),
        }

        with patch(
            "auto_coder.quota_selector.evaluate_backend_quota",
            side_effect=lambda backend_name, **kwargs: evaluations[backend_name],
        ):
            ranked = rank_high_score_backends_by_quota([{"backend-a", "backend-b"}, "backend-c"], now=fixed_now)

        assert ranked == ["backend-b", "backend-a", "backend-c"]

    def test_burst_prefers_earliest_reset_in_equal_priority_group(self, fixed_now):
        evaluations = {
            "later": BackendQuotaEvaluation(backend_name="later", actual_remaining_ratio=0.8, time_until_reset_seconds=500),
            "earlier": BackendQuotaEvaluation(backend_name="earlier", actual_remaining_ratio=0.2, time_until_reset_seconds=100),
        }
        config = LLMBackendConfiguration(quota_selection_strategy="burst")
        with patch("auto_coder.quota_selector.evaluate_backend_quota", side_effect=lambda backend_name, **kwargs: evaluations[backend_name]):
            ranked = rank_high_score_backends_by_quota({"later", "earlier"}, config=config, now=fixed_now)
        assert ranked == ["earlier", "later"]

    def test_burst_does_not_cross_ordered_priority_boundaries(self, fixed_now):
        evaluations = {
            "first": BackendQuotaEvaluation(backend_name="first", actual_remaining_ratio=0.5, time_until_reset_seconds=500),
            "later": BackendQuotaEvaluation(backend_name="later", actual_remaining_ratio=0.5, time_until_reset_seconds=1),
        }
        config = LLMBackendConfiguration(quota_selection_strategy="burst")
        with patch("auto_coder.quota_selector.evaluate_backend_quota", side_effect=lambda backend_name, **kwargs: evaluations[backend_name]):
            ranked = rank_high_score_backends_by_quota(["first", "later"], config=config, now=fixed_now)
        assert ranked == ["first", "later"]

    def test_loaded_burst_configuration_reorders_only_declared_equal_group(self, fixed_now):
        """Production config input preserves the group through the selector boundary."""
        config = LLMBackendConfiguration.load_from_dict(
            {
                "quota_selection": {"strategy": "burst"},
                "backend_with_high_score": {"order": [["later", "earlier"], "metered"]},
            }
        )
        evaluations = {
            "later": BackendQuotaEvaluation(backend_name="later", actual_remaining_ratio=0.8, time_until_reset_seconds=500),
            "earlier": BackendQuotaEvaluation(backend_name="earlier", actual_remaining_ratio=0.2, time_until_reset_seconds=100),
            "metered": BackendQuotaEvaluation(backend_name="metered"),
        }
        with patch("auto_coder.quota_selector.evaluate_backend_quota", side_effect=lambda backend_name, **kwargs: evaluations[backend_name]):
            ranked = rank_high_score_backends_by_quota(config.backend_with_high_score_order, config=config, now=fixed_now)
        assert config.quota_selection_strategy == "burst"
        assert ranked == ["earlier", "later", "metered"]

    def test_burst_strategy_and_equal_group_survive_config_round_trip(self, tmp_path):
        path = tmp_path / "llm_config.toml"
        config = LLMBackendConfiguration.load_from_dict(
            {
                "quota_selection": {"strategy": "burst"},
                "backend_with_high_score": {"order": [["codex-cloud", "claude-routine"], "muse"]},
            }
        )
        config.save_to_file(str(path))
        restored = LLMBackendConfiguration.load_from_file(str(path))
        assert restored.quota_selection_strategy == "burst"
        assert restored.backend_with_high_score_order == [["codex-cloud", "claude-routine"], "muse"]

    @pytest.mark.parametrize(("count", "expected"), [(1, True), (0, False)])
    def test_exhausted_codex_exposes_reset_credit_state(self, fixed_now, count, expected):
        usage = CodexWeeklyUsage(
            remaining_percent=0,
            reset_at=fixed_now + timedelta(days=2),
            days_until_reset=2,
            minimum_remaining_percent=15,
            reset_credits=CodexResetCredits(available_count=count, status="available"),
        )
        config = LLMBackendConfiguration(backends={"codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud")})
        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage):
            evaluation = evaluate_backend_quota("codex-cloud", config=config, now=fixed_now)
        assert evaluation.is_eligible is False
        assert evaluation.reset_credit_count == count
        assert evaluation.reset_credit_available is expected

    def test_failed_codex_usage_has_unknown_quota_and_credit_state(self, fixed_now):
        config = LLMBackendConfiguration(backends={"codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud")})
        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=None):
            evaluation = evaluate_backend_quota("codex-cloud", config=config, now=fixed_now)
        assert evaluation.usage_retrieval_failed is True
        assert evaluation.actual_remaining_ratio is None
        assert evaluation.reset_credit_available is None

    def test_ineligible_backend_is_filtered_out(self, fixed_now):
        """Test that ineligibility (quota limit reached, disabled, missing credentials) filters out candidates."""
        codex_reset = fixed_now + timedelta(days=2)

        # Codex has 2% remaining but requires minimum 15% (days_until_reset=2 -> threshold=15%)
        codex_usage = CodexWeeklyUsage(
            remaining_percent=2.0,
            reset_at=codex_reset,
            days_until_reset=2,
            minimum_remaining_percent=15.0,
        )
        assert codex_usage.can_start_task is False

        claude_reset = fixed_now + timedelta(days=4)
        claude_quota = ClaudeUsageQuota(
            seven_day=ClaudeUsageWindow(
                utilization=50.0,
                resets_at=claude_reset.isoformat(),
            ),
            is_quota_insufficient=False,
        )

        config = LLMBackendConfiguration(
            backends={
                "codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud"),
                "claude-routine": BackendConfig(name="claude-routine", backend_type="claude-routine"),
            }
        )

        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=codex_usage), patch("auto_coder.claude_usage_checker.check_claude_usage", return_value=claude_quota), patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="test-token"):

            ranked = rank_high_score_backends_by_quota(
                ["codex-cloud", "claude-routine"],
                config=config,
                now=fixed_now,
            )

            assert ranked == ["claude-routine"]

    @pytest.mark.parametrize(("strategy", "expected"), [("burst", ["codex-cloud", "qwen"]), ("surplus", ["qwen"])])
    def test_strategy_admission_from_provider_payload_through_ranking(self, fixed_now, strategy, expected):
        """Provider usage and loaded config retain strategy semantics through selection."""
        reset_at = fixed_now + timedelta(days=2)
        usage = parse_codex_weekly_usage(
            {
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 88,
                        "limit_window_seconds": 604_800,
                        "reset_at": reset_at.timestamp(),
                    }
                },
                "rate_limit_reset_credits": {"available_count": 1},
            },
            now=fixed_now,
        )
        config = LLMBackendConfiguration.load_from_dict(
            {
                "quota_selection": {"strategy": strategy},
                "backends": {
                    "codex-cloud": {"type": "codex-cloud"},
                    "qwen": {"type": "qwen"},
                },
            }
        )
        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage):
            ranked = rank_high_score_backends_by_quota(["codex-cloud", "qwen"], config=config, now=fixed_now)
        assert usage.remaining_percent == 12
        assert usage.minimum_remaining_percent == 15
        assert ranked == expected

    def test_unmetered_and_fallback_backends_preserve_order(self, fixed_now):
        """Test that unmetered backends retain stable ordering when quota metrics are not available."""
        config = LLMBackendConfiguration(
            backends={
                "qwen": BackendConfig(name="qwen", backend_type="qwen"),
                "antigravity": BackendConfig(name="antigravity", backend_type="antigravity"),
            }
        )

        ranked = rank_high_score_backends_by_quota(["qwen", "antigravity"], config=config, now=fixed_now)
        assert ranked == ["qwen", "antigravity"]

    def test_unretrieved_claude_usage_lowers_priority_below_measured_backend(self, fixed_now):
        """Test that a backend whose Claude usage data could not be retrieved has lower priority than a measured backend."""
        codex_reset = fixed_now + timedelta(days=2)
        codex_usage = CodexWeeklyUsage(
            remaining_percent=35.0,
            reset_at=codex_reset,
            days_until_reset=2,
            minimum_remaining_percent=5.0,
        )

        claude_unretrieved = ClaudeUsageQuota(
            is_quota_insufficient=True,
            reason="Claude usage data could not be retrieved",
        )

        config = LLMBackendConfiguration(
            backends={
                "claude-routine": BackendConfig(name="claude-routine", backend_type="claude-routine"),
                "codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud"),
            }
        )

        with (
            patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=codex_usage),
            patch("auto_coder.claude_usage_checker.check_claude_usage", return_value=claude_unretrieved),
            patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="test-token"),
        ):
            ranked = rank_high_score_backends_by_quota(
                {"claude-routine", "codex-cloud"},
                config=config,
                now=fixed_now,
            )
            assert ranked == ["codex-cloud", "claude-routine"]

    def test_ordered_unretrieved_claude_stays_ahead_of_unmetered_backend(self, fixed_now):
        """An ordered backend stays ahead even when its usage cannot be retrieved."""
        claude_unretrieved = ClaudeUsageQuota(
            is_quota_insufficient=True,
            reason="Claude usage data could not be retrieved",
        )

        config = LLMBackendConfiguration(
            backends={
                "claude-routine": BackendConfig(name="claude-routine", backend_type="claude-routine"),
                "antigravity": BackendConfig(name="antigravity", backend_type="antigravity"),
            }
        )

        with (
            patch("auto_coder.claude_usage_checker.check_claude_usage", return_value=claude_unretrieved),
            patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="test-token"),
        ):
            ranked = rank_high_score_backends_by_quota(
                ["claude-routine", "antigravity"],
                config=config,
                now=fixed_now,
            )
            assert ranked == ["claude-routine", "antigravity"]

    def test_ordered_unretrieved_codex_stays_ahead_of_unmetered_backend(self, fixed_now):
        """Ordered Codex Cloud stays ahead when its usage is unavailable."""
        config = LLMBackendConfiguration(
            backends={
                "codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud"),
                "qwen": BackendConfig(name="qwen", backend_type="qwen"),
            }
        )

        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=None):
            ranked = rank_high_score_backends_by_quota(
                ["codex-cloud", "qwen"],
                config=config,
                now=fixed_now,
            )
            assert ranked == ["codex-cloud", "qwen"]

    def test_ordered_usage_statuses_preserve_configured_order(self, fixed_now):
        """Usage retrieval and metering status do not change ordered priorities."""
        claude_unretrieved = ClaudeUsageQuota(
            is_quota_insufficient=True,
            reason="Claude usage data could not be retrieved",
        )

        config = LLMBackendConfiguration(
            backends={
                "claude-routine": BackendConfig(name="claude-routine", backend_type="claude-routine"),
                "codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud"),
                "qwen": BackendConfig(name="qwen", backend_type="qwen"),
            }
        )

        with (
            patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=None),
            patch("auto_coder.claude_usage_checker.check_claude_usage", return_value=claude_unretrieved),
            patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="test-token"),
        ):
            ranked = rank_high_score_backends_by_quota(
                ["claude-routine", "codex-cloud", "qwen"],
                config=config,
                now=fixed_now,
            )
            assert ranked == ["claude-routine", "codex-cloud", "qwen"]

    def test_custom_consumption_curve_isolation(self, fixed_now):
        """Test that a custom consumption curve can be supplied."""
        # Custom non-linear curve (e.g. exponential or step curve)
        custom_curve = lambda time_left, period: 0.1  # Always assumes planned remaining is 10%

        eval_res = evaluate_backend_quota(
            "generic",
            consumption_curve=custom_curve,
            now=fixed_now,
        )
        assert eval_res.is_eligible is True


class TestHighScoreBackendManagerIntegration:
    """Test integration of quota selector with create_high_score_backend_manager."""

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.build_backend_manager")
    @patch("auto_coder.quota_selector.rank_high_score_backends_by_quota")
    def test_create_high_score_backend_manager_preserves_order(self, mock_rank, mock_build, mock_get_config):
        """An ordered high-score configuration keeps its declared priority."""
        mock_config = MagicMock(spec=LLMBackendConfiguration)
        mock_config.backend_with_high_score_order = ["backend-b", "backend-a"]
        mock_config.get_backend_with_high_score.return_value = None
        mock_config.get_model_for_backend.side_effect = lambda x: f"model-{x}"
        mock_get_config.return_value = mock_config

        mock_rank.return_value = ["backend-b", "backend-a"]

        create_high_score_backend_manager()

        mock_rank.assert_called_once_with(["backend-b", "backend-a"], mock_config)
        mock_build.assert_called_once()
        call_args = mock_build.call_args[1]
        assert call_args["selected_backends"] == ["backend-b", "backend-a"]
        assert call_args["primary_backend"] == "backend-b"

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.build_backend_manager")
    def test_exhausted_singleton_burst_backend_is_not_installed(self, mock_build, mock_get_config):
        """Parsed provider exhaustion must survive ranking and manager creation."""
        now = datetime.now(timezone.utc)
        config = LLMBackendConfiguration.load_from_dict(
            {
                "quota_selection": {"strategy": "burst"},
                "backend_with_high_score": {"order": ["codex-cloud"]},
                "backends": {"codex-cloud": {"type": "codex-cloud"}},
            }
        )
        usage = parse_codex_weekly_usage(
            {
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 100,
                        "limit_window_seconds": 604_800,
                        "reset_at": (now + timedelta(days=2)).timestamp(),
                    }
                }
            },
            now=now,
        )
        mock_get_config.return_value = config
        with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage):
            manager = create_high_score_backend_manager()

        assert usage.remaining_percent == 0
        assert manager is None
        mock_build.assert_not_called()

    @pytest.mark.parametrize(
        ("config_section", "processor"),
        [
            ("backend_with_high_score_cloud", _process_issue_high_score_cloud),
            ("backend_cloud", _process_issue_cloud_backend),
        ],
    )
    def test_direct_issue_processing_does_not_restore_all_ineligible_candidates(self, config_section, processor):
        """Direct issue routing must preserve an empty quota-ranked failover list."""
        now = datetime.now(timezone.utc)
        config = LLMBackendConfiguration.load_from_dict(
            {
                "quota_selection": {"strategy": "burst"},
                config_section: {"order": ["codex-cloud", "disabled-cloud"]},
                "backends": {
                    "codex-cloud": {"type": "codex-cloud"},
                    "disabled-cloud": {"type": "jules", "enabled": False},
                },
            }
        )
        usage = parse_codex_weekly_usage(
            {
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 100,
                        "limit_window_seconds": 604_800,
                        "reset_at": (now + timedelta(days=2)).timestamp(),
                    }
                }
            },
            now=now,
        )
        fallback_actions = ["Used default backend"]
        with (
            patch("auto_coder.llm_backend_config.get_llm_config", return_value=config),
            patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
            patch("auto_coder.issue_processor._process_issue_codex_cloud_mode") as codex_dispatch,
            patch("auto_coder.issue_processor._process_issue_jules_mode") as jules_dispatch,
            patch("auto_coder.issue_processor._take_issue_actions", return_value=fallback_actions) as default_dispatch,
            patch("auto_coder.cli_helpers.create_high_score_cloud_backend_manager", return_value=None),
            patch("auto_coder.cli_helpers.create_high_score_backend_manager", return_value=None),
            patch("auto_coder.cli_helpers.create_cloud_backend_manager", return_value=None),
        ):
            actions = processor("owner/repo", {"number": 1685}, AutomationConfig(), MagicMock())

        assert actions == fallback_actions
        codex_dispatch.assert_not_called()
        jules_dispatch.assert_not_called()
        default_dispatch.assert_called_once()

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.build_backend_manager")
    @patch("auto_coder.quota_selector.rank_high_score_backends_by_quota")
    def test_create_high_score_cloud_backend_manager_preserves_order(self, mock_rank, mock_build, mock_get_config):
        """An ordered high-score cloud configuration keeps its declared priority."""
        mock_config = MagicMock(spec=LLMBackendConfiguration)
        mock_config.backend_with_high_score_cloud_order = ["claude-opus", "codex-cloud"]
        mock_config.get_backend_with_high_score_cloud.return_value = None
        mock_config.get_model_for_backend.side_effect = lambda x: f"model-{x}"
        mock_get_config.return_value = mock_config

        mock_rank.return_value = ["claude-opus", "codex-cloud"]

        create_high_score_cloud_backend_manager()

        mock_rank.assert_called_once_with(["claude-opus", "codex-cloud"], mock_config)
        mock_build.assert_called_once()
        call_args = mock_build.call_args[1]
        assert call_args["selected_backends"] == ["claude-opus", "codex-cloud"]
        assert call_args["primary_backend"] == "claude-opus"

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.build_backend_manager")
    @patch("auto_coder.quota_selector.evaluate_backend_quota")
    def test_cloud_order_regression_keeps_first_backend_primary(self, mock_evaluate, mock_build, mock_get_config):
        """backend_cloud.order wins even when its second item has more quota."""
        mock_config = MagicMock(spec=LLMBackendConfiguration)
        mock_config.backend_cloud_order = ["codex-cloud-spark", "codex-cloud-luna", "jules"]
        mock_config.backend_cloud_priority_groups = []
        mock_config.get_backend_cloud.return_value = None
        mock_config.get_model_for_backend.side_effect = lambda name: f"model-{name}"
        mock_get_config.return_value = mock_config
        surpluses = {
            "codex-cloud-spark": -0.8,
            "codex-cloud-luna": 0.8,
            "jules": None,
        }
        mock_evaluate.side_effect = lambda backend_name, **kwargs: BackendQuotaEvaluation(
            backend_name=backend_name,
            quota_surplus=surpluses[backend_name],
        )

        create_cloud_backend_manager()

        call_args = mock_build.call_args.kwargs
        assert call_args["selected_backends"] == [
            "codex-cloud-spark",
            "codex-cloud-luna",
            "jules",
        ]
        assert call_args["primary_backend"] == "codex-cloud-spark"
