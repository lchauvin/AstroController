"""
The settings surface: description, coercion, and the TOML patcher.

The patcher gets the most attention here. It edits a file the process has to
start from next time, in the dark, so the properties that matter are that it
never loses a comment and never produces something that will not parse.
"""

from __future__ import annotations

import pytest

from astrocontroller import settings
from astrocontroller.config import Config, ConfigError, load_config

SAMPLE = """\
# AstroController -- live configuration.
# The observatory PC is AstroMini.

[server]
# Loopback only unless a token is set.
host = "127.0.0.1"
port = 3005

[nina]
host = "192.168.2.200"
port = 1888

[phd2]
host = "192.168.2.200"
instance = 1          # -> port 4400

[site]
# Left unset on purpose: derived from NINA's profile.
# latitude = 45.5

[tuning]
mode = "suggest"
enabled = false

[[tuning.bounds]]
axis = "ra"
param = "aggression"
lo = 0.4
hi = 1.0
max_delta = 0.1
"""


# ── describing ─────────────────────────────────────────────────────────


def test_describe_covers_every_declared_field():
    described = settings.describe(Config())
    seen = {f["path"] for g in described["groups"] for f in g["fields"]}
    assert seen == set(settings.FIELDS_BY_PATH)


def test_describe_reports_current_values():
    config = Config()
    config.nina.host = "10.0.0.9"
    described = settings.describe(config)
    fields = {f["path"]: f for g in described["groups"] for f in g["fields"]}
    assert fields["nina.host"]["value"] == "10.0.0.9"
    assert fields["tuning.mode"]["choices"] == ["off", "suggest", "auto"]


def test_a_simulated_config_is_not_writable():
    config = Config()
    config.simulated = True
    assert settings.describe(config)["writable"] is False


# ── coercion ───────────────────────────────────────────────────────────


def test_integer_fields_come_back_as_integers():
    config = Config()
    settings.apply_to(config, {"nina.port": "1888"})
    assert config.nina.port == 1888
    assert isinstance(config.nina.port, int)


def test_out_of_range_values_are_rejected():
    config = Config()
    with pytest.raises(ConfigError, match="at most"):
        settings.apply_to(config, {"phd2.instance": 99})


def test_unknown_settings_are_rejected():
    with pytest.raises(ConfigError, match="not an editable setting"):
        settings.apply_to(Config(), {"tuning.panic_rms_multiple": 9.0})


def test_a_rejected_patch_changes_nothing():
    # Every value is coerced before any is written, so a bad field in the
    # middle of a patch cannot leave half of it applied to a running rig.
    config = Config()
    with pytest.raises(ConfigError):
        settings.apply_to(config, {"nina.host": "10.0.0.9", "phd2.instance": 99})
    assert config.nina.host == "127.0.0.1"


def test_only_the_site_coordinates_may_be_cleared():
    config = Config()
    settings.apply_to(config, {"site.latitude": ""})
    assert config.site.latitude is None
    with pytest.raises(ConfigError, match="cannot be empty"):
        settings.apply_to(config, {"nina.port": ""})


def test_the_image_share_may_be_emptied():
    config = Config()
    config.images.share_path = "G:/Astro/.Download"
    settings.apply_to(config, {"images.share_path": ""})
    assert config.images.share_path == ""


def test_restart_required_names_only_the_non_live_fields():
    labels = settings.restart_required(
        ["tuning.mode", "nina.host", "images.share_path", "site.latitude", "server.port"]
    )
    assert labels == ["Bind port", "NINA host"]


# ── patching ───────────────────────────────────────────────────────────


def test_patching_replaces_a_value_in_place():
    out = settings.patch_toml(SAMPLE, {"nina.host": "10.0.0.5"})
    assert 'host = "10.0.0.5"' in out
    # The other section's host is untouched.
    assert 'host = "127.0.0.1"' in out


def test_patching_keeps_every_comment():
    out = settings.patch_toml(SAMPLE, {"tuning.mode": "auto", "server.port": 3010})
    for comment in (
        "# AstroController -- live configuration.",
        "# The observatory PC is AstroMini.",
        "# Loopback only unless a token is set.",
        "# Left unset on purpose: derived from NINA's profile.",
    ):
        assert comment in out


