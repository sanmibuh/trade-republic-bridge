"""Tests for tr_bridge.config — YAML file loading and validation."""

import textwrap

import pytest

from tr_bridge.config import Config, ConfigError, InstanceConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path, content: str) -> str:
    """Write *content* to a temp config.yml and return its path as a string."""
    p = tmp_path / "config.yml"
    p.write_text(textwrap.dedent(content))
    return str(p)


# ---------------------------------------------------------------------------
# Happy-path loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_minimal_valid_config(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: secret
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )

        cfg = Config.from_file(path)

        assert cfg.api_key == "secret"
        assert len(cfg.instances) == 1
        assert cfg.instances[0].name == "user1"
        assert cfg.instances[0].phone == "+49123456789"
        assert cfg.instances[0].pin == "1234"

    def test_multiple_instances(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: alice
                phone: "+491"
                pin: "0000"
              - name: bob
                phone: "+492"
                pin: "9999"
            """,
        )

        cfg = Config.from_file(path)

        assert [i.name for i in cfg.instances] == ["alice", "bob"]

    def test_instance_names_list(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: alice
                phone: "+491"
                pin: "0000"
              - name: bob
                phone: "+492"
                pin: "9999"
            """,
        )

        cfg = Config.from_file(path)

        assert cfg.instance_names == ["alice", "bob"]


# ---------------------------------------------------------------------------
# CONFIG_PATH constant and load()
# ---------------------------------------------------------------------------


class TestConfigPath:
    def test_default_path_is_data_config_yml(self) -> None:
        from tr_bridge.config import CONFIG_PATH

        assert CONFIG_PATH == "/data/config.yml"

    def test_load_delegates_to_from_file_with_default_path(self, tmp_path) -> None:
        from unittest.mock import patch

        import tr_bridge.config as cfg_module

        fake_cfg = object()
        with patch.object(
            cfg_module.Config, "from_file", return_value=fake_cfg
        ) as mock_from_file:
            result = cfg_module.Config.load()

        mock_from_file.assert_called_once_with()
        assert result is fake_cfg


# ---------------------------------------------------------------------------
# Top-level YAML type validation
# ---------------------------------------------------------------------------


class TestYamlTopLevelType:
    def test_yaml_list_at_top_level_raises(self, tmp_path) -> None:
        path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("- item1\n- item2\n")

        with pytest.raises(ConfigError, match="mapping"):
            Config.from_file(path)

    def test_yaml_string_at_top_level_raises(self, tmp_path) -> None:
        path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("just a string\n")

        with pytest.raises(ConfigError, match="mapping"):
            Config.from_file(path)

    def test_malformed_yaml_raises_config_error(self, tmp_path) -> None:
        path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("key: [unclosed\n")

        with pytest.raises(ConfigError, match=r"[Ii]nvalid YAML"):
            Config.from_file(path)


# ---------------------------------------------------------------------------
# instances must be a list of mappings
# ---------------------------------------------------------------------------


class TestInstancesMustBeMappings:
    def test_instances_as_string_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances: "not-a-list"
            """,
        )

        with pytest.raises(ConfigError, match="instances"):
            Config.from_file(path)

    def test_instance_entry_not_a_mapping_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - "just a string"
            """,
        )

        with pytest.raises(ConfigError, match="mapping"):
            Config.from_file(path)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


