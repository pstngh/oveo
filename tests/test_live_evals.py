"""Prompt evaluations: offline proof of the harness, and opt-in runs against the model.

The offline tests run every scenario through the real generation path with a
known-good answer that its check must accept and a known-bad answer it must reject.
Before deploying prompt changes, run the same scenarios against the pinned model (it
bills real model calls and takes several minutes):

    OVEO_LIVE_EVALS=1 OVEO_OPENROUTER_API_KEY=sk-or-... \\
        uv run --frozen pytest -m live tests/test_live_evals.py
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from oveo.config import Settings
from oveo.db import Database
from oveo.generation import OpenRouterProvider
from oveo.models import User
from tests.live_evals import (
    SCENARIOS,
    SUMMARY_BAD,
    SUMMARY_GOOD,
    CannedProvider,
    Scenario,
    check_summary,
    run_scenario,
    summarize,
)

LIVE = os.environ.get("OVEO_LIVE_EVALS") == "1" and bool(os.environ.get("OVEO_OPENROUTER_API_KEY"))
live_only = pytest.mark.skipif(
    not LIVE, reason="set OVEO_LIVE_EVALS=1 and OVEO_OPENROUTER_API_KEY to call the model"
)
NAMES = [scenario.name for scenario in SCENARIOS]


def _live_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "openrouter_api_key": SecretStr(os.environ["OVEO_OPENROUTER_API_KEY"]),
            "provider_retry_attempts": 3,
        }
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=NAMES)
async def test_scenario_accepts_a_known_good_answer(
    manager_database: tuple[Database, Settings, User], scenario: Scenario
) -> None:
    database, settings, user = manager_database
    provider = CannedProvider(scenario.good, title=scenario.title)
    outcome = await run_scenario(
        scenario,
        database=database,
        settings=settings,
        user=user,
        provider=provider,
        wait_seconds=10,
    )
    scenario.check(outcome)
    assert not provider.answers


@pytest.mark.parametrize("scenario", SCENARIOS, ids=NAMES)
async def test_scenario_rejects_a_known_bad_answer(
    manager_database: tuple[Database, Settings, User], scenario: Scenario
) -> None:
    database, settings, user = manager_database
    provider = CannedProvider(scenario.bad, title=scenario.title)
    outcome = await run_scenario(
        scenario,
        database=database,
        settings=settings,
        user=user,
        provider=provider,
        wait_seconds=10,
    )
    # The bad answers are valid responses, so the scenario's own check must catch them.
    assert [snapshot["status"] for snapshot in outcome.snapshots] == ["completed"] * len(
        scenario.turns
    )
    assert not provider.answers
    with pytest.raises(AssertionError):
        scenario.check(outcome)


async def test_summary_check_accepts_and_rejects_known_answers() -> None:
    check_summary(await summarize(CannedProvider((), title="", summary=SUMMARY_GOOD)))
    with pytest.raises(AssertionError):
        check_summary(await summarize(CannedProvider((), title="", summary=SUMMARY_BAD)))


@pytest.mark.live
@live_only
@pytest.mark.parametrize("scenario", SCENARIOS, ids=NAMES)
async def test_scenario_with_the_pinned_model(
    manager_database: tuple[Database, Settings, User], scenario: Scenario
) -> None:
    database, settings, user = manager_database
    live_settings = _live_settings(settings)
    provider = OpenRouterProvider(live_settings)
    try:
        outcome = await run_scenario(
            scenario,
            database=database,
            settings=live_settings,
            user=user,
            provider=provider,
            wait_seconds=900,
        )
    finally:
        await provider.aclose()
    scenario.check(outcome)


@pytest.mark.live
@live_only
async def test_summary_with_the_pinned_model(
    manager_database: tuple[Database, Settings, User],
) -> None:
    _, settings, _ = manager_database
    provider = OpenRouterProvider(_live_settings(settings))
    try:
        summary = await summarize(provider)
    finally:
        await provider.aclose()
    check_summary(summary)
