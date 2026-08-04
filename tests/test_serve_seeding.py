"""Seeding a fresh volume must not become a way to silently revert risk limits.

The container overrides TRADING_ROOT so data/, config/ and memory/ live on the
mounted volume instead of the ephemeral image layer. On boot the image's config
is copied across — and the whole hazard is that copy running on *every* deploy,
which would quietly restore limits.yaml over whatever the dashboard saved.
"""

from pathlib import Path

from trading.cli import _seed_root

IMAGE = Path(__file__).resolve().parents[1]


class TestSeedRoot:
    def test_no_override_is_a_no_op(self, monkeypatch, tmp_path):
        """Running from the checkout: the root already is the image."""
        monkeypatch.setattr("trading.config.PROJECT_ROOT", IMAGE)
        _seed_root()
        assert not (IMAGE / "data" / "config").exists()

    def test_first_boot_seeds_config_memory_and_data(self, monkeypatch, tmp_path):
        monkeypatch.setattr("trading.config.PROJECT_ROOT", tmp_path)
        _seed_root()
        assert (tmp_path / "data").is_dir()
        assert (tmp_path / "config" / "limits.yaml").is_file()
        assert (tmp_path / "memory" / "lessons.md").is_file()

    def test_redeploy_never_overwrites_saved_limits(self, monkeypatch, tmp_path):
        """The regression that matters: a second boot must leave edits alone."""
        monkeypatch.setattr("trading.config.PROJECT_ROOT", tmp_path)
        _seed_root()
        edited = tmp_path / "config" / "limits.yaml"
        edited.write_text("mode: live  # edited from the dashboard\n", encoding="utf-8")

        _seed_root()  # redeploy

        assert edited.read_text(encoding="utf-8").startswith("mode: live")

    def test_a_config_file_added_upstream_still_lands(self, monkeypatch, tmp_path):
        """Skipping existing files must not mean skipping the whole directory."""
        monkeypatch.setattr("trading.config.PROJECT_ROOT", tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "limits.yaml").write_text("mode: paper\n", encoding="utf-8")

        _seed_root()

        assert (tmp_path / "config" / "settings.yaml").is_file()
        assert (tmp_path / "config" / "limits.yaml").read_text(encoding="utf-8") == "mode: paper\n"