class TestFileMissing:
    def test_missing_file_raises_config_error(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            Config.from_file(str(tmp_path / "no_such_file.yml"))


# ---------------------------------------------------------------------------
# Required fields validation
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_missing_api_key_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            instances:
              - name: u1
                phone: "+491"
                pin: "0000"
            """,
        )

        with pytest.raises(ConfigError, match="api_key"):
            Config.from_file(path)

    def test_empty_api_key_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: ""
            instances:
              - name: u1
                phone: "+491"
                pin: "0000"
            """,
        )

        with pytest.raises(ConfigError, match="api_key"):
            Config.from_file(path)

    def test_missing_instances_raises(self, tmp_path) -> None:
        path = _write_config(tmp_path, "api_key: k\n")

        with pytest.raises(ConfigError, match="instances"):
            Config.from_file(path)

    def test_empty_instances_list_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances: []
            """,
        )

        with pytest.raises(ConfigError, match="instances"):
            Config.from_file(path)

    def test_instance_missing_phone_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: u1
                pin: "0000"
            """,
        )

        with pytest.raises(ConfigError, match="phone"):
            Config.from_file(path)

    def test_instance_missing_pin_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: u1
                phone: "+491"
            """,
        )

        with pytest.raises(ConfigError, match="pin"):
            Config.from_file(path)


# ---------------------------------------------------------------------------
# Instance name validation
# ---------------------------------------------------------------------------


class TestInstanceNameValidation:
    def test_valid_alphanumeric(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: user1
                phone: "+491"
                pin: "0000"
            """,
        )

        cfg = Config.from_file(path)

        assert cfg.instances[0].name == "user1"

    def test_valid_with_dash_and_underscore(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: my-user_1
                phone: "+491"
                pin: "0000"
            """,
        )

        cfg = Config.from_file(path)

        assert cfg.instances[0].name == "my-user_1"

    def test_dot_in_name_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: user.1
                phone: "+491"
                pin: "0000"
            """,
        )

        with pytest.raises(ConfigError, match=r"user\.1"):
            Config.from_file(path)

    def test_slash_in_name_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: user/1
                phone: "+491"
                pin: "0000"
            """,
        )

        with pytest.raises(ConfigError, match="user/1"):
            Config.from_file(path)


# ---------------------------------------------------------------------------
# session_dir helper
# ---------------------------------------------------------------------------


class TestSessionDir:
    def test_session_dir_path(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: user1
                phone: "+491"
                pin: "0000"
            """,
        )

        cfg = Config.from_file(path)

        assert cfg.session_dir("user1") == "/data/tr_session_user1"

    def test_unknown_instance_raises(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: k
            instances:
              - name: user1
                phone: "+491"
                pin: "0000"
            """,
        )

        cfg = Config.from_file(path)

        with pytest.raises(ConfigError, match="Unknown instance"):
            cfg.session_dir("ghost")


# ---------------------------------------------------------------------------
# InstanceConfig model
# ---------------------------------------------------------------------------


class TestDuplicateInstanceNames:
    def test_duplicate_instance_names_raise(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: secret
            instances:
              - name: user1
                phone: "+49111111111"
                pin: "1234"
              - name: user1
                phone: "+49222222222"
                pin: "5678"
            """,
        )

        with pytest.raises(ConfigError, match="Duplicate instance name"):
            Config.from_file(path)


class TestInstanceConfig:
    def test_fields(self) -> None:
        inst = InstanceConfig(name="u1", phone="+491", pin="0000")

        assert inst.name == "u1"
        assert inst.phone == "+491"
        assert inst.pin == "0000"


class TestTfaTimeout:
    def test_default_tfa_timeout_when_not_specified(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: "secret"
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )
        config = Config.from_file(path)
        assert config.tfa_timeout == 120

    def test_custom_tfa_timeout_is_loaded(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: "secret"
            tfa_timeout: 300
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )
        config = Config.from_file(path)
        assert config.tfa_timeout == 300

    def test_tfa_timeout_zero_raises_config_error(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: "secret"
            tfa_timeout: 0
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )
        with pytest.raises(ConfigError, match="tfa_timeout"):
            Config.from_file(path)

    def test_tfa_timeout_negative_raises_config_error(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: "secret"
            tfa_timeout: -5
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )
        with pytest.raises(ConfigError, match="tfa_timeout"):
            Config.from_file(path)

    def test_tfa_timeout_string_raises_config_error(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: "secret"
            tfa_timeout: "fast"
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )
        with pytest.raises(ConfigError, match="tfa_timeout"):
            Config.from_file(path)

    def test_tfa_timeout_bool_true_raises_config_error(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: "secret"
            tfa_timeout: true
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )
        with pytest.raises(ConfigError, match="tfa_timeout"):
            Config.from_file(path)

    def test_tfa_timeout_bool_false_raises_config_error(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            """
            api_key: "secret"
            tfa_timeout: false
            instances:
              - name: user1
                phone: "+49123456789"
                pin: "1234"
            """,
        )
        with pytest.raises(ConfigError, match="tfa_timeout"):
            Config.from_file(path)
