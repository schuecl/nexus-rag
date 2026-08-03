"""#349: setup_profiling() must stay a no-op with no server address configured
(#133's opt-in posture), and must configure the real pyroscope agent exactly
once per process when it is. pyroscope.configure() is mocked throughout --
unlike tracing's pure-Python SDK, it starts a native background thread that
dials out to the configured server address, which unit tests must not do."""

from __future__ import annotations

from unittest.mock import patch

from common import profiling


class TestSetup:
    def setup_method(self):
        self._before = dict(profiling._state)
        profiling._state.update({"configured": False, "enabled": False})

    def teardown_method(self):
        profiling._state.update(self._before)

    @patch("common.profiling.pyroscope.configure")
    def test_disabled_without_server_address(self, mock_configure, monkeypatch):
        monkeypatch.delenv("PYROSCOPE_SERVER_ADDRESS", raising=False)
        assert profiling.setup_profiling("orchestration-mcp") is False
        mock_configure.assert_not_called()

    @patch("common.profiling.pyroscope.configure")
    def test_enabled_configures_agent_with_service_application_name(
        self, mock_configure, monkeypatch
    ):
        monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
        monkeypatch.delenv("PYROSCOPE_APPLICATION_NAME", raising=False)
        assert profiling.setup_profiling("orchestration-mcp") is True
        mock_configure.assert_called_once()
        kwargs = mock_configure.call_args.kwargs
        assert kwargs["application_name"] == "nexus-rag-orchestration-mcp"
        assert kwargs["server_address"] == "http://pyroscope:4040"
        assert kwargs["cpu_enabled"] is True
        # #349's chosen mode: CPU only, memory profiling stays off.
        assert kwargs["mem_enabled"] is False

    @patch("common.profiling.pyroscope.configure")
    def test_application_name_override_takes_precedence(self, mock_configure, monkeypatch):
        monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
        monkeypatch.setenv("PYROSCOPE_APPLICATION_NAME", "custom-name")
        profiling.setup_profiling("orchestration-mcp")
        assert mock_configure.call_args.kwargs["application_name"] == "custom-name"

    @patch("common.profiling.pyroscope.configure")
    def test_second_call_is_a_no_op(self, mock_configure, monkeypatch):
        # The native agent can't be meaningfully reconfigured in-process;
        # a second setup_profiling() call (module re-import in tests, a
        # second call site) must not start a second agent.
        monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
        assert profiling.setup_profiling("orchestration-mcp") is True
        assert profiling.setup_profiling("orchestration-mcp") is True
        mock_configure.assert_called_once()


class TestSampleRate:
    def setup_method(self):
        self._before = dict(profiling._state)

    def teardown_method(self):
        profiling._state.update(self._before)

    def test_default_is_100hz(self, monkeypatch):
        monkeypatch.delenv("PYROSCOPE_SAMPLE_RATE", raising=False)
        assert profiling._sample_rate() == 100

    def test_valid_rate_is_used(self, monkeypatch):
        monkeypatch.setenv("PYROSCOPE_SAMPLE_RATE", "50")
        assert profiling._sample_rate() == 50

    def test_non_positive_rate_falls_back(self, monkeypatch):
        monkeypatch.setenv("PYROSCOPE_SAMPLE_RATE", "0")
        assert profiling._sample_rate() == 100
        monkeypatch.setenv("PYROSCOPE_SAMPLE_RATE", "-5")
        assert profiling._sample_rate() == 100

    def test_non_integer_rate_falls_back(self, monkeypatch):
        monkeypatch.setenv("PYROSCOPE_SAMPLE_RATE", "nonsense")
        assert profiling._sample_rate() == 100

    @patch("common.profiling.pyroscope.configure")
    def test_custom_rate_reaches_configure(self, mock_configure, monkeypatch):
        monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
        monkeypatch.setenv("PYROSCOPE_SAMPLE_RATE", "25")
        profiling._state.update({"configured": False, "enabled": False})
        profiling.setup_profiling("ingestion-api")
        assert mock_configure.call_args.kwargs["sample_rate"] == 25
