"""
Nexus AI — Settings unit tests.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


class TestSettings:

    def test_default_host_is_localhost(self):
        s = Settings()
        assert s.host == "127.0.0.1"

    def test_0000_raises(self):
        with pytest.raises((ValidationError, ValueError)):
            Settings(host="0.0.0.0")

    def test_default_rate_limit(self):
        s = Settings()
        assert s.rate_limit_per_minute == 10

    def test_excessive_rate_limit_raises(self):
        with pytest.raises((ValidationError, ValueError)):
            Settings(rate_limit_per_minute=500)

    def test_default_max_debate_rounds(self):
        s = Settings()
        assert s.max_debate_rounds == 3

    def test_default_convergence_threshold(self):
        s = Settings()
        assert s.convergence_threshold == 0.92

    def test_default_max_browser_agents(self):
        s = Settings()
        assert s.max_browser_agents == 6

    def test_hitl_expiry_default(self):
        s = Settings()
        assert s.hitl_expiry_hours == 24

    def test_data_paths_exist_in_project(self):
        s = Settings()
        # Parent of data_dir should exist (project root)
        assert s.data_dir.parent.exists()