def test_an_inline_comment_survives_the_value_it_annotated():
    out = settings.patch_toml(SAMPLE, {"phd2.instance": 2})
    assert "instance = 2" in out
    assert "# -> port 4400" in out


def test_a_commented_out_key_is_not_treated_as_the_setting():
    # `# latitude = 45.5` is documentation. Writing over it would both destroy
    # the hint and leave the real key unset.
    out = settings.patch_toml(SAMPLE, {"site.latitude": 45.5})
    assert "# latitude = 45.5" in out
    assert "\nlatitude = 45.5" in out


def test_a_new_key_lands_inside_its_own_section():
    out = settings.patch_toml(SAMPLE, {"server.port": 3005, "nina.timeout_s": 20})
    lines = out.splitlines()
    nina = lines.index("[nina]")
    phd2 = lines.index("[phd2]")
    assert any("timeout_s = 20" in line for line in lines[nina:phd2])


def test_a_missing_section_is_created():
    out = settings.patch_toml(SAMPLE, {"images.share_path": "G:/Astro/.Download"})
    assert "[images]" in out
    assert 'share_path = "G:/Astro/.Download"' in out


def test_an_array_of_tables_is_left_alone():
    # Anything after `[[tuning.bounds]]` belongs to that table, so appending a
    # `[tuning]` key there would silently attach it to the wrong one.
    out = settings.patch_toml(SAMPLE, {"tuning.max_changes_per_hour": 2})
    lines = out.splitlines()
    bounds = lines.index("[[tuning.bounds]]")
    assert any("max_changes_per_hour = 2" in line for line in lines[:bounds])
    assert all("max_changes_per_hour" not in line for line in lines[bounds:])


def test_clearing_a_value_comments_the_line_out():
    patched = settings.patch_toml(SAMPLE, {"server.port": 3010})
    cleared = settings.patch_toml(patched, {"server.port": None})
    assert "# port = 3010" in cleared


def test_a_patched_file_still_parses(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    path.write_text(
        settings.patch_toml(
            SAMPLE,
            {
                "nina.host": "10.0.0.5",
                "tuning.mode": "auto",
                "images.share_path": "G:/Astro/.Download",
                "site.latitude": 45.5,
            },
        ),
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert config.nina.host == "10.0.0.5"
    assert config.tuning.mode == "auto"
    assert config.images.share_path == "G:/Astro/.Download"
    assert config.site.latitude == 45.5
    assert len(config.tuning.bounds) == 1


def test_string_values_are_escaped():
    out = settings.patch_toml(SAMPLE, {"storage.db_path": 'C:\\a "b"\\x.db'})
    reparsed = __import__("tomllib").loads(out)
    assert reparsed["storage"]["db_path"] == 'C:\\a "b"\\x.db'


# ── saving ─────────────────────────────────────────────────────────────


def test_save_writes_a_backup_and_the_new_file(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    config = load_config(str(path))

    coerced = settings.apply_to(config, {"nina.host": "10.0.0.5"})
    settings.save(config, coerced)

    assert 'host = "10.0.0.5"' in path.read_text(encoding="utf-8")
    assert path.with_suffix(".toml.bak").read_text(encoding="utf-8") == SAMPLE


def test_save_refuses_a_simulated_config(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    config = load_config(str(path))
    config.simulated = True
    with pytest.raises(ConfigError, match="simulation mode"):
        settings.save(config, {"nina.host": "10.0.0.5"})
    assert path.read_text(encoding="utf-8") == SAMPLE


def test_save_leaves_the_original_when_the_result_would_not_parse(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    config = load_config(str(path))

    def refuse(_path: str):
        raise ConfigError("nope")

    with pytest.raises(ConfigError):
        settings.save(config, {"nina.host": "10.0.0.5"}, loader=refuse)
    assert path.read_text(encoding="utf-8") == SAMPLE
    assert not path.with_suffix(".toml.new").exists()
